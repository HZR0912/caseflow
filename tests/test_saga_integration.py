"""Postgres: precise Saga scope, durable undo, partial outcomes and failure boundaries."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from apps.mockcorp.preview import preview_plan
from core.graph.approval_service import ApprovalError, ApprovalService
from core.graph.contracts import Plan, PlanStep
from core.graph.onboarding import OnboardingFacts, PlanningRequest, make_plan
from core.tools.execution import ExecutionService, RetryPolicy
from core.tools.gateway import ToolResult
from core.tools.payload import PreparedApproval
from psycopg.types.json import Jsonb

from tests import test_execution_integration as execution_tests
from tests.test_approval_integration import choice
from tests.test_execution_integration import SupplierGateway

execution_env = execution_tests.execution_env

pytestmark = pytest.mark.integration


def paid_plan(env):
    _, owner, _, _, store, _ = env
    state = store.snapshot(owner.tenant)
    return make_plan(
        OnboardingFacts(
            employees=[{"name": "测试员工", "username": "sagauser"}],
            role="developer",
            start_date="2026-10-26",
            end_date="2026-12-31",
            email_domain="example.com",
            repo_id=next(iter(state["repos"])),
            project_id=next(iter(state["projects"])),
            permission="read",
            paid_item="editor",
            unit_price_cents=500,
        ),
        [],
    )


async def prepare(env, plan, faults=None):
    service, owner, reviewer, _, store, supplier = env

    async def snapshot():
        preview = preview_plan(store, owner.tenant, owner.id, plan).body
        assert preview["ok"]
        return PreparedApproval(
            {
                "status": "awaiting_approval",
                "plan": plan.model_dump(mode="json"),
                "preview": preview,
            },
            plan,
        )

    row = await service.create(PlanningRequest(text="Saga test"), owner, str(uuid4()), snapshot)
    await service.decide(row["id"], choice(row), reviewer)
    gateway = SupplierGateway(supplier, owner.tenant, faults)
    return row, ExecutionService(service, gateway, RetryPolicy(base_delay=0, max_delay=0)), gateway


async def test_insufficient_funds_removes_only_membership_keeps_identity(execution_env):
    _, owner, _, _, store, _ = execution_env
    row, executor, _ = await prepare(
        execution_env, paid_plan(execution_env), [None, None, None, "insufficient_funds"]
    )
    result = await executor.execute(row["id"], owner)
    assert result["status"] == "partial_success"
    assert result["summary"]["retained"] == ["p1_user", "p1_mail"]
    assert result["summary"]["compensated"] == ["p1_invite"]
    state = store.snapshot(owner.tenant)
    assert not state["members"] and not state["orders"]
    assert all(u["active"] for u in state["users"].values())
    assert all(m["active"] for m in state["mailboxes"].values())
    assert state["balance_cents"] == 100000


async def test_seat_failure_refunds_order_then_removes_member_once(execution_env):
    _, owner, _, _, store, _ = execution_env
    row, executor, gateway = await prepare(execution_env, paid_plan(execution_env))
    original = gateway.call

    async def fail_seat(step, args, key):
        if step.operation == "money.bind_seat":
            return ToolResult("compensation_required", "purchased_seat_limit")
        return await original(step, args, key)

    gateway.call = fail_seat
    result = await executor.execute(row["id"], owner)
    assert [i["step_id"] for i in result["saga"]["steps"]] == ["p1_order", "p1_invite"]
    before = store.snapshot(owner.tenant)
    assert before["balance_cents"] == 100000 and len(before["refunds"]) == 1
    assert not before["members"]
    assert await executor.execute(row["id"], owner) == result
    assert store.snapshot(owner.tenant) == before


async def test_last_step_failure_unbind_refund_remove_reverse_order(execution_env):
    _, owner, _, _, store, _ = execution_env
    row, executor, gateway = await prepare(execution_env, paid_plan(execution_env))
    original = gateway.call

    async def fail_ticket(step, args, key):
        return (
            ToolResult("human_required", "http_422")
            if step.operation == "ticket.create_ticket"
            else await original(step, args, key)
        )

    gateway.call = fail_ticket
    result = await executor.execute(row["id"], owner)
    assert [i["step_id"] for i in result["saga"]["steps"]] == ["p1_seat", "p1_order", "p1_invite"]
    state = store.snapshot(owner.tenant)
    assert all(not seat["active"] for seat in state["seats"].values())
    assert state["balance_cents"] == 100000 and not state["members"]


async def test_compensation_commit_then_crash_resumes_same_key(execution_env):
    service, owner, _, _, store, _ = execution_env
    row, executor, gateway = await prepare(
        execution_env, paid_plan(execution_env), [None, None, None, "insufficient_funds"]
    )
    original = gateway.compensate

    async def crash(*args):
        await original(*args)
        raise RuntimeError("crash_after_undo_commit")

    gateway.compensate = crash
    with pytest.raises(RuntimeError, match="crash_after_undo_commit"):
        await executor.execute(row["id"], owner)
    assert not store.snapshot(owner.tenant)["members"]
    gateway.compensate = original
    fresh = ExecutionService(
        ApprovalService(service.url, service.policy, cipher=service.cipher), gateway
    )
    result = await fresh.execute(row["id"], owner)
    assert result["status"] == "partial_success"
    item = result["saga"]["steps"][0]
    assert item["attempts"] == 2 and item["events"][1]["replayed"]
    logs = store.oplog(owner.tenant)["items"]
    assert (
        sum(
            r["operation"] == "saga.compensate"
            and r["outcome"] == "committed"
            and not r["replayed"]
            for r in logs
        )
        == 1
    )


@pytest.mark.parametrize("mode", ["denied", "exhausted", "changed"])
async def test_compensation_failure_stops_before_earlier_dependencies(execution_env, mode):
    service, owner, _, _, store, supplier = execution_env
    row, executor, gateway = await prepare(execution_env, paid_plan(execution_env))
    forward = gateway.call
    original_undo = gateway.compensate

    async def failure(step, args, key):
        if step.operation == "money.bind_seat":
            if mode == "denied":
                original_policy = service.policy.authorize

                async def denied(query):
                    result = await original_policy(query)
                    return result.model_copy(update={"allow": query.action != "money.refund"})

                service.policy.authorize = denied
            return ToolResult("compensation_required", "purchased_seat_limit")
        return await forward(step, args, key)

    async def blocked_undo(operation, source, key):
        if mode == "exhausted":
            return ToolResult("retryable", "http_500", outcome_unknown=True)
        if mode == "changed":
            order = next(iter(store.snapshot(owner.tenant)["orders"]))
            supplier.execute(
                owner.tenant,
                "money.refund",
                {"order_id": order, "amount_cents": 100},
                key="external-refund",
            )
        return await original_undo(operation, source, key)

    gateway.call, gateway.compensate = failure, blocked_undo
    result = await executor.execute(row["id"], owner)
    assert result["status"] == "compensation_failed"
    assert result["saga"]["steps"][0]["status"] == "failed"
    assert result["saga"]["steps"][1]["attempts"] == 0
    assert store.snapshot(owner.tenant)["members"]
    assert await executor.execute(row["id"], owner) == result


async def test_partial_assignment_reports_items_and_restores_successful_subset(execution_env):
    _, owner, _, _, store, supplier = execution_env
    # A preexisting assignee and tickets are independent of this plan.
    user = supplier.execute(
        owner.tenant,
        "identity.create_user",
        {"username": "assignee", "display_name": "Assignee"},
        key="assignee",
    ).body["result"]["id"]
    project = next(iter(store.snapshot(owner.tenant)["projects"]))
    ids = [
        supplier.execute(
            owner.tenant,
            "ticket.create_ticket",
            {"project_id": project, "title": str(i), "assignee_id": None},
            key="ticket" + str(i),
        ).body["result"]["id"]
        for i in range(4)
    ]
    plan = Plan(
        saga_version=1,
        steps=[
            PlanStep(
                id="batch",
                operation="ticket.assign_tickets",
                arguments={"ticket_ids": ids, "user_id": user},
                risk="medium",
                reversibility="reversible",
                rollback="restore",
                saga_group="batch",
            )
        ],
    )
    before = deepcopy(store.snapshot(owner.tenant))
    row, executor, _ = await prepare(execution_env, plan, ["partial_success"])
    result = await executor.execute(row["id"], owner)
    assert result["status"] == "compensated"
    assert result["steps"][0]["status"] == "partial_success"
    assert [i["status"] for i in result["steps"][0]["items"]] == [
        "assigned",
        "assigned",
        "failed",
        "failed",
    ]
    assert store.snapshot(owner.tenant) == before


async def test_unknown_receipt_never_starts_compensation(execution_env):
    _, owner, _, _, store, _ = execution_env
    row, executor, gateway = await prepare(execution_env, paid_plan(execution_env))
    original = gateway.call

    async def unknown(step, args, key):
        if step.operation == "money.create_order":
            return ToolResult("human_required", "invalid_success_receipt", outcome_unknown=True)
        return await original(step, args, key)

    gateway.call = unknown
    result = await executor.execute(row["id"], owner)
    assert result["saga"]["status"] == "blocked_unknown_outcome"
    assert result["summary"]["unknown"] == ["p1_order"]
    assert not result["compensation_executed"]
    assert store.snapshot(owner.tenant)["members"]


async def test_crash_after_forward_failure_before_saga_is_recoverable(execution_env, monkeypatch):
    service, owner, _, _, _, _ = execution_env
    row, executor, gateway = await prepare(
        execution_env, paid_plan(execution_env), [None, None, None, "insufficient_funds"]
    )
    from core.tools.saga import Saga

    async def crash(*args):
        raise RuntimeError("before_saga")

    with monkeypatch.context() as patch:
        patch.setattr(Saga, "run", crash)
        with pytest.raises(RuntimeError, match="before_saga"):
            await executor.execute(row["id"], owner)
    fresh = ExecutionService(service, gateway)
    assert (await fresh.execute(row["id"], owner))["status"] == "partial_success"


async def test_compensation_concurrency_and_retry_after_survive_restart(execution_env):
    service, owner, _, _, _, _ = execution_env
    row, executor, gateway = await prepare(
        execution_env, paid_plan(execution_env), [None, None, None, "insufficient_funds"]
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = gateway.compensate

    async def limited(*args):
        entered.set()
        await release.wait()
        return ToolResult("retryable", "http_429", retry_after=60)

    gateway.compensate = limited
    first = asyncio.create_task(executor.execute(row["id"], owner))
    await asyncio.wait_for(entered.wait(), 10)
    try:
        with pytest.raises(ApprovalError, match="approval_busy_retry"):
            await executor.execute(row["id"], owner)
    finally:
        release.set()
    waiting = await first
    assert waiting["status"] == "compensation_waiting"
    assert waiting["saga"]["steps"][0]["attempts"] == 1
    assert not waiting["summary"]["compensation_unknown"]
    fresh = ExecutionService(service, gateway)
    assert await fresh.execute(row["id"], owner) == waiting
    # Advance the persisted deadline, without sleeping or resetting the attempt budget.
    waiting["saga"]["steps"][0]["next_attempt_at"] = (
        datetime.now(UTC) - timedelta(seconds=1)
    ).isoformat()
    with psycopg.connect(service.url) as conn:
        conn.execute(
            "UPDATE execution_runs SET journal=%s WHERE request_id=%s", (Jsonb(waiting), row["id"])
        )
    gateway.compensate = original
    result = await fresh.execute(row["id"], owner)
    assert result["status"] == "partial_success"
    assert result["saga"]["steps"][0]["attempts"] == 2


async def test_irreversible_retained_consumer_protects_ancestors(execution_env):
    _, owner, _, _, store, _ = execution_env
    plan = paid_plan(execution_env)
    # Fail after the irreversible ticket, in the same access group.
    extra = plan.steps[-2].model_copy(deep=True)
    extra.id = "second_order"
    extra.depends_on = ["p1_ticket"]
    plan.steps.append(extra)
    row, executor, gateway = await prepare(execution_env, plan)
    original = gateway.call

    async def fail_extra(step, args, key):
        if step.id == "second_order":
            return ToolResult("compensation_required", "insufficient_funds")
        return await original(step, args, key)

    gateway.call = fail_extra
    result = await executor.execute(row["id"], owner)
    assert result["status"] == "compensation_failed"
    assert all(i["status"] == "blocked" for i in result["saga"]["steps"])
    assert not result["compensation_executed"]
    state = store.snapshot(owner.tenant)
    assert state["members"] and all(s["active"] for s in state["seats"].values())
    assert state["balance_cents"] == 99500 and not state["refunds"]


async def test_w7_encrypted_plan_without_saga_fields_keeps_original_hash_and_behavior(
    execution_env,
):
    service, owner, reviewer, _, store, _ = execution_env
    plan = paid_plan(execution_env)
    legacy = plan.model_dump(mode="json")
    legacy.pop("saga_version")
    for step in legacy["steps"]:
        step.pop("saga_group")
    # Seal exactly the old schema, preserving its digest through decrypt/validate.
    original_seal = service.cipher.seal

    def seal_old(_plan, tenant, request_id):
        return service.cipher.fernet.encrypt(
            json.dumps({"tenant": tenant, "request_id": request_id, "plan": legacy}).encode()
        ).decode()

    service.cipher.seal = seal_old
    try:

        class LegacyPlan(Plan):
            def model_dump(self, **kwargs):
                return legacy

        async def snapshot():
            return PreparedApproval(
                {"status": "awaiting_approval"}, LegacyPlan.model_validate(legacy)
            )

        row = await service.create(PlanningRequest(text="legacy"), owner, str(uuid4()), snapshot)
    finally:
        service.cipher.seal = original_seal
    await service.decide(row["id"], choice(row), reviewer)
    gateway = SupplierGateway(
        execution_env[-1], owner.tenant, [None, None, None, "insufficient_funds"]
    )
    result = await ExecutionService(service, gateway).execute(row["id"], owner)
    assert result["status"] == "compensation_required" and "saga" not in result
    assert store.snapshot(owner.tenant)["members"]
