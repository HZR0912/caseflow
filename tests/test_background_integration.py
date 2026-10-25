"""Durable consent, crash windows, real PostgreSQL exclusion and real Redis redelivery."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from apps.api.settings import Settings
from arq import Worker
from arq.connections import RedisSettings, create_pool
from core.graph.approval_service import ApprovalError, ApprovalService
from core.jobs.service import BackgroundJobs
from core.jobs.worker import close_worker, dispatch_due, resume_execution, serialize
from core.policy.opa_client import PolicyDecision
from core.tools.execution import ExecutionService, RetryPolicy
from core.tools.gateway import ToolResult
from core.tools.saga import Saga
from psycopg.types.json import Jsonb

from tests.test_approval_integration import choice
from tests.test_execution_integration import SupplierGateway

pytest_plugins = ("tests.test_execution_integration",)
pytestmark = pytest.mark.integration
TOKEN = "background-test-token-never-real"
AUTHORIZATION = "Bearer " + TOKEN


def settings_for(subject, token=TOKEN):
    return Settings(
        _env_file=None,
        auth_credentials=[
            {
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "user_id": subject.id,
                "tenant": subject.tenant,
                "role": subject.role,
            }
        ],
    )


async def setup_jobs(env, faults=None):
    service, owner, reviewer, row, _, supplier = env
    await service.decide(row["id"], choice(row), reviewer)
    gateway = SupplierGateway(supplier, owner.tenant, faults)
    holder = [settings_for(owner)]
    jobs = BackgroundJobs(
        service,
        lambda: holder[0],
        gateway_factory=lambda *_: gateway,
        retry=RetryPolicy(base_delay=0, max_delay=0, max_inline_delay=0),
    )
    return jobs, gateway, holder


async def job_row(jobs, request_id):
    async with jobs.approvals.connection() as conn:
        return await (
            await conn.execute(
                "SELECT * FROM background_jobs WHERE request_id=%s",
                (request_id,),
            )
        ).fetchone()


async def make_due(jobs, request_id, *, journal=True):
    async with jobs.approvals.connection() as conn:
        await conn.execute(
            "UPDATE background_jobs SET due_at=now() WHERE request_id=%s", (request_id,)
        )
        if journal:
            saved = await (
                await conn.execute(
                    "SELECT journal FROM execution_runs WHERE request_id=%s",
                    (request_id,),
                )
            ).fetchone()
            if saved:
                value = saved["journal"]
                for item in value["steps"] + value.get("saga", {}).get("steps", []):
                    if item.get("next_attempt_at"):
                        item["next_attempt_at"] = (
                            datetime.now(UTC) - timedelta(seconds=1)
                        ).isoformat()
                await conn.execute(
                    "UPDATE execution_runs SET journal=%s WHERE request_id=%s",
                    (Jsonb(value), request_id),
                )


def wait_once(gateway, *, compensation=False):
    name = "compensate" if compensation else "call"
    original = getattr(gateway, name)
    calls = []

    async def waiting(*args):
        calls.append(args[-1])
        if len(calls) == 1:
            return ToolResult("retryable", "rate_limited", retry_after=60)
        return await original(*args)

    setattr(gateway, name, waiting)
    return calls


async def test_unapproved_or_wrong_subject_never_creates_background_intent(execution_env):
    service, owner, _, row, store, supplier = execution_env
    jobs = BackgroundJobs(
        service,
        lambda: settings_for(owner),
        gateway_factory=lambda *_: SupplierGateway(supplier, owner.tenant),
    )
    with pytest.raises(ApprovalError, match="execution_requires_approval"):
        await jobs.execute(row["id"], owner, AUTHORIZATION)
    assert await job_row(jobs, row["id"]) is None
    assert row["id"] not in await jobs.due()
    assert not store.snapshot(owner.tenant)["users"]
    with pytest.raises(ApprovalError, match="background_identity_changed"):
        await jobs.execute(
            row["id"], owner.model_copy(update={"tenant": "elsewhere"}), AUTHORIZATION
        )
    assert await job_row(jobs, row["id"]) is None


async def test_waiting_due_time_terminal_cleanup_and_tenant_visibility(execution_env):
    _, owner, _, row, store, _ = execution_env
    jobs, gateway, _ = await setup_jobs(execution_env)
    calls = wait_once(gateway)
    result = await jobs.execute(row["id"], owner, AUTHORIZATION)
    assert result["status"] == "retry_waiting"
    job = await job_row(jobs, row["id"])
    assert job["status"] == "waiting" and TOKEN not in job["credential"]
    assert row["id"] not in await jobs.due()
    await jobs.run(row["id"])
    assert len(calls) == 1
    with pytest.raises(ApprovalError, match="approval_not_found"):
        await jobs.get(row["id"], owner.model_copy(update={"tenant": "elsewhere"}))
    public = await jobs.get(row["id"], owner)
    assert "credential" not in public["job"] and TOKEN not in json.dumps(public, default=str)
    await make_due(jobs, row["id"])
    await jobs.run(row["id"])
    result = (await jobs.approvals.get(row["id"], owner))["execution"]
    assert result["status"] == "succeeded" and result["steps"][0]["attempts"] == 2
    assert calls[0] == calls[1]
    assert len(store.snapshot(owner.tenant)["users"]) == 3
    job = await job_row(jobs, row["id"])
    assert job["status"] == "completed" and job["credential"] is None
    before = len(calls)
    await jobs.run(row["id"])
    await jobs.execute(row["id"], owner, AUTHORIZATION)
    assert len(calls) == before


async def test_crash_before_first_journal_survives_as_durable_consent(execution_env, monkeypatch):
    _, owner, _, row, store, _ = execution_env
    jobs, _, _ = await setup_jobs(execution_env)
    original = ExecutionService.run_prepared

    async def crash(*_):
        raise RuntimeError("simulated_process_loss")

    monkeypatch.setattr(ExecutionService, "run_prepared", crash)
    with pytest.raises(RuntimeError, match="simulated_process_loss"):
        await jobs.execute(row["id"], owner, AUTHORIZATION)
    assert (await job_row(jobs, row["id"]))["status"] == "queued"
    assert (await jobs.approvals.get(row["id"], owner))["execution"] is None
    monkeypatch.setattr(ExecutionService, "run_prepared", original)
    await jobs.run(row["id"])
    assert (await job_row(jobs, row["id"]))["status"] == "completed"
    assert len(store.snapshot(owner.tenant)["users"]) == 3


async def test_cancel_after_remote_commit_fresh_service_replays_same_key(execution_env):
    service, owner, _, row, store, _ = execution_env
    jobs, gateway, holder = await setup_jobs(execution_env)
    original = gateway.call
    committed = asyncio.Event()

    async def lose_response(*args):
        await original(*args)
        committed.set()
        await asyncio.Future()

    gateway.call = lose_response
    task = asyncio.create_task(jobs.execute(row["id"], owner, AUTHORIZATION))
    await asyncio.wait_for(committed.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(store.snapshot(owner.tenant)["users"]) == 1
    gateway.call = original
    fresh = ApprovalService(service.url, service.policy, cipher=service.cipher, pool=service.pool)
    recovered = BackgroundJobs(fresh, lambda: holder[0], gateway_factory=lambda *_: gateway)
    await recovered.run(row["id"])
    result = (await fresh.get(row["id"], owner))["execution"]
    assert result["status"] == "succeeded"
    assert result["steps"][0]["events"][-1]["replayed"]
    assert gateway.calls[0] == gateway.calls[1]
    assert len(store.snapshot(owner.tenant)["users"]) == 3


async def test_duplicate_workers_and_manual_execute_share_one_lock(execution_env):
    _, owner, _, row, store, _ = execution_env
    jobs, gateway, _ = await setup_jobs(execution_env)
    wait_once(gateway)
    await jobs.execute(row["id"], owner, AUTHORIZATION)
    await make_due(jobs, row["id"])
    original = gateway.call
    entered, release = asyncio.Event(), asyncio.Event()

    async def paused(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    gateway.call = paused
    task = asyncio.create_task(jobs.run(row["id"]))
    await asyncio.wait_for(entered.wait(), 10)
    await asyncio.gather(*(jobs.run(row["id"]) for _ in range(4)))
    with pytest.raises(ApprovalError, match="approval_busy_retry"):
        await jobs.execute(row["id"], owner, AUTHORIZATION)
    release.set()
    await task
    assert len(store.snapshot(owner.tenant)["users"]) == 3
    assert (await job_row(jobs, row["id"]))["runs"] == 1


@pytest.mark.parametrize("change", ["revoked", "role", "tenant", "actor", "ciphertext", "expired"])
async def test_stale_delegation_blocks_without_consuming_business_attempt(execution_env, change):
    _, owner, _, row, store, _ = execution_env
    jobs, gateway, holder = await setup_jobs(execution_env)
    wait_once(gateway)
    await jobs.execute(row["id"], owner, AUTHORIZATION)
    if change == "revoked":
        holder[0] = Settings(_env_file=None)
    elif change in {"role", "tenant", "actor"}:
        field = "user_id" if change == "actor" else change
        credential = holder[0].auth_credentials[0].model_copy(update={field: "changed"})
        holder[0] = holder[0].model_copy(update={"auth_credentials": [credential]})
    else:
        async with jobs.approvals.connection() as conn:
            job = await job_row(jobs, row["id"])
            expires = datetime.now(UTC) - timedelta(seconds=1)
            credential = (
                "damaged"
                if change == "ciphertext"
                else jobs.seal(
                    AUTHORIZATION,
                    owner,
                    {"id": row["id"], "plan_hash": row["plan_hash"]},
                    expires,
                )
            )
            await conn.execute(
                "UPDATE background_jobs SET credential=%s,expires_at=%s WHERE request_id=%s",
                (credential, expires, row["id"]),
            )
    await make_due(jobs, row["id"])
    await jobs.run(row["id"])
    job = await job_row(jobs, row["id"])
    assert job["status"] == "blocked" and job["credential"] is None
    assert not store.snapshot(owner.tenant)["users"]
    result = (await jobs.approvals.get(row["id"], owner))["execution"]
    assert result["steps"][0]["attempts"] == 1


async def test_explicit_new_credential_reauthorizes_without_resetting_journal(execution_env):
    _, owner, _, row, _, _ = execution_env
    jobs, gateway, holder = await setup_jobs(execution_env)
    calls = wait_once(gateway)
    await jobs.execute(row["id"], owner, AUTHORIZATION)
    holder[0] = settings_for(owner, "rotated-test-token")
    await make_due(jobs, row["id"])
    await jobs.run(row["id"])
    assert (await job_row(jobs, row["id"]))["status"] == "blocked"
    result = await jobs.execute(row["id"], owner, "Bearer rotated-test-token")
    assert result["status"] == "succeeded" and result["steps"][0]["attempts"] == 2
    assert calls[0] == calls[1]


async def test_compensation_retry_waiting_is_resumed(execution_env):
    _, owner, _, row, _, _ = execution_env
    jobs, gateway, _ = await setup_jobs(execution_env, [None, None, None, "domain_policy"])
    calls = wait_once(gateway, compensation=True)
    result = await jobs.execute(row["id"], owner, AUTHORIZATION)
    assert result["status"] == "compensation_waiting"
    await make_due(jobs, row["id"])
    await jobs.run(row["id"])
    result = (await jobs.approvals.get(row["id"], owner))["execution"]
    assert result["status"] == "partial_success"
    assert result["saga"]["steps"][0]["attempts"] == 2
    assert calls[0] == calls[1]
    assert (await job_row(jobs, row["id"]))["status"] == "completed"


@pytest.mark.parametrize("stage", ["entry", "forward", "compensation"])
async def test_policy_outage_pauses_without_wrong_saga_or_consuming_attempts(
    execution_env,
    monkeypatch,
    stage,
):
    service, owner, _, row, _, _ = execution_env
    jobs, gateway, _ = await setup_jobs(
        execution_env,
        [None, None, None, "domain_policy"] if stage == "compensation" else None,
    )
    wait_once(gateway)
    await jobs.execute(row["id"], owner, AUTHORIZATION)
    await make_due(jobs, row["id"])
    original = service.policy.authorize
    denied_action = {
        "entry": "approval.execute",
        "forward": "identity.create_user",
        "compensation": "identity.disable_user",
    }[stage]

    async def unavailable(query):
        if query.action == denied_action:
            return PolicyDecision(allow=False, reason="temporary_outage", degraded=True)
        return await original(query)

    monkeypatch.setattr(service.policy, "authorize", unavailable)
    await jobs.run(row["id"])
    job = await job_row(jobs, row["id"])
    assert job["status"] == "waiting" and job["last_error"] == "execution_policy_unavailable"
    result = (await service.get(row["id"], owner))["execution"]
    if stage == "compensation":
        assert result["status"] == "compensating"
        assert result["saga"]["steps"][0]["attempts"] == 0
    else:
        assert result["steps"][0]["attempts"] == 1 and "saga" not in result
    monkeypatch.setattr(service.policy, "authorize", original)
    await make_due(jobs, row["id"])
    await jobs.run(row["id"])
    assert (await job_row(jobs, row["id"]))["status"] == "completed"


async def test_legacy_crash_after_forward_stop_before_saga_remains_recoverable(
    execution_env,
    monkeypatch,
):
    service, owner, _, row, _, _ = execution_env
    jobs, gateway, _ = await setup_jobs(execution_env, [None, None, None, "domain_policy"])
    original = Saga.run

    async def crash(*_):
        raise RuntimeError("legacy_crash_before_saga")

    monkeypatch.setattr(Saga, "run", crash)
    with pytest.raises(RuntimeError, match="legacy_crash_before_saga"):
        await ExecutionService(service, gateway).execute(row["id"], owner)
    assert await job_row(jobs, row["id"]) is None
    result = (await service.get(row["id"], owner))["execution"]
    assert result["status"] == "compensation_required" and "saga" not in result
    monkeypatch.setattr(Saga, "run", original)
    result = await jobs.execute(row["id"], owner, AUTHORIZATION)
    assert result["status"] == "partial_success"
    assert result["summary"]["compensated"] == ["p1_user"]
    assert (await job_row(jobs, row["id"]))["status"] == "completed"


async def test_infrastructure_errors_have_bounded_retries(execution_env, monkeypatch):
    _, owner, _, row, store, _ = execution_env
    jobs, gateway, _ = await setup_jobs(execution_env)
    wait_once(gateway)
    await jobs.execute(row["id"], owner, AUTHORIZATION)

    async def broken(*_):
        raise ApprovalError(503, "temporary_dependency_unavailable")

    monkeypatch.setattr(ExecutionService, "run_prepared", broken)
    for _ in range(6):
        await make_due(jobs, row["id"])
        await jobs.run(row["id"])
    job = await job_row(jobs, row["id"])
    assert job["failures"] == 6 and job["status"] == "blocked" and job["credential"] is None
    await jobs.run(row["id"])
    assert (await job_row(jobs, row["id"]))["failures"] == 6
    assert not store.snapshot(owner.tenant)["users"]


async def test_real_redis_lost_queue_rebuilt_from_postgres(execution_env, monkeypatch):
    _, owner, _, row, store, _ = execution_env
    jobs, gateway, _ = await setup_jobs(execution_env)
    wait_once(gateway)
    await jobs.execute(row["id"], owner, AUTHORIZATION)
    await make_due(jobs, row["id"])
    queue = "caseflow:test-background:" + uuid4().hex
    monkeypatch.setattr("core.jobs.worker.QUEUE", queue)
    redis = await create_pool(
        RedisSettings.from_dsn(Settings().redis_url),
        job_serializer=serialize,
        job_deserializer=json.loads,
    )
    worker = Worker(
        [resume_execution],
        redis_pool=redis,
        queue_name=queue,
        ctx={"jobs": jobs},
        handle_signals=False,
        burst=True,
        job_serializer=serialize,
        job_deserializer=json.loads,
        keep_result=0,
        max_tries=1,
        retry_jobs=False,
        poll_delay=0.01,
    )
    try:
        assert await dispatch_due(jobs, redis) == 1
        queued = await redis.zrange(queue, 0, -1)
        assert len(queued) == 1
        blob = await redis.get(b"arq:job:" + queued[0])
        assert TOKEN.encode() not in blob and b"credential" not in blob
        # Delete this test's queued data only; never flush a shared Redis database.
        await redis.delete(queue, b"arq:job:" + queued[0])
        assert await dispatch_due(jobs, redis) == 1
        await worker.async_run()
        assert (await job_row(jobs, row["id"]))["status"] == "completed"
        assert len(store.snapshot(owner.tenant)["users"]) == 3
    finally:
        await redis.delete(queue, queue + ":health-check")
        async for key in redis.scan_iter(match=f"*{row['id']}*"):
            await redis.delete(key)
        await close_worker(worker)
