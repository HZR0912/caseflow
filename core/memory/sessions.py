"""Durable LangGraph session slots, encrypted at rest and bounded by owner and lifetime."""

import json
from datetime import timedelta
from typing import TypedDict
from uuid import uuid4

from cryptography.fernet import InvalidToken
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

from core.graph.approval_service import ApprovalError, digest
from core.graph.onboarding import OnboardingFacts, RuleInterpreter
from core.graph.workflow import Redactor
from core.guardrails.injection import RuleInjectionDetector
from core.guardrails.normalize import normalize


class SessionState(TypedDict, total=False):
    version: int
    encrypted_facts: str
    turns: list[dict]


def session_graph(saver):
    builder = StateGraph(SessionState)
    builder.add_node("confirm", lambda state: state)
    builder.add_edge(START, "confirm")
    builder.add_edge("confirm", END)
    return builder.compile(checkpointer=saver)


class SessionService:
    def __init__(self, memory):
        self.memory, self.approvals = memory, memory.approvals

    @staticmethod
    def thread_id(subject, session_id):
        return "memory:" + digest([subject.tenant, subject.id, session_id])

    def cipher(self):
        if not self.approvals.cipher:
            raise ApprovalError(503, "memory_encryption_key_not_configured")
        return self.approvals.cipher.fernet

    def seal(self, subject, session_id, facts):
        return (
            self.cipher()
            .encrypt(
                json.dumps(
                    {
                        "tenant": subject.tenant,
                        "owner": subject.id,
                        "session": session_id,
                        "facts": facts,
                    },
                    ensure_ascii=False,
                ).encode()
            )
            .decode()
        )

    def open(self, subject, session_id, value):
        if value is None:
            return {}
        try:
            data = json.loads(self.cipher().decrypt(value.encode()))
            if (data["tenant"], data["owner"], data["session"]) != (
                subject.tenant,
                subject.id,
                session_id,
            ):
                raise ValueError("binding")
            OnboardingFacts.model_validate(data["facts"])
            return data["facts"]
        except (InvalidToken, ValueError, TypeError, KeyError) as exc:
            raise ApprovalError(409, "memory_payload_invalid") from exc

    async def create(self, subject):
        await self.memory.require(subject, "memory.write")
        self.cipher()
        session_id = str(uuid4())
        expires = self.memory.clock() + timedelta(days=30)
        async with self.approvals.session() as (conn, _):
            await conn.execute(
                "INSERT INTO memory_sessions(id,tenant,owner_id,expires_at) VALUES (%s,%s,%s,%s)",
                (session_id, subject.tenant, subject.id, expires),
            )
        return {"id": session_id, "version": 0, "expires_at": expires.isoformat()}

    async def row(self, conn, subject, session_id, allow_expired=False):
        row = await (
            await conn.execute(
                "SELECT * FROM memory_sessions WHERE id=%s AND tenant=%s AND owner_id=%s",
                (session_id, subject.tenant, subject.id),
            )
        ).fetchone()
        if row is None:
            raise ApprovalError(404, "memory_session_not_found")
        if not allow_expired and row["expires_at"] <= self.memory.clock():
            raise ApprovalError(410, "memory_session_expired")
        return row

    async def state(self, conn, subject, session_id):
        saver = AsyncPostgresSaver(conn)
        graph = session_graph(saver)
        config = {"configurable": {"thread_id": self.thread_id(subject, session_id)}}
        current = await graph.aget_state(config)
        if current.next:
            # A crash after input checkpoint but before confirm has no external side effect.
            await graph.ainvoke(None, config)
            current = await graph.aget_state(config)
        return graph, config, current.values

    @staticmethod
    def redact(facts, text=""):
        redactor = Redactor(text)
        for employee in facts.get("employees", []):
            redactor.register("person", employee["name"])
            redactor.register("account", employee["username"])
        return redactor

    def view(self, subject, session_id, row, state):
        facts = self.open(subject, session_id, state.get("encrypted_facts"))
        return {
            "id": session_id,
            "version": state.get("version", 0),
            "expires_at": row["expires_at"].isoformat(),
            "facts": self.redact(facts).clean(facts),
            "questions": OnboardingFacts.model_validate(facts).questions(),
            "turns": [
                {k: v for k, v in t.items() if k != "input_hash"} for t in state.get("turns", [])
            ],
        }

    async def get(self, subject, session_id):
        await self.memory.require(subject, "memory.read")
        async with self.approvals.session(self.thread_id(subject, session_id)) as (conn, _):
            row = await self.row(conn, subject, session_id)
            _, _, state = await self.state(conn, subject, session_id)
            return self.view(subject, session_id, row, state)

    async def turn(self, subject, session_id, body):
        await self.memory.require(subject, "memory.write")
        self.cipher()
        # Do not recursively import other memories into confirmed session slots.
        if body.request.memory is not None:
            raise ApprovalError(422, "session_turn_cannot_import_memory")
        if RuleInjectionDetector().check(normalize(body.request.model_dump_json())).blocked:
            raise ApprovalError(422, "unsafe_session_input")
        async with self.approvals.session(self.thread_id(subject, session_id)) as (conn, _):
            row = await self.row(conn, subject, session_id)
            graph, config, state = await self.state(conn, subject, session_id)
            input_hash = digest(body.model_dump(mode="json"))
            for turn in state.get("turns", []):
                if turn["turn_id"] == str(body.turn_id):
                    if turn["input_hash"] != input_hash:
                        raise ApprovalError(409, "memory_turn_payload_mismatch")
                    return self.view(subject, session_id, row, state)
            if state.get("version", 0) != body.expected_version:
                raise ApprovalError(409, "memory_session_version_conflict")
            previous = self.open(subject, session_id, state.get("encrypted_facts"))
            parsed = await RuleInterpreter().understand(body.request)
            merged = {k: v for k, v in previous.items() if k not in body.clear_fields}
            merged.update(parsed.model_dump(mode="json", exclude_unset=True))
            # Explicit null/empty is a correction, not a missing value to fill from old memory.
            merged.update(body.request.facts.model_dump(mode="json", exclude_unset=True))
            OnboardingFacts.model_validate(merged)
            turns = [
                *state.get("turns", []),
                {
                    "turn_id": str(body.turn_id),
                    "input_hash": input_hash,
                    "confirmed_fields": sorted(parsed.model_fields_set),
                    # No free-form transcript is retained: even unrecognised names stay off disk.
                    "summary": "已确认本轮结构化槽位",
                },
            ][-32:]
            saved = await graph.ainvoke(
                {
                    "version": body.expected_version + 1,
                    "encrypted_facts": self.seal(subject, session_id, merged),
                    "turns": turns,
                },
                config,
            )
            return self.view(subject, session_id, row, saved)

    async def facts(self, subject, session_id, expected_version):
        await self.memory.require(subject, "memory.read")
        async with self.approvals.session(self.thread_id(subject, session_id)) as (conn, _):
            await self.row(conn, subject, session_id)
            _, _, state = await self.state(conn, subject, session_id)
            if expected_version is None or state.get("version", 0) != expected_version:
                raise ApprovalError(409, "memory_session_version_conflict")
            return self.open(subject, session_id, state.get("encrypted_facts"))

    async def forget(self, subject, session_id):
        await self.memory.require(subject, "memory.write")
        async with self.approvals.session(self.thread_id(subject, session_id)) as (conn, _):
            await self.row(conn, subject, session_id, allow_expired=True)
            async with conn.transaction():
                await AsyncPostgresSaver(conn).adelete_thread(self.thread_id(subject, session_id))
                await conn.execute("DELETE FROM memory_sessions WHERE id=%s", (session_id,))
        return {"status": "forgotten"}
