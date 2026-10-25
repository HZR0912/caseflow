"""Real PG receipts, immutable journals, role/tenant scopes and closure concurrency."""

import asyncio
import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest
from apps.api.auth import get_current_subject
from apps.mockcorp.main import create_app
from core.graph.approval_service import ApprovalError
from core.tools.gateway import ToolResult
from core.tools.reconciliation import HttpEvidenceGateway, ReconciliationService, ResolutionDecision
from psycopg.types.json import Jsonb

from tests import test_execution_integration as execution_tests
from tests.test_saga_integration import paid_plan, prepare

execution_env = execution_tests.execution_env
pytestmark = pytest.mark.integration


async def failed_refund(env, *, commit=True):
    _, owner, _, _, store, supplier = env
    row, executor, gateway = await prepare(env, paid_plan(env))
    forward, undo = gateway.call, gateway.compensate

    async def fail_seat(step, args, key):
        if step.operation == "money.bind_seat":
            return ToolResult("compensation_required", "seat_limit")
        return await forward(step, args, key)

    async def lost_undo(operation, source_key, key):
        if operation == "money.create_order":
            if commit:
                await undo(operation, source_key, key)
            return ToolResult("human_required", "invalid_success_receipt", outcome_unknown=True)
        return await undo(operation, source_key, key)

    gateway.call, gateway.compensate = fail_seat, lost_undo
    journal = await executor.execute(row["id"], owner)
    assert journal["status"] == "compensation_failed"
    # Another Saga member remains intact, so closure must explicitly accept it.
    assert journal["summary"]["compensation_unknown"] == ["p1_order"]
    app = create_app(store, supplier.settings)
    app.dependency_overrides[get_current_subject] = lambda: owner
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://supplier")
    evidence = HttpEvidenceGateway("http://supplier", "Bearer test", client)
    return row, executor, journal, ReconciliationService(env[0], evidence), client


def decision(view, action="accept_observed_state", reason="核对收据后接受保留的成员权限"):
    evidence = view["latest_evidence"]
    return ResolutionDecision(
        evidence_id=evidence["id"],
        journal_hash=evidence["journal_hash"],
        decision=action,
        reason=reason,
    )


async def test_committed_lost_refund_is_reconciled_closed_once_without_writes(execution_env):
    approvals, owner, _, _, store, _ = execution_env
    row, executor, original, service, client = await failed_refund(execution_env)
    async with client:
        before, logs = store.snapshot(owner.tenant), store.oplog(owner.tenant)
        inspected = await service.inspect(row["id"], owner)
        evidence = inspected["latest_evidence"]
        assert (
            next(i for i in evidence["items"] if i["step_id"] == "p1_order")["state"]
            == "compensated"
        )
        assert evidence["can_accept_observed_state"] and not evidence["can_confirm_compensated"]
        with pytest.raises(ApprovalError, match="reconciliation_compensation_not_confirmed"):
            await service.resolve(row["id"], decision(inspected, "confirm_compensated"), owner)
        closed = await service.resolve(row["id"], decision(inspected), owner)
        assert closed["status"] == "closed"
        assert (await service.resolve(row["id"], decision(inspected), owner)) == closed
        with pytest.raises(ApprovalError, match="reconciliation_already_closed"):
            await executor.execute(row["id"], owner)
        assert (await approvals.get(row["id"], owner))["execution"] == original
        assert store.snapshot(owner.tenant) == before and store.oplog(owner.tenant) == logs
        assert len(before["refunds"]) == 1
        assert [e["event"] for e in closed["events"]] == ["closed", "inspected"]
        assert closed["events"][0]["previous_hash"] == closed["events"][1]["event_hash"]


async def test_missing_unknown_receipt_cannot_be_closed_but_can_be_kept_open(execution_env):
    _, owner, _, _, store, _ = execution_env
    row, _, _, service, client = await failed_refund(execution_env, commit=False)
    async with client:
        inspected = await service.inspect(row["id"], owner)
        assert not inspected["latest_evidence"]["can_accept_observed_state"]
        with pytest.raises(ApprovalError, match="reconciliation_unconfirmed_outcome"):
            await service.resolve(row["id"], decision(inspected), owner)
        result = await service.resolve(row["id"], decision(inspected, "keep_open"), owner)
        assert result["status"] == "open" and result["resolution"] is None
        assert not store.snapshot(owner.tenant)["refunds"]


@pytest.mark.parametrize("change", ["journal", "expired", "superseded", "supplier"])
async def test_stale_evidence_cannot_close(execution_env, change):
    approvals, owner, _, _, store, supplier = execution_env
    row, _, original, service, client = await failed_refund(execution_env)
    async with client:
        inspected = await service.inspect(row["id"], owner)
        expected = "reconciliation_stale_evidence"
        if change == "journal":
            changed = deepcopy(original)
            changed["changed"] = True
            with psycopg.connect(approvals.url) as conn:
                conn.execute(
                    "UPDATE execution_runs SET journal=%s WHERE request_id=%s",
                    (Jsonb(changed), row["id"]),
                )
        elif change == "expired":
            with psycopg.connect(approvals.url) as conn:
                conn.execute(
                    "UPDATE reconciliation_evidence SET expires_at=%s WHERE id=%s",
                    (datetime.now(UTC) - timedelta(seconds=1), inspected["latest_evidence"]["id"]),
                )
            expected = "reconciliation_evidence_expired"
        elif change == "superseded":
            await service.inspect(row["id"], owner)
        else:
            member = next(iter(store.snapshot(owner.tenant)["members"].values()))
            supplier.execute(
                owner.tenant,
                "code.set_permission",
                {"repo_id": member["repo_id"], "user_id": member["user_id"], "permission": "write"},
                key="external-permission-change",
            )
            expected = "reconciliation_evidence_changed"
        with pytest.raises(ApprovalError, match=expected):
            await service.resolve(row["id"], decision(inspected), owner)
        assert (await service.get(row["id"], owner))["resolution"] is None


async def test_authority_tenant_isolation_and_same_request_lock(execution_env):
    approvals, owner, _, _, _, _ = execution_env
    row, _, _, service, client = await failed_refund(execution_env)
    async with client:
        inspected = await service.inspect(row["id"], owner)
        for method in (service.inspect, service.get):
            with pytest.raises(ApprovalError, match="approval_not_found"):
                await method(row["id"], owner.model_copy(update={"tenant": "other"}))
        with pytest.raises(ApprovalError, match="approval_policy_denied"):
            await service.inspect(row["id"], owner.model_copy(update={"role": "employee"}))
        with pytest.raises(ApprovalError, match="approval_policy_denied"):
            await service.resolve(
                row["id"], decision(inspected), owner.model_copy(update={"role": "employee"})
            )
        async with approvals.session(row["id"]):
            with pytest.raises(ApprovalError, match="approval_busy_retry"):
                await service.resolve(row["id"], decision(inspected), owner)
        outcomes = await asyncio.gather(
            *(service.resolve(row["id"], decision(inspected), owner) for _ in range(2)),
            return_exceptions=True,
        )
        assert any(isinstance(o, dict) and o["status"] == "closed" for o in outcomes)
        assert all(
            isinstance(o, dict)
            or (isinstance(o, ApprovalError) and o.detail == "approval_busy_retry")
            for o in outcomes
        )


async def test_external_changes_can_be_explicitly_accepted_after_fresh_inspection(execution_env):
    _, owner, _, _, store, supplier = execution_env
    row, _, _, service, client = await failed_refund(execution_env)
    member = next(iter(store.snapshot(owner.tenant)["members"].values()))
    supplier.execute(
        owner.tenant,
        "code.set_permission",
        {"repo_id": member["repo_id"], "user_id": member["user_id"], "permission": "write"},
        key="external-change-before-inspection",
    )
    async with client:
        inspected = await service.inspect(row["id"], owner)
        changed = next(
            i for i in inspected["latest_evidence"]["items"] if i["step_id"] == "p1_invite"
        )
        assert changed["state"] == "changed" and changed["needs_attention"]
        assert inspected["latest_evidence"]["can_accept_observed_state"]
        before = store.oplog(owner.tenant)
        result = await service.resolve(
            row["id"], decision(inspected, reason="已核实外部权限变更并接受现状"), owner
        )
        assert result["status"] == "closed"
        assert store.oplog(owner.tenant) == before


async def test_all_saga_compensations_confirmed_can_close_without_claiming_workflow_success(
    execution_env,
):
    approvals, owner, _, _, store, supplier = execution_env
    row, _, journal, service, client = await failed_refund(execution_env)
    # Supplier operation completes outside the failed executor, under the SAME undo key.
    remaining = journal["saga"]["steps"][1]
    supplier.compensate(
        owner.tenant, remaining["source_key"], remaining["operation"], key=remaining["key"]
    )
    async with client:
        inspected = await service.inspect(row["id"], owner)
        assert inspected["latest_evidence"]["can_confirm_compensated"]
        result = await service.resolve(row["id"], decision(inspected, "confirm_compensated"), owner)
        assert result["status"] == "closed"
        assert (await approvals.get(row["id"], owner))["execution"] == journal
        assert len(store.snapshot(owner.tenant)["refunds"]) == 1


async def test_foreign_payload_receipt_under_same_key_is_never_accepted(execution_env):
    approvals, owner, _, _, _, _ = execution_env
    row, _, journal, service, client = await failed_refund(execution_env)
    key = next(s["key"] for s in journal["steps"] if s["step_id"] == "p1_order")
    with psycopg.connect(approvals.url) as conn:
        conn.execute(
            "UPDATE mockcorp_receipts SET fingerprint=%s WHERE tenant=%s AND key=%s",
            ("0" * 64, owner.tenant, key),
        )
    async with client:
        inspected = await service.inspect(row["id"], owner)
        item = next(i for i in inspected["latest_evidence"]["items"] if i["step_id"] == "p1_order")
        assert item["forward"] == "invalid" and item["state"] == "unconfirmed"
        with pytest.raises(ApprovalError, match="reconciliation_unconfirmed_outcome"):
            await service.resolve(row["id"], decision(inspected), owner)


async def test_api_routes_authentication_tenant_scope_and_closure(execution_env, monkeypatch):
    from apps.api import reconciliation as routes
    from apps.api.main import app
    from apps.api.settings import Settings, get_settings
    from core.jobs.service import BackgroundJobs
    from core.obs.runtime import Runtime

    approvals, owner, _, _, store, _ = execution_env
    row, _, journal, service, supplier_client = await failed_refund(execution_env)
    identities = [
        {
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "user_id": actor,
            "role": role,
            "tenant": tenant,
        }
        for token, actor, role, tenant in [
            ("recon-admin", owner.id, "admin", owner.tenant),
            ("recon-employee", owner.id, "employee", owner.tenant),
            ("recon-outsider", "outside", "admin", "other-tenant"),
        ]
    ]
    settings = Settings(_env_file=None, database_url=approvals.url, auth_credentials=identities)
    runtime = Runtime(settings)
    monkeypatch.setattr(app.state, "approvals", approvals, raising=False)
    monkeypatch.setattr(
        app.state, "background_jobs", BackgroundJobs(approvals, lambda: settings), raising=False
    )
    monkeypatch.setattr(app.state, "observability", runtime, raising=False)
    monkeypatch.setitem(app.dependency_overrides, get_settings, lambda: settings)
    monkeypatch.setattr(routes, "service", lambda approvals, request: service)
    path = "/v1/approval-requests/" + row["id"]
    async with (
        supplier_client,
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://api") as client,
    ):
        try:
            assert (await client.get(path + "/reconciliation")).status_code == 401
            for route in ("reconciliation", "background"):
                response = await client.get(
                    path + "/" + route, headers={"Authorization": "Bearer recon-outsider"}
                )
                assert response.status_code == 404
            employee = {"Authorization": "Bearer recon-employee"}
            assert (await client.get(path + "/reconciliation", headers=employee)).status_code == 200
            assert (await client.post(path + "/reconcile", headers=employee)).status_code == 403
            admin = {"Authorization": "Bearer recon-admin"}
            assert (await client.get(path + "/background", headers=admin)).json() == {"job": None}
            inspected = await client.post(path + "/reconcile", headers=admin)
            assert inspected.status_code == 200
            body = decision(inspected.json()).model_dump(mode="json")
            assert (
                await client.post(path + "/resolve", headers=employee, json=body)
            ).status_code == 403
            assert (
                await client.post(
                    path + "/resolve", headers=admin, json={**body, "tenant": owner.tenant}
                )
            ).status_code == 422
            logs = store.oplog(owner.tenant)
            closed = await client.post(path + "/resolve", headers=admin, json=body)
            assert closed.status_code == 200 and closed.json()["status"] == "closed"
            assert (await client.get(path + "/reconciliation", headers=admin)).json()["resolution"][
                "decision"
            ] == "accept_observed_state"
            assert (await client.post(path + "/execute", headers=admin)).status_code == 409
            assert store.oplog(owner.tenant) == logs
            assert (await approvals.get(row["id"], owner))["execution"] == journal
        finally:
            await runtime.close()
