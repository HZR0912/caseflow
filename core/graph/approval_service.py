"""Persisted approval lifecycle. DB intent precedes resumable checkpoint advancement."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from core.graph.approval import ApprovalDecision, build_approval_graph
from core.graph.workflow import Redactor
from core.policy.opa_client import PolicyQuery, PolicyResource
from core.tools.payload import PreparedApproval


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ApprovalError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail
        super().__init__(detail)


class ApprovalService:
    def __init__(self, url, policy, ttl_hours=24, cipher=None, *, pool=None):
        self.url = url.replace("postgresql+psycopg://", "postgresql://")
        self.policy = policy
        self.ttl_hours = ttl_hours
        self.cipher = cipher
        self.ready = False
        self.setup_lock = asyncio.Lock()
        self.pool = pool

    @asynccontextmanager
    async def connection(self):
        if self.pool is not None:
            async with self.pool.connection() as conn:
                yield conn
        else:
            # Short-lived scripts and explicit restart probes own a fresh connection.
            async with await psycopg.AsyncConnection.connect(
                self.url,
                autocommit=True,
                row_factory=dict_row,
                prepare_threshold=0,
                connect_timeout=5,
            ) as conn:
                yield conn

    @asynccontextmanager
    async def session(self, lock_id=None):
        try:
            async with self.connection() as conn:
                try:
                    async with self.locked_session(conn, lock_id) as session:
                        yield session
                finally:
                    # Advisory locks belong to the physical connection, not the transaction.
                    # Release on success, rejection, error and cancellation BEFORE returning
                    # it to the pool. Discard a connection if cleanup cannot be confirmed.
                    if not conn.closed:
                        try:
                            await conn.execute("SELECT pg_advisory_unlock_all()")
                        except BaseException:
                            await conn.close()
                            raise
        except psycopg.Error as exc:
            raise ApprovalError(503, "approval_storage_unavailable") from exc

    @asynccontextmanager
    async def locked_session(self, conn, lock_id):
        saver = AsyncPostgresSaver(conn)
        async with self.setup_lock:
            if not self.ready:
                # Separate workers can initialize concurrently; serialize saver migrations.
                # A blocking SELECT holds a snapshot while waiting. Concurrent
                # checkpoint index creation waits for that snapshot, deadlocking
                # the initializer against the lock waiter. Poll without a live
                # statement/transaction instead.
                for _ in range(600):
                    cursor = await conn.execute("SELECT pg_try_advisory_lock(60424001) AS locked")
                    if (await cursor.fetchone())["locked"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise ApprovalError(503, "approval_setup_busy")
                try:
                    await saver.setup()
                    self.ready = True
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(60424001)")
        if lock_id:
            key = int.from_bytes(hashlib.sha256(lock_id.encode()).digest()[:8], signed=True)
            cursor = await conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (key,))
            if not (await cursor.fetchone())["locked"]:
                raise ApprovalError(409, "approval_busy_retry")
        yield conn, build_approval_graph(saver)

    async def permitted(self, subject, action, request_id=None):
        decision = await self.policy.authorize(
            PolicyQuery(
                subject=subject,
                action=action,
                resource=PolicyResource(type="approval_request", id=request_id),
            )
        )
        return decision.allow

    async def require(self, subject, action, request_id=None):
        if not await self.permitted(subject, action, request_id):
            raise ApprovalError(403, "approval_policy_denied")

    async def row(self, conn, request_id, subject):
        cursor = await conn.execute(
            "SELECT * FROM approval_requests WHERE id=%s AND tenant=%s",
            (request_id, subject.tenant),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ApprovalError(404, "approval_not_found")
        return row

    async def readable(self, row, subject):
        if row["requester"] != subject.id:
            await self.require(subject, "approval.read", row["id"])

    @staticmethod
    def config(request_id):
        return {"configurable": {"thread_id": "approval:" + request_id}}

    async def drive(self, conn, graph, row):
        """Repair any crash window using the persisted snapshot and single decision."""
        if digest(row["snapshot"]) != row["plan_hash"]:
            raise ApprovalError(409, "approval_plan_changed")
        config = self.config(row["id"])
        state = await graph.aget_state(config)
        if not state.values:
            await graph.ainvoke(
                {
                    "request_id": row["id"],
                    "snapshot": row["snapshot"],
                    "plan_hash": row["plan_hash"],
                    "status": "submitted",
                    "trace": [],
                },
                config,
            )
        elif state.next and state.next != ("approval",):
            # A crash after review or approval checkpoint, before the next node.
            await graph.ainvoke(None, config)
        state = await graph.aget_state(config)
        if state.next == ("approval",) and not any(task.interrupts for task in state.tasks):
            await graph.ainvoke(None, config)
            state = await graph.aget_state(config)
        if row["decision"] and state.next == ("approval",):
            await graph.ainvoke(Command(resume=row["decision"]), config)
        state = await graph.aget_state(config)
        await conn.execute(
            "UPDATE approval_requests SET status=%s WHERE id=%s",
            (state.values["status"], row["id"]),
        )
        row["status"] = state.values["status"]
        return self.view(row, state)

    @staticmethod
    def view(row, state=None):
        expired = datetime.now(UTC) >= row["expires_at"]
        result = {
            key: row[key]
            for key in (
                "id",
                "requester",
                "plan_hash",
                "created_at",
                "expires_at",
                "decision",
                "status",
            )
        }
        result["expired"] = expired
        if state is not None:
            result.update(
                {
                    "status": state.values.get("status", row["status"]),
                    "snapshot": row["snapshot"],
                    "trace": state.values.get("trace", []),
                    "next": list(state.next),
                    "report": state.values.get("report"),
                    "interrupted": any(task.interrupts for task in state.tasks),
                    "checkpoint_id": (state.config or {})
                    .get("configurable", {})
                    .get("checkpoint_id"),
                    "recovery_needed": bool(row["decision"] and state.next)
                    or not state.values
                    or row["status"] != state.values.get("status"),
                }
            )
        return result

    async def create(self, request, subject, creation_key, prepare):
        await self.require(subject, "approval.create")
        # Key scope includes requester and tenant; caller cannot select an existing thread.
        request_id = str(
            uuid5(NAMESPACE_URL, json.dumps([subject.tenant, subject.id, creation_key]))
        )
        input_hash = digest(request.model_dump(mode="json"))
        async with self.session(request_id) as (conn, graph):
            cursor = await conn.execute(
                "SELECT * FROM approval_requests WHERE id=%s", (request_id,)
            )
            existing = await cursor.fetchone()
            if existing:
                if existing["input_hash"] != input_hash:
                    raise ApprovalError(409, "creation_key_payload_mismatch")
                return await self.drive(conn, graph, existing)
            prepared = await prepare()
            snapshot = prepared.snapshot if isinstance(prepared, PreparedApproval) else prepared
            if snapshot.get("status") != "awaiting_approval":
                return {"status": snapshot["status"], "snapshot": snapshot}
            payload = None
            if isinstance(prepared, PreparedApproval):
                if self.cipher is None:
                    raise ApprovalError(503, "execution_key_not_configured")
                snapshot = {
                    **snapshot,
                    "execution_plan_hash": digest(prepared.plan.model_dump(mode="json")),
                }
                payload = self.cipher.seal(prepared.plan, subject.tenant, request_id)
            # Only the server-generated, redacted result crosses this boundary.
            plan_hash = digest(snapshot)
            await conn.execute(
                "INSERT INTO approval_requests "
                "(id,tenant,requester,creation_key,input_hash,plan_hash,snapshot,"
                "expires_at,execution_payload) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    request_id,
                    subject.tenant,
                    subject.id,
                    creation_key,
                    input_hash,
                    plan_hash,
                    Jsonb(snapshot),
                    datetime.now(UTC) + timedelta(hours=self.ttl_hours),
                    payload,
                ),
            )
            row = await self.row(conn, request_id, subject)
            return await self.drive(conn, graph, row)

    async def get(self, request_id, subject):
        async with self.session() as (conn, graph):
            row = await self.row(conn, request_id, subject)
            await self.readable(row, subject)
            state = await graph.aget_state(self.config(request_id))
            result = self.view(row, state)
            cursor = await conn.execute(
                "SELECT journal FROM execution_runs WHERE request_id=%s", (request_id,)
            )
            execution = await cursor.fetchone()
            result["execution"] = execution["journal"] if execution else None
            result["executable"] = bool(row.get("execution_payload"))
            return result

    async def list(self, subject):
        all_in_tenant = await self.permitted(subject, "approval.read")
        async with self.session() as (conn, _):
            cursor = await conn.execute(
                "SELECT a.*, e.journal AS execution FROM approval_requests a "
                "LEFT JOIN execution_runs e ON e.request_id=a.id "
                "WHERE a.tenant=%s AND (%s OR a.requester=%s) "
                "ORDER BY a.created_at DESC, a.id DESC LIMIT 100",
                (subject.tenant, all_in_tenant, subject.id),
            )
            return [
                {**self.view(row), "execution": row["execution"]} for row in await cursor.fetchall()
            ]

    async def decide(self, request_id, choice: ApprovalDecision, subject):
        async with self.session(request_id) as (conn, graph):
            row = await self.row(conn, request_id, subject)
            await self.require(subject, "approval.decide", request_id)
            if row["requester"] == subject.id:
                raise ApprovalError(403, "self_approval_forbidden")
            if row["plan_hash"] != choice.plan_hash or digest(row["snapshot"]) != row["plan_hash"]:
                raise ApprovalError(409, "approval_plan_changed")
            reason = Redactor(choice.reason).clean(choice.reason)
            decision = {**choice.model_dump(), "reason": reason, "actor": subject.id}
            if row["decision"]:
                if any(row["decision"].get(k) != v for k, v in decision.items()):
                    raise ApprovalError(409, "approval_already_decided")
            else:
                if datetime.now(UTC) >= row["expires_at"]:
                    raise ApprovalError(409, "approval_expired")
                decision["decided_at"] = datetime.now(UTC).isoformat()
                # Autocommit intent BEFORE graph resume: retries cannot change the winner.
                await conn.execute(
                    "UPDATE approval_requests SET decision=%s WHERE id=%s",
                    (Jsonb(decision), request_id),
                )
                row["decision"] = decision
            return await self.drive(conn, graph, row)

    async def recover(self, request_id, subject):
        async with self.session(request_id) as (conn, graph):
            row = await self.row(conn, request_id, subject)
            await self.readable(row, subject)
            if row["decision"]:
                # Recovery must still be authorized now; no token or old role is replayed.
                await self.require(subject, "approval.decide", request_id)
            if digest(row["snapshot"]) != row["plan_hash"]:
                raise ApprovalError(409, "approval_plan_changed")
            return await self.drive(conn, graph, row)
