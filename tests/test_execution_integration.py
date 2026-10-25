"""Real DB journal + real supplier transactions; HTTP/crash E2E lives in W7 demo."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
from apps.mockcorp.models import MockSettings
from apps.mockcorp.service import MockCorp
from apps.mockcorp.store import PostgresStore
from core.graph.approval_service import ApprovalError, ApprovalService
from core.graph.onboarding import PlanningRequest
from core.graph.workflow import build_planning_graph
from core.policy.opa_client import PolicySubject
from core.tools.execution import ExecutionService, RetryPolicy
from core.tools.gateway import ToolResult, classify
from core.tools.payload import PlanCipher, PreparedApproval
from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb

from tests.test_approval_integration import ApprovalPolicy, choice
from tests.test_planning import TEXT, TODAY, LocalPreview

pytestmark = pytest.mark.integration


class SupplierGateway:
    def __init__(self, supplier, tenant, faults=None):
        self.supplier, self.tenant = supplier, tenant
        self.faults = list(faults or [])
        self.calls = []

    async def call(self, step, arguments, key):
        self.calls.append(key)
        fault = self.faults.pop(0) if self.faults else None
        reply = await asyncio.to_thread(
            self.supplier.execute, self.tenant, step.operation, arguments, key=key, fault=fault
        )
        return classify(
            httpx.Response(reply.status, json=reply.body, headers=reply.headers),
            step.operation,
            arguments,
        )

    async def compensate(self, operation, source_key, key):
        reply = await asyncio.to_thread(
            self.supplier.compensate, self.tenant, source_key, operation, key=key
        )
        return classify(
            httpx.Response(reply.status, json=reply.body, headers=reply.headers), "saga.compensate"
        )


@pytest_asyncio.fixture
async def execution_env(supplier_connections, approval_connections):
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required")
    tenant = "execution-test-" + uuid4().hex
    settings = MockSettings(_env_file=None, faults_enabled=True, fault_delay_s=0)
    store = PostgresStore(url, settings, pool=supplier_connections)
    supplier = MockCorp(store, settings)
    repo = supplier.execute(tenant, "code.create_repo", {"name": "frontend"}, key="seed-repo").body[
        "result"
    ]["id"]
    project = supplier.execute(
        tenant, "ticket.create_project", {"key": "ONBOARD", "name": "Onboard"}, key="seed-project"
    ).body["result"]["id"]
    service = ApprovalService(
        url,
        ApprovalPolicy(),
        cipher=PlanCipher(Fernet.generate_key().decode()),
        pool=approval_connections,
    )
    owner = PolicySubject(id="owner", role="admin", tenant=tenant)
    reviewer = owner.model_copy(update={"id": "reviewer"})
    request = PlanningRequest(
        text=TEXT.replace("r_frontend", repo).replace("p_onboard", project), today=TODAY
    )
    captured = []
    graph = build_planning_graph(
        request, owner, service.policy, LocalPreview(store, tenant), execution_sink=captured.append
    )

    async def prepare():
        snapshot = await graph.ainvoke({"trace": []})
        return PreparedApproval(snapshot, captured[0])

    row = await service.create(request, owner, str(uuid4()), prepare)
    try:
        yield service, owner, reviewer, row, store, supplier
    finally:
        from scripts.w6_approval_demo import cleanup

        cleanup(store.url, tenant)


async def approved(env, faults=None):
    service, owner, reviewer, row, _store, supplier = env
    await service.decide(row["id"], choice(row), reviewer)
    gateway = SupplierGateway(supplier, owner.tenant, faults)
    executor = ExecutionService(service, gateway, RetryPolicy(base_delay=0, max_delay=0))
    return executor, gateway


async def test_429_retry_loss_after_commit_replay_and_no_pii(execution_env):
    service, owner, _, row, store, _ = execution_env
    executor, gateway = await approved(execution_env, ["429", "timeout_after_commit"])
    result = await executor.execute(row["id"], owner)
    assert result["status"] == "succeeded"
    assert len(result["steps"]) == 12 and result["steps"][0]["attempts"] == 3
    assert result["steps"][0]["events"][-1]["replayed"]
    assert len(set(gateway.calls[:3])) == 1
    assert len(store.snapshot(owner.tenant)["users"]) == 3
    before = len(gateway.calls)
    assert await executor.execute(row["id"], owner) == result
    assert len(gateway.calls) == before
    view = await service.get(row["id"], owner)
    assert view["execution"] == result and view["executable"]
    assert (await service.list(owner))[0]["execution"]["status"] == "succeeded"
    assert "张三" not in json.dumps(view, ensure_ascii=False, default=str)
    assert "zhangsan" not in json.dumps(view, ensure_ascii=False, default=str)
    with psycopg.connect(service.url) as conn:
        payload = conn.execute(
            "SELECT execution_payload FROM approval_requests WHERE id=%s", (row["id"],)
        ).fetchone()[0]
        assert "zhangsan" not in payload


async def test_no_execution_before_approval_and_tenant_role_gates(execution_env):
    service, owner, _, row, store, supplier = execution_env
    executor = ExecutionService(service, SupplierGateway(supplier, owner.tenant))
    with pytest.raises(ApprovalError, match="execution_requires_approval"):
        await executor.execute(row["id"], owner)
    await approved(execution_env)
    with pytest.raises(ApprovalError, match="approval_not_found"):
        await executor.execute(row["id"], owner.model_copy(update={"tenant": "other"}))
    with pytest.raises(ApprovalError, match="approval_policy_denied"):
        await executor.execute(row["id"], owner.model_copy(update={"role": "intern"}))
    assert not store.snapshot(owner.tenant)["users"]


async def test_business_failure_stops_preserves_completed_steps(execution_env):
    _, owner, _, row, store, _ = execution_env
    # Topological order starts with the three users, followed by mailboxes.
    executor, _ = await approved(execution_env, [None, None, None, "domain_policy"])
    result = await executor.execute(row["id"], owner)
    assert result["status"] == "partial_success"
    assert len(result["completed_steps"]) == 3
    assert result["compensation_executed"] is True
    assert result["summary"]["compensated"] == ["p1_user"]
    assert result["steps"][3]["attempts"] == 1
    assert all(s["attempts"] == 0 for s in result["steps"][4:])
    assert len(store.snapshot(owner.tenant)["users"]) == 3
    assert not store.snapshot(owner.tenant)["mailboxes"]


async def test_restart_recovers_remote_commit_before_local_save(execution_env):
    service, owner, _, row, store, _ = execution_env
    executor, gateway = await approved(execution_env)
    original = gateway.call

    async def crash(*args):
        await original(*args)
        raise RuntimeError("crash_after_remote_commit")

    gateway.call = crash
    with pytest.raises(RuntimeError, match="crash_after_remote_commit"):
        await executor.execute(row["id"], owner)
    assert len(store.snapshot(owner.tenant)["users"]) == 1
    gateway.call = original
    fresh = ApprovalService(service.url, service.policy, cipher=service.cipher)
    result = await ExecutionService(fresh, gateway).execute(row["id"], owner)
    assert result["status"] == "succeeded"
    assert result["steps"][0]["events"][1]["replayed"]
    assert len(store.snapshot(owner.tenant)["users"]) == 3


async def test_concurrent_execution_has_single_winner(execution_env):
    _, owner, _, row, store, _ = execution_env
    executor, gateway = await approved(execution_env)
    entered, release = asyncio.Event(), asyncio.Event()
    original = gateway.call

    async def paused(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    gateway.call = paused
    first = asyncio.create_task(executor.execute(row["id"], owner))
    await asyncio.wait_for(entered.wait(), 10)
    try:
        with pytest.raises(ApprovalError, match="approval_busy_retry"):
            await executor.execute(row["id"], owner)
    finally:
        release.set()
    assert (await first)["status"] == "succeeded"
    assert len(store.snapshot(owner.tenant)["users"]) == 3


async def test_retry_budget_persists_and_terminal_replay_never_retries(execution_env):
    service, owner, _, row, _, _ = execution_env
    executor, gateway = await approved(execution_env, ["500"] * 5)
    result = await executor.execute(row["id"], owner)
    assert result["status"] == "retry_exhausted"
    assert result["steps"][0]["attempts"] == 4 and len(gateway.calls) == 4
    assert result["steps"][0]["outcome_unknown"]
    fresh = ExecutionService(
        ApprovalService(service.url, service.policy, cipher=service.cipher), gateway
    )
    assert await fresh.execute(row["id"], owner) == result
    assert len(gateway.calls) == 4


async def test_retry_after_is_persisted_without_early_retries(execution_env):
    service, owner, _, row, _, _ = execution_env
    executor, gateway = await approved(execution_env)

    async def limited(*args):
        return ToolResult("retryable", "http_429", retry_after=60)

    gateway.call = limited
    first = await executor.execute(row["id"], owner)
    assert first["status"] == "retry_waiting"
    assert first["steps"][0]["attempts"] == 1
    fresh = ExecutionService(service, gateway)
    second = await fresh.execute(row["id"], owner)
    assert second == first
    assert datetime.fromisoformat(first["steps"][0]["next_attempt_at"]) > datetime.now(UTC)


@pytest.mark.parametrize(
    "change,error",
    [
        ("expired", "execution_approval_expired"),
        ("legacy", "legacy_approval_resubmit_required"),
        ("cipher", "execution_payload_invalid"),
        ("snapshot", "approval_plan_changed"),
    ],
)
async def test_expiry_integrity_and_legacy_fail_closed(execution_env, change, error):
    service, owner, _, row, store, _ = execution_env
    executor, _ = await approved(execution_env)
    with psycopg.connect(service.url) as conn:
        if change == "expired":
            conn.execute(
                "UPDATE approval_requests SET expires_at=%s WHERE id=%s",
                (datetime.now(UTC) - timedelta(seconds=1), row["id"]),
            )
        elif change == "legacy":
            conn.execute(
                "UPDATE approval_requests SET execution_payload=NULL WHERE id=%s", (row["id"],)
            )
        elif change == "cipher":
            conn.execute(
                "UPDATE approval_requests SET execution_payload='tampered' WHERE id=%s",
                (row["id"],),
            )
        else:
            conn.execute(
                "UPDATE approval_requests SET snapshot=%s WHERE id=%s", (Jsonb({}), row["id"])
            )
    with pytest.raises(ApprovalError, match=error):
        await executor.execute(row["id"], owner)
    assert not store.snapshot(owner.tenant)["users"]


async def test_policy_rechecked_on_retry(execution_env):
    service, owner, _, row, store, _ = execution_env
    executor, gateway = await approved(execution_env)
    original = service.policy.authorize

    async def first_failure(*args):
        async def denied(query):
            decision = await original(query)
            return decision.model_copy(update={"allow": query.action.startswith("approval.")})

        service.policy.authorize = denied
        return ToolResult("retryable", "http_500")

    gateway.call = first_failure
    result = await executor.execute(row["id"], owner)
    assert result["status"] == "failed"
    assert result["forward_status"] == "human_required"
    assert result["steps"][0]["attempts"] == 1
    assert result["steps"][0]["code"] == "execution_policy_denied"
    assert not store.snapshot(owner.tenant)["users"]


async def test_rejected_approval_cannot_execute(execution_env):
    service, owner, reviewer, row, store, supplier = execution_env
    await service.decide(row["id"], choice(row, "reject"), reviewer)
    executor = ExecutionService(service, SupplierGateway(supplier, owner.tenant))
    with pytest.raises(ApprovalError, match="execution_requires_approval"):
        await executor.execute(row["id"], owner)
    assert not store.snapshot(owner.tenant)["users"]


async def test_unknown_commit_is_not_cleared_by_later_auth_error(execution_env):
    _, owner, _, row, _, _ = execution_env
    executor, gateway = await approved(execution_env)
    responses = iter(
        [
            ToolResult("retryable", "transport_error", outcome_unknown=True),
            ToolResult("human_required", "http_401"),
        ]
    )

    async def unknown_then_denied(*args):
        return next(responses)

    gateway.call = unknown_then_denied
    result = await executor.execute(row["id"], owner)
    assert result["status"] == "human_required"
    assert result["steps"][0]["attempts"] == 2
    assert result["steps"][0]["outcome_unknown"] is True
