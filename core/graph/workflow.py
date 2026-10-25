"""W5 请求作用域 LangGraph。原文、身份和 vault 在闭包里，不进入 checkpoint。"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Protocol, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from core.graph.contracts import Plan
from core.graph.onboarding import Interpreter, PlanningRequest, RuleInterpreter, make_plan
from core.guardrails.injection import RuleInjectionDetector
from core.guardrails.normalize import normalize
from core.guardrails.pii import PiiVault, mask
from core.guardrails.pipeline import Guardrails
from core.obs.runtime import headers
from core.obs.runtime import node as observed_node
from core.policy.opa_client import PolicyClient, PolicyQuery, PolicyResource, PolicySubject
from core.rag.chunking import load_corpus
from core.rag.offline import OfflineIndex

POLICIES = Path(__file__).resolve().parents[2] / "data" / "policies"


class PreviewClient(Protocol):
    async def preview(self, plan: Plan) -> dict: ...


class HttpPreviewClient:
    def __init__(self, base_url: str, bearer: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.bearer = bearer
        self.client = client

    async def preview(self, plan: Plan) -> dict:
        async def send(client):
            response = await client.post(
                self.base_url + "/v1/preview",
                json=plan.model_dump(mode="json"),
                headers={"Authorization": self.bearer, **headers()},
                timeout=10,
            )
            response.raise_for_status()
            body = response.json()
            # 校验整批完整性：不能将缺步骤或标记为正式执行的响应当成成功预演。
            if not isinstance(body, dict) or not isinstance(body.get("steps"), list):
                raise ValueError("invalid_preview_response")
            items = body.get("steps", [])
            if any(
                not isinstance(item, dict) or not isinstance(item.get("step_id"), str)
                for item in items
            ):
                raise ValueError("invalid_preview_response")
            ids = [item.get("step_id") for item in items]
            if (
                body.get("dry_run") is not True
                or type(body.get("ok")) is not bool
                or len(ids) != len(plan.steps)
                or set(ids) != {step.id for step in plan.steps}
                or any(item.get("status") not in {"ok", "conflict", "skipped"} for item in items)
                or body["ok"] != all(item["status"] == "ok" for item in items)
                or any(
                    item["status"] == "conflict" and not isinstance(item.get("code"), str)
                    for item in items
                )
            ):
                raise ValueError("invalid_preview_response")
            return body

        if self.client is not None:
            return await send(self.client)
        async with httpx.AsyncClient() as client:
            return await send(client)


class PolicyEvidence:
    """默认本地全文检索；可注入 PolicyStore 使用真实混合检索。"""

    def __init__(self, index=None, directory: Path = POLICIES):
        chunks, docs = load_corpus(directory)
        self.index = index or OfflineIndex(chunks)
        self.docs = {doc.doc_id: doc for doc in docs}

    def retrieve(self, text: str, today: date) -> list[dict]:
        ranked = self.index.search(text, top_k=3, as_of=today)
        evidence = []
        rejected = False
        for hit in ranked.hits:
            if RuleInjectionDetector().check(normalize(hit.text)).blocked:
                rejected = True
                continue
            doc = self.docs.get(f"{hit.doc_key}@{hit.version}")
            if doc is None or doc.body[hit.char_start : hit.char_end] != hit.text:
                raise ValueError("unverifiable_citation")
            evidence.append(
                {
                    "chunk_id": hit.chunk_id,
                    "citation": hit.citation,
                    "quote": hit.text,
                    "source": hit.source,
                    "char_start": hit.char_start,
                    "char_end": hit.char_end,
                }
            )
        if rejected and not evidence:
            raise ValueError("retrieved_instruction_injection")
        return evidence


class PlanningState(TypedDict, total=False):
    status: str
    trace: list[str]
    masked_text: str
    intent: str
    facts: dict
    risk: list[str]
    questions: list[str]
    evidence: list[dict]
    plan: dict
    dag: str
    decisions: list[dict]
    preview: dict
    reason: str
    interpreter: str
    memory_context: dict


class Redactor:
    def __init__(self, text: str, public_sources=()):
        self.vault: PiiVault = mask(text).vault
        self.public_sources = frozenset(public_sources)

    def register(self, kind: str, value: str):
        self.vault.tokenize(kind, value)

    def clean(self, value):
        if isinstance(value, dict):
            return {key: self.clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            # Curated corpus filenames contain @version.md, which resembles an email.
            # Only exact names from the trusted loaded corpus are public metadata.
            if value in self.public_sources:
                return value
            for placeholder, raw in sorted(self.vault.entries.items(), key=lambda p: -len(p[1])):
                # 短账号名只能替换完整标识符，避免把 alice 中的 li 或 operation 破坏。
                pattern = re.escape(raw)
                if raw.isascii():
                    pattern = rf"(?<![A-Za-z0-9_]){pattern}(?![A-Za-z0-9_])"
                value = re.sub(pattern, lambda _, p=placeholder: p, value, flags=re.IGNORECASE)
            return mask(value, self.vault).text
        return value


def build_planning_graph(
    request: PlanningRequest,
    subject: PolicySubject,
    policy: PolicyClient,
    preview: PreviewClient,
    *,
    evidence: PolicyEvidence | None = None,
    interpreter: Interpreter | None = None,
    checkpointer=None,
    execution_sink=None,
):
    """每请求构图，敏感输入只留在闭包。不可跨用户复用该实例。"""
    interpreter = interpreter or RuleInterpreter()
    evidence = evidence or PolicyEvidence()
    redactor = Redactor(
        request.text,
        (doc.source for doc in evidence.docs.values())
        if isinstance(evidence, PolicyEvidence)
        else (),
    )
    working = {}

    def update(state, node, **fields):
        return {"trace": [*state.get("trace", []), node], **redactor.clean(fields)}

    async def authorize_action(action, resource="account", resource_id=None):
        decision = await policy.authorize(
            PolicyQuery(
                subject=subject,
                action=action,
                resource=PolicyResource(type=resource, id=resource_id),
            )
        )
        return {"action": action, **decision.model_dump()}

    async def understand(state):
        # 结构化补充也属于用户输入，不能借 facts 绕开注入检查。
        detector = RuleInjectionDetector()
        structured = request.facts.model_dump_json()
        if detector.check(normalize(structured)).blocked:
            return update(state, "understand", status="blocked", reason="unsafe_structured_input")
        guard = Guardrails().check(request.text, today=request.today)
        if guard.disposition == "blocked":
            return update(state, "understand", status="blocked", reason="input_injection")
        intent = guard.intent.intent
        if intent == "policy_query":
            working["query"] = guard.masked_text
            return update(
                state,
                "understand",
                status="understood",
                intent=intent,
                masked_text=guard.masked_text,
                risk=guard.risk,
            )
        preflight = {"provision_employee": "identity.create_user", "data_export": "data.export"}
        if intent in preflight:
            decision = await authorize_action(preflight[intent])
            if not decision["allow"]:
                return update(
                    state,
                    "understand",
                    status="denied",
                    intent=intent,
                    decisions=[decision],
                    reason=decision["reason"],
                )
        if intent != "provision_employee" or guard.intent.route != "route":
            return update(
                state,
                "understand",
                status="human_review",
                intent=intent,
                reason="unsupported_or_uncertain_intent",
            )
        try:
            facts = await interpreter.understand(request)
        except (ValueError, httpx.HTTPError):
            return update(
                state,
                "understand",
                status="clarify",
                reason="invalid_facts",
                questions=["无法可靠解析槽位，请用 facts 提供完整信息。"],
            )
        working["facts"] = facts
        for employee in facts.employees:
            redactor.register("person", employee.name)
            redactor.register("account", employee.username)
            if facts.email_domain:
                redactor.register("email", f"{employee.username}@{facts.email_domain}")
        questions = facts.questions()
        memory_context = getattr(interpreter, "context", None)
        return update(
            state,
            "understand",
            status="clarify" if questions else "understood",
            masked_text=guard.masked_text,
            intent=intent,
            risk=guard.risk,
            facts=facts.model_dump(mode="json"),
            questions=questions,
            interpreter=interpreter.name,
            **({"memory_context": memory_context} if memory_context is not None else {}),
        )

    def retrieve(state):
        queries = (
            [working["query"]]
            if "query" in working
            else [
                "入职账号邮箱开通申请",
                "实习生账号有效期权限分级",
                "批量开通权限审批",
            ]
        )
        found = {}
        try:
            for query in queries:
                for item in evidence.retrieve(query, request.today):
                    found[item["chunk_id"]] = item
        except (ValueError, OSError):
            return update(
                state, "retrieve", status="human_review", reason="unsafe_or_invalid_evidence"
            )
        if not found:
            return update(
                state, "retrieve", status="human_review", reason="policy_evidence_missing"
            )
        return update(
            state,
            "retrieve",
            status="evidence_only" if "query" in working else "retrieved",
            evidence=list(found.values()),
        )

    def plan(state):
        try:
            result = make_plan(working["facts"], [e["chunk_id"] for e in state["evidence"]])
        except (ValueError, ValidationError):
            return update(state, "plan", status="human_review", reason="invalid_plan")
        working["plan"] = result
        return update(
            state,
            "plan",
            status="planned",
            plan=result.model_dump(mode="json"),
            dag=result.mermaid(),
        )

    async def authorize(state):
        decisions = []
        for step in working["plan"].ordered_steps():
            actions = [step.operation]
            if step.arguments.get("permission") == "admin":
                actions.append("admin.grant")
            for action in actions:
                decision = await authorize_action(
                    action, step.operation.split(".")[0], step.arguments.get("repo_id")
                )
                decisions.append({"step_id": step.id, **decision})
                if not decision["allow"]:
                    return update(
                        state,
                        "authorize",
                        status="denied",
                        decisions=decisions,
                        reason=decision["reason"],
                    )
        return update(state, "authorize", status="authorized", decisions=decisions)

    async def dry_run(state):
        try:
            result = await preview.preview(working["plan"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return update(
                state,
                "dry_run",
                status="preview_unavailable",
                reason="preview_service_unavailable_or_invalid",
            )
        conflicts = [item for item in result["steps"] if item["status"] == "conflict"]
        if not result.get("ok") and not conflicts:
            return update(
                state, "dry_run", status="preview_unavailable", reason="incomplete_preview"
            )
        questions = [
            f"步骤 {item['step_id']} 预演冲突：{item['code']}。请核对账号、邮箱或资源配置。"
            for item in conflicts
        ]
        if not conflicts and execution_sink is not None:
            # Trusted server callback only; never put executable PII in graph state.
            execution_sink(working["plan"])
        return update(
            state,
            "dry_run",
            status="clarify" if conflicts else "awaiting_approval",
            preview=result,
            questions=questions,
        )

    builder = StateGraph(PlanningState)
    for name, node in (
        ("understand", understand),
        ("retrieve", retrieve),
        ("plan", plan),
        ("authorize", authorize),
        ("dry_run", dry_run),
    ):
        builder.add_node(name, observed_node(name, node))
    builder.add_edge(START, "understand")
    for source, expected, target in (
        ("understand", "understood", "retrieve"),
        ("retrieve", "retrieved", "plan"),
        ("plan", "planned", "authorize"),
        ("authorize", "authorized", "dry_run"),
    ):
        builder.add_conditional_edges(
            source, lambda s, e=expected, t=target: t if s["status"] == e else END
        )
    builder.add_edge("dry_run", END)
    return builder.compile(checkpointer=checkpointer)
