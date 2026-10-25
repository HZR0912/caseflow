"""Real application services, PostgreSQL receipts and HTTP OPA; no model calls."""

import asyncio
from collections import Counter
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
from apps.mockcorp.models import MockSettings
from apps.mockcorp.preview import preview_plan
from apps.mockcorp.service import MockCorp
from apps.mockcorp.store import PostgresStore
from core.graph.approval import ApprovalDecision
from core.graph.approval_service import ApprovalError, ApprovalService
from core.graph.onboarding import PlanningRequest
from core.graph.workflow import PolicyEvidence, build_planning_graph
from core.policy.opa_client import PolicyClient, PolicyQuery, PolicyResource, PolicySubject
from core.tools.execution import ExecutionService, RetryPolicy
from core.tools.gateway import classify
from core.tools.payload import PlanCipher, PreparedApproval
from cryptography.fernet import Fernet
from eval.scenarios import COUNTS

CORPUS = Path(__file__).resolve().parents[1] / "data/policies"


class RestartBoundary(Exception):
    """Injected after remote commit, before the executor can save its receipt."""


class RecordingPolicy:
    def __init__(self, client):
        self.client, self.decisions = client, []

    async def authorize(self, query):
        result = await self.client.authorize(query)
        self.decisions.append(result.model_dump())
        return result


class LocalPreview:
    def __init__(self, store, tenant):
        self.store, self.tenant = store, tenant

    async def preview(self, plan):
        return (await asyncio.to_thread(preview_plan, self.store, self.tenant, "eval", plan)).body


class SupplierGateway:
    def __init__(self, supplier, tenant, scenario):
        self.supplier, self.tenant = supplier, tenant
        self.faults = {fault.operation: list(fault.sequence) for fault in scenario.faults}
        self.restart_pending = scenario.restart
        self.calls = []

    async def call(self, step, arguments, key):
        sequence = self.faults.get(step.operation, [])
        fault = sequence.pop(0) if sequence else None
        self.calls.append({"operation": step.operation, "key": key, "fault": fault})
        reply = await asyncio.to_thread(
            self.supplier.execute, self.tenant, step.operation, arguments, key=key, fault=fault
        )
        if self.restart_pending and 200 <= reply.status < 300:
            self.restart_pending = False
            raise RestartBoundary()
        return classify(
            httpx.Response(reply.status, json=reply.body, headers=reply.headers),
            step.operation,
            arguments,
        )

    async def compensate(self, operation, source_key, key):
        self.calls.append({"operation": "saga.compensate", "key": key, "fault": None})
        reply = await asyncio.to_thread(
            self.supplier.compensate, self.tenant, source_key, operation, key=key
        )
        return classify(
            httpx.Response(reply.status, json=reply.body, headers=reply.headers), "saga.compensate"
        )


def verify_citations(evidence, terms, corpus):
    """Check every quote against source offsets, plus explicit topical expectations."""
    if not evidence:
        return False
    from core.rag.chunking import load_corpus

    chunks, _ = load_corpus(corpus)
    known = {chunk.chunk_id: chunk for chunk in chunks}
    for item in evidence:
        source = corpus / item.get("source", "")
        if not source.is_file() or corpus.resolve() not in source.resolve().parents:
            return False
        chunk = known.get(item.get("chunk_id"))
        if (
            chunk is None
            or item.get("citation") != f"{chunk.title}（{chunk.version}）· {chunk.section_path}"
            or chunk.source != item.get("source")
            or chunk.char_start != item.get("char_start")
            or chunk.char_end != item.get("char_end")
            or chunk.text != item.get("quote")
        ):
            return False
    text = "\n".join(item["quote"] for item in evidence)
    return all(term in text for term in terms)


async def run_case(
    case, repeat, url, opa_url, *, policy_client=None, supplier_pool=None, approval_pool=None
):
    started = perf_counter()
    settings = MockSettings(
        _env_file=None, faults_enabled=True, fault_delay_s=0, code_seat_limit=100
    )
    store = PostgresStore(url, settings, pool=supplier_pool)
    supplier = MockCorp(store, settings)
    tenant = "w11-" + uuid4().hex
    client = policy_client or PolicyClient(opa_url)
    policy = RecordingPolicy(client)
    gateway = SupplierGateway(supplier, tenant, case)
    row = dict(
        scenario_id=case.id,
        repeat=repeat,
        status="error",
        errors=[],
        latency_ms=0.0,
        tokens=0,
        cost_usd=0.0,
        steps=0,
        calls=[],
        supplier_counts={},
        committed_effects=0,
        duplicate_effects=0,
        no_business_writes=False,
        citation_ok=None,
        evidence=[],
        compensations=[],
        replay_ok=None,
        restart_ok=None,
        policy_healthy=True,
        policy_degraded_reasons=[],
        passed=False,
    )
    snapshot, result, evidence = {}, {}, []
    try:

        def seed(operation, payload, key):
            reply = supplier.execute(tenant, operation, payload, key=key)
            if reply.status >= 400:
                raise RuntimeError("fixture_seed_failed")
            return reply.body["result"]["id"]

        repo = await asyncio.to_thread(seed, "code.create_repo", {"name": "eval"}, "seed-repo")
        project = await asyncio.to_thread(
            seed, "ticket.create_project", {"key": "EVAL", "name": "eval"}, "seed-project"
        )
        before = await asyncio.to_thread(store.snapshot, tenant)
        baseline_logs = await asyncio.to_thread(store.oplog, tenant, 0, 1000)
        after_id = max((item["id"] for item in baseline_logs["items"]), default=0)
        facts = (
            dict(
                employees=[
                    {"name": f"测试员工{i}", "username": f"evaluser{i}"}
                    for i in range(case.employees)
                ],
                role="developer",
                start_date="2026-10-26",
                end_date="2026-12-31",
                email_domain="example.com",
                repo_id=repo,
                project_id=project,
                permission="read",
            )
            if case.complete_facts
            else {}
        )
        facts.update(case.facts)
        for key in case.omit_facts:
            facts.pop(key, None)
        owner = PolicySubject(id="eval-owner", role=case.role, tenant=tenant)
        if case.kind == "policy":
            decision = await policy.authorize(
                PolicyQuery(subject=owner, action=case.action, resource=PolicyResource(type="eval"))
            )
            row["status"] = "allowed" if decision.allow else "denied"
        else:
            request = PlanningRequest(text=case.text, today=case.today, facts=facts)
            plans = []
            snapshot = await build_planning_graph(
                request,
                owner,
                policy,
                LocalPreview(store, tenant),
                evidence=PolicyEvidence(),
                execution_sink=plans.append,
            ).ainvoke({})
            row["status"] = snapshot["status"]
            evidence = snapshot.get("evidence", [])
            if case.kind == "task" and row["status"] == "awaiting_approval":
                cipher = PlanCipher(Fernet.generate_key().decode())
                approvals = ApprovalService(url, policy, cipher=cipher, pool=approval_pool)

                async def prepare():
                    return PreparedApproval(snapshot, plans[0])

                approval = await approvals.create(request, owner, uuid4().hex, prepare)
                executor = ExecutionService(
                    approvals, gateway, RetryPolicy(base_delay=0, max_delay=0)
                )
                if case.approval == "none":
                    try:
                        await executor.execute(approval["id"], owner)
                    except ApprovalError as exc:
                        if exc.detail != "execution_requires_approval":
                            raise
                        row["status"] = "approval_required"
                    else:
                        row["errors"].append("unapproved_execution_was_allowed")
                else:
                    reviewer = PolicySubject(id="eval-reviewer", role="admin", tenant=tenant)
                    await approvals.decide(
                        approval["id"],
                        ApprovalDecision(
                            decision=case.approval,
                            reason="evaluation",
                            plan_hash=approval["plan_hash"],
                            operation_id=str(uuid4()),
                        ),
                        reviewer,
                    )
                    if case.approval == "reject":
                        try:
                            await executor.execute(approval["id"], owner)
                        except ApprovalError as exc:
                            if exc.detail != "execution_requires_approval":
                                raise
                            row["status"] = "rejected"
                        else:
                            row["errors"].append("rejected_execution_was_allowed")
                    else:
                        try:
                            result = await executor.execute(approval["id"], owner)
                        except RestartBoundary:
                            # Fresh service and saver connection recover only from durable state.
                            fresh = ApprovalService(url, policy, cipher=cipher)
                            result = await ExecutionService(fresh, gateway, executor.retry).execute(
                                approval["id"], owner
                            )
                            row["restart_ok"] = result["status"] == "succeeded"
                        row["status"] = result["status"]
                        if case.replay:
                            pre = await asyncio.to_thread(store.snapshot, tenant)
                            calls = len(gateway.calls)
                            again = await executor.execute(approval["id"], owner)
                            post = await asyncio.to_thread(store.snapshot, tenant)
                            row["replay_ok"] = (
                                pre == post and again == result and calls == len(gateway.calls)
                            )
        after = await asyncio.to_thread(store.snapshot, tenant)
        logs = (await asyncio.to_thread(store.oplog, tenant, after_id, 1000))["items"]
        if len(logs) >= 1000:
            row["errors"].append("supplier_log_truncated")
        committed = [
            item for item in logs if item["outcome"] == "committed" and not item["replayed"]
        ]
        keys = Counter(item["idempotency_key"] for item in committed)
        row.update(
            calls=gateway.calls,
            no_business_writes=before == after and not committed,
            committed_effects=len(committed),
            duplicate_effects=sum(max(0, n - 1) for n in keys.values()),
            supplier_counts={
                name: sum(item.get("active", True) for item in after[name].values())
                for name in COUNTS
            },
            balance_cents=after["balance_cents"],
            compensations=[
                item["step_id"]
                for item in result.get("saga", {}).get("steps", [])
                if item["status"] == "succeeded"
            ],
            supplier_log=[
                {key: item[key] for key in ("operation", "outcome", "replayed", "idempotency_key")}
                for item in logs
            ],
            steps=len(snapshot.get("trace", [])) + len(gateway.calls),
            policy_healthy=not any(item["degraded"] for item in policy.decisions),
        )
        expect = case.expect
        row["evidence"] = evidence
        if expect.citation_required:
            row["citation_ok"] = await asyncio.to_thread(
                verify_citations, evidence, expect.citation_terms, CORPUS
            )
        checks = {
            "final_state": row["status"] == expect.final_state,
            "required_calls": set(expect.must_call)
            <= {item["operation"] for item in gateway.calls},
            "forbidden_calls": not set(expect.must_not_call)
            & {item["operation"] for item in gateway.calls},
            "state_counts": all(
                row["supplier_counts"][key] == value for key, value in expect.state_counts.items()
            ),
            "balance": expect.balance_cents is None or row["balance_cents"] == expect.balance_cents,
            "no_business_writes": not expect.no_business_writes or row["no_business_writes"],
            "compensations": row["compensations"] == expect.compensations,
            "citations": not expect.citation_required or row["citation_ok"] is True,
            "step_budget": row["steps"] <= expect.max_steps,
            "token_budget": row["tokens"] <= expect.max_tokens,
            "zero_duplicate_effects": row["duplicate_effects"] == 0,
            "policy_healthy": row["policy_healthy"],
            "replay": not case.replay or row["replay_ok"] is True,
            "restart": not case.restart or row["restart_ok"] is True,
            "faults_consumed": not any(gateway.faults.values()),
        }
        row["errors"].extend(key for key, ok in checks.items() if not ok)
    except Exception as exc:
        # No upstream message or raw input: errors can contain credentials/PII.
        row["errors"].append("exception:" + type(exc).__name__)
        # Whitelist diagnostic categories; never emit connection strings or SQL text.
        detail = str(exc.__cause__ or exc).lower()
        for signature in (
            "address already in use",
            "ssl negotiation",
            "connection reset",
            "server closed",
            "too many clients",
            "cannot assign requested address",
            "connection refused",
            "timeout expired",
            "connection timeout",
            "connection failed",
            "connection is closed",
        ):
            if signature in detail:
                row["errors"].append("storage:" + signature.replace(" ", "_"))
                break
        if isinstance(exc, ApprovalError):
            row["errors"].append(exc.detail)
            if getattr(exc.__cause__, "sqlstate", None):
                row["errors"].append("sqlstate:" + exc.__cause__.sqlstate)
    finally:
        row["policy_degraded_reasons"] = [
            item["reason"] for item in policy.decisions if item["degraded"]
        ]
        row["policy_healthy"] = not row["policy_degraded_reasons"]
        if policy_client is None:
            await client.aclose()
    row["latency_ms"] = round((perf_counter() - started) * 1000, 3)
    row["passed"] = not row["errors"]
    return row
