"""Real Postgres: durable decisions, concurrent submitters, recovery windows and isolation."""

import asyncio
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from core.graph.approval import ApprovalDecision
from core.graph.approval_service import ApprovalError, ApprovalService, digest
from core.graph.onboarding import PlanningRequest
from core.policy.opa_client import PolicyDecision, PolicySubject
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tests.test_planning import TEXT, TODAY, invoke

pytestmark = pytest.mark.integration


async def test_four_services_initialize_empty_checkpoint_schema_without_deadlock():
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required")
    schema = "w11_setup_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            scoped = make_conninfo(url, options="-csearch_path=" + schema)
            barrier = asyncio.Barrier(4)

            async def initialize():
                service = ApprovalService(scoped, ApprovalPolicy())
                await barrier.wait()
                async with service.session():
                    assert service.ready

            results = await asyncio.wait_for(
                asyncio.gather(*(initialize() for _ in range(4)), return_exceptions=True), 30
            )
            assert results == [None] * 4, results
        finally:
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


class ApprovalPolicy:
    denied = False

    async def authorize(self, query):
        return PolicyDecision(
            allow=not self.denied and query.subject.role == "admin", reason="test"
        )


@pytest_asyncio.fixture
async def approval_env(approval_connections):
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required")
    tenant = "approval-test-" + uuid4().hex
    policy = ApprovalPolicy()
    service = ApprovalService(url, policy, pool=approval_connections)
    owner = PolicySubject(id="requester", role="admin", tenant=tenant)
    reviewer = PolicySubject(id="reviewer", role="admin", tenant=tenant)
    snapshot, _, _, _ = await invoke()
    calls = []

    async def prepare():
        calls.append(1)
        return snapshot

    request = PlanningRequest(text=TEXT, today=TODAY)
    try:
        yield service, owner, reviewer, request, prepare, calls
    finally:
        with psycopg.connect(url) as conn:
            ids = conn.execute(
                "SELECT id FROM approval_requests WHERE tenant=%s", (tenant,)
            ).fetchall()
            for (request_id,) in ids:
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE thread_id=%s", ("approval:" + request_id,)
                    )
            conn.execute("DELETE FROM approval_requests WHERE tenant=%s", (tenant,))


def choice(row, decision="approve", **extra):
    return ApprovalDecision(
        decision=decision,
        reason="审核完成",
        plan_hash=row["plan_hash"],
        operation_id=str(uuid4()),
        **extra,
    )


async def create(env):
    service, owner, _, request, prepare, _ = env
    return await service.create(request, owner, str(uuid4()), prepare)


@pytest.mark.parametrize("decision,status", [("approve", "approved_ready"), ("reject", "rejected")])
async def test_new_service_resumes_checkpoint_and_idempotent_decision(
    approval_env, decision, status
):
    service, owner, reviewer, _, _, calls = approval_env
    row = await create(approval_env)
    assert row["interrupted"] and row["next"] == ["approval"]
    assert row["trace"] == ["review"]
    resumed_service = ApprovalService(service.url, service.policy)
    command = choice(row, decision)
    done = await resumed_service.decide(row["id"], command, reviewer)
    assert done["status"] == status and done["next"] == []
    assert done["trace"] == ["review", "approval:" + decision, "report"]
    assert (await resumed_service.decide(row["id"], command, reviewer))["decision"] == done[
        "decision"
    ]
    assert len(calls) == 1
    assert (await service.get(row["id"], owner))["status"] == status
    assert "张三" not in json.dumps(done, ensure_ascii=False, default=str)


async def test_creation_key_replay_skips_planning_and_mismatch_conflicts(approval_env):
    service, owner, _, request, prepare, calls = approval_env
    key = str(uuid4())
    first = await service.create(request, owner, key, prepare)
    second = await service.create(request, owner, key, prepare)
    assert first["id"] == second["id"] and len(calls) == 1
    with pytest.raises(ApprovalError, match="creation_key_payload_mismatch"):
        await service.create(PlanningRequest(text="changed"), owner, key, prepare)


async def test_tenant_isolation_self_review_and_role_denial(approval_env):
    service, owner, reviewer, *_ = approval_env
    row = await create(approval_env)
    other = reviewer.model_copy(update={"tenant": "elsewhere"})
    assert await service.list(other) == []
    with pytest.raises(ApprovalError, match="approval_not_found"):
        await service.get(row["id"], other)
    with pytest.raises(ApprovalError, match="approval_not_found"):
        await service.decide(row["id"], choice(row), other)
    with pytest.raises(ApprovalError, match="self_approval_forbidden"):
        await service.decide(row["id"], choice(row), owner)
    intern = reviewer.model_copy(update={"role": "intern"})
    with pytest.raises(ApprovalError, match="approval_policy_denied"):
        await service.decide(row["id"], choice(row), intern)
    assert await service.list(intern) == []
    assert (await service.get(row["id"], owner))["decision"] is None


async def test_expired_and_stale_plan_are_rejected(approval_env):
    service, _, reviewer, *_ = approval_env
    row = await create(approval_env)
    with pytest.raises(ApprovalError, match="approval_plan_changed"):
        await service.decide(
            row["id"], choice(row).model_copy(update={"plan_hash": "f" * 64}), reviewer
        )
    with psycopg.connect(service.url) as conn:
        conn.execute(
            "UPDATE approval_requests SET expires_at=%s WHERE id=%s",
            (datetime.now(UTC) - timedelta(seconds=1), row["id"]),
        )
    with pytest.raises(ApprovalError, match="approval_expired"):
        await service.decide(row["id"], choice(row), reviewer)


async def test_concurrent_opposing_decisions_have_one_winner(approval_env):
    service, _, reviewer, *_ = approval_env
    row = await create(approval_env)
    results = await asyncio.gather(
        service.decide(row["id"], choice(row), reviewer),
        service.decide(row["id"], choice(row, "reject"), reviewer),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    loser = next(result for result in results if isinstance(result, ApprovalError))
    assert loser.status == 409
    done = await service.get(row["id"], reviewer)
    assert len(done["trace"]) == 3 and not done["next"]


async def test_crash_after_decision_commit_before_resume_recovers(approval_env, monkeypatch):
    service, _, reviewer, *_ = approval_env
    row = await create(approval_env)

    async def crash(*args):
        raise RuntimeError("simulated_process_crash")

    monkeypatch.setattr(service, "drive", crash)
    command = choice(row)
    with pytest.raises(RuntimeError, match="simulated_process_crash"):
        await service.decide(row["id"], command, reviewer)
    new_service = ApprovalService(service.url, service.policy)
    pending = await new_service.get(row["id"], reviewer)
    assert pending["recovery_needed"] and pending["decision"]["decision"] == "approve"
    with pytest.raises(ApprovalError, match="approval_already_decided"):
        await new_service.decide(row["id"], choice(row, "reject"), reviewer)
    done = await new_service.recover(row["id"], reviewer)
    assert done["status"] == "approved_ready" and not done["recovery_needed"]


async def test_crash_before_first_checkpoint_recovers_from_snapshot(approval_env, monkeypatch):
    service, owner, reviewer, request, prepare, calls = approval_env

    async def crash(*args):
        raise RuntimeError("before_checkpoint")

    monkeypatch.setattr(service, "drive", crash)
    with pytest.raises(RuntimeError, match="before_checkpoint"):
        await service.create(request, owner, str(uuid4()), prepare)
    fresh = ApprovalService(service.url, service.policy)
    row = (await fresh.list(owner))[0]
    assert row["status"] == "submitted"
    recovered = await fresh.recover(row["id"], owner)
    assert recovered["interrupted"] and len(calls) == 1
    assert (await fresh.decide(row["id"], choice(recovered), reviewer))[
        "status"
    ] == "approved_ready"


async def test_crash_after_graph_completion_repairs_list_projection(approval_env, monkeypatch):
    service, _, reviewer, *_ = approval_env
    row = await create(approval_env)
    original = psycopg.AsyncConnection.execute

    async def fail_projection(conn, query, *args, **kwargs):
        if isinstance(query, str) and query.startswith("UPDATE approval_requests SET status"):
            raise RuntimeError("projection_crash")
        return await original(conn, query, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(psycopg.AsyncConnection, "execute", fail_projection)
        with pytest.raises(RuntimeError, match="projection_crash"):
            await service.decide(row["id"], choice(row), reviewer)
    fresh = ApprovalService(service.url, service.policy)
    current = await fresh.get(row["id"], reviewer)
    assert current["status"] == "approved_ready" and current["recovery_needed"]
    repaired = await fresh.recover(row["id"], reviewer)
    assert not repaired["recovery_needed"] and len(repaired["trace"]) == 3
    assert (await fresh.list(reviewer))[0]["status"] == "approved_ready"


async def test_policy_revocation_fails_closed_and_snapshot_integrity(approval_env):
    service, owner, reviewer, *_ = approval_env
    row = await create(approval_env)
    service.policy.denied = True
    with pytest.raises(ApprovalError, match="approval_policy_denied"):
        await service.decide(row["id"], choice(row), reviewer)
    service.policy.denied = False
    with psycopg.connect(service.url) as conn:
        conn.execute("UPDATE approval_requests SET snapshot='{}'::jsonb WHERE id=%s", (row["id"],))
    with pytest.raises(ApprovalError, match="approval_plan_changed"):
        await service.recover(row["id"], owner)
    assert digest({}) != row["plan_hash"]


async def test_killed_process_resumes_in_fresh_python_process(approval_env):
    _, owner, _, *_ = approval_env
    await asyncio.to_thread(check_killed_process, owner.tenant)


def check_killed_process(tenant):
    command = [sys.executable, "-m", "tests.approval_worker"]
    proc = subprocess.Popen(
        [*command, "start", "--tenant", tenant, "--key", str(uuid4())],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        line = pool.submit(proc.stdout.readline).result(timeout=30)
        assert line, "worker failed to persist interrupt"
        first = json.loads(line)
        assert first["interrupted"] and first["next"] == ["approval"]
        proc.kill()
        proc.wait(timeout=10)
        result = subprocess.run(
            [*command, "resume", "--tenant", tenant, "--request-id", first["id"]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=True,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        resumed = json.loads(result.stdout)
        assert resumed["before"]["checkpoint_id"] == first["checkpoint_id"]
        assert resumed["after"]["status"] == "approved_ready"
        assert resumed["after"]["trace"] == ["review", "approval:approve", "report"]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        pool.shutdown(wait=True)
        proc.stdout.close()
        proc.stderr.close()
