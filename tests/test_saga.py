"""Supplier compensation semantics and strict partial receipt validation."""

from copy import deepcopy

import httpx
import pytest
from apps.mockcorp.models import MockSettings
from apps.mockcorp.service import MockCorp
from core.graph.onboarding import OnboardingFacts
from core.tools.compensation import compensation_key
from core.tools.gateway import classify

from tests.mockcorp_support import MemoryStore


def supplier():
    settings = MockSettings(_env_file=None, faults_enabled=True, fault_delay_s=0)
    store = MemoryStore(settings)
    return store, MockCorp(store, settings)


def call(service, operation, payload, key):
    reply = service.execute("t", operation, payload, key=key)
    assert reply.status < 300, reply.body
    return reply.body["result"]


def undo(service, operation, source, **kwargs):
    return service.compensate("t", source, operation, key=compensation_key(source), **kwargs)


def test_refund_uses_delta_preserves_unrelated_purchase_and_is_idempotent():
    store, service = supplier()
    call(
        service,
        "money.create_order",
        {"item": "first", "quantity": 1, "unit_price_cents": 700},
        "order",
    )
    call(
        service,
        "money.create_order",
        {"item": "other", "quantity": 1, "unit_price_cents": 200},
        "other",
    )
    first = undo(service, "money.create_order", "order")
    assert first.status == 200
    assert store.snapshot("t")["balance_cents"] == 99800
    assert undo(service, "money.create_order", "order").headers["Idempotency-Replayed"] == "true"
    assert len(store.snapshot("t")["refunds"]) == 1
    assert service.compensate("t", "order", "money.create_order", key="new-key").status == 409
    assert undo(service, "money.create_order", "order", dry_run=True).status == 409
    assert len(store.snapshot("t")["refunds"]) == 1


def test_cross_tenant_and_operation_substitution_cannot_compensate():
    store, service = supplier()
    call(
        service,
        "money.create_order",
        {"item": "first", "quantity": 1, "unit_price_cents": 700},
        "order",
    )
    before = store.snapshot("t")
    assert (
        service.compensate(
            "other", "order", "money.create_order", key=compensation_key("order")
        ).status
        == 409
    )
    assert undo(service, "code.invite_member", "order").status == 409
    assert store.snapshot("t") == before


def test_cas_rejects_changed_membership_and_preview_never_mutates():
    store, service = supplier()
    user = call(
        service, "identity.create_user", {"username": "alice", "display_name": "Alice"}, "user"
    )
    call(
        service,
        "identity.create_mailbox",
        {"user_id": user["id"], "email": "alice@example.com"},
        "mail",
    )
    repo = call(service, "code.create_repo", {"name": "repo"}, "repo")
    member = call(
        service,
        "code.invite_member",
        {"repo_id": repo["id"], "user_id": user["id"], "permission": "read"},
        "member",
    )
    before = store.snapshot("t")
    assert undo(service, "code.invite_member", "member", dry_run=True).status == 200
    assert store.snapshot("t") == before
    call(
        service,
        "code.set_permission",
        {"repo_id": repo["id"], "user_id": user["id"], "permission": "write"},
        "changed",
    )
    assert (
        undo(service, "code.invite_member", "member").body["error"]["code"]
        == "compensation_state_changed"
    )
    assert store.snapshot("t")["members"][member["id"]]["permission"] == "write"


def test_user_undo_does_not_disable_unrelated_dependencies():
    store, service = supplier()
    user = call(
        service, "identity.create_user", {"username": "alice", "display_name": "Alice"}, "user"
    )
    call(
        service,
        "identity.create_mailbox",
        {"user_id": user["id"], "email": "alice@example.com"},
        "mail",
    )
    assert (
        undo(service, "identity.create_user", "user").body["error"]["code"]
        == "compensation_dependency_in_use"
    )
    assert store.snapshot("t")["users"][user["id"]]["active"]
    assert undo(service, "identity.create_mailbox", "mail").status == 200
    assert undo(service, "identity.create_user", "user").status == 200
    assert not store.snapshot("t")["users"][user["id"]]["active"]


@pytest.mark.parametrize("changed_success", [False, True])
def test_partial_batch_restores_only_changed_items_and_atomic_cas(changed_success):
    store, service = supplier()
    user = call(
        service, "identity.create_user", {"username": "alice", "display_name": "Alice"}, "user"
    )
    project = call(service, "ticket.create_project", {"key": "TEST", "name": "Test"}, "project")
    tickets = [
        call(
            service,
            "ticket.create_ticket",
            {"project_id": project["id"], "title": str(i), "assignee_id": None},
            "t" + str(i),
        )["id"]
        for i in range(4)
    ]
    reply = service.execute(
        "t",
        "ticket.assign_tickets",
        {"ticket_ids": tickets, "user_id": user["id"]},
        key="batch",
        fault="partial_success",
    )
    assert reply.status == 207
    if changed_success:
        store.states["t"]["tickets"][tickets[1]]["status"] = "closed"
        before_undo = store.snapshot("t")
        assert undo(service, "ticket.assign_tickets", "batch").status == 409
        assert store.snapshot("t") == before_undo
        return
    # Failed item was later modified independently; compensation must leave it alone.
    store.states["t"]["tickets"][tickets[-1]]["assignee_id"] = user["id"]
    assert undo(service, "ticket.assign_tickets", "batch").status == 200
    current = store.snapshot("t")["tickets"]
    assert all(current[tid]["assignee_id"] is None for tid in tickets[:3])
    assert current[tickets[-1]]["assignee_id"] == user["id"]
    assert undo(service, "ticket.assign_tickets", "batch").headers["Idempotency-Replayed"] == "true"


@pytest.mark.parametrize(
    "mutation", ["duplicate", "missing", "foreign", "counts", "status", "dry_run"]
)
def test_partial_receipt_must_account_for_every_requested_item(mutation):
    body = {
        "operation": "ticket.assign_tickets",
        "dry_run": False,
        "result": {
            "items": [{"id": "a", "status": "assigned"}, {"id": "b", "status": "failed"}],
            "succeeded": 1,
            "failed": 1,
        },
    }
    valid = classify(
        httpx.Response(207, json=body), "ticket.assign_tickets", {"ticket_ids": ["a", "b"]}
    )
    assert valid.status == "partial_success" and not valid.outcome_unknown
    broken = deepcopy(body)
    if mutation == "duplicate":
        broken["result"]["items"][1]["id"] = "a"
    elif mutation == "missing":
        broken["result"]["items"].pop()
    elif mutation == "foreign":
        broken["result"]["items"][1]["id"] = "c"
    elif mutation == "counts":
        broken["result"]["succeeded"] = 2
    elif mutation == "status":
        broken["result"]["items"][0]["status"] = "skipped"
    else:
        broken["dry_run"] = True
    result = classify(
        httpx.Response(207, json=broken), "ticket.assign_tickets", {"ticket_ids": ["a", "b"]}
    )
    assert result.status == "human_required" and result.outcome_unknown


def test_batch_zero_success_is_not_reported_as_partial_success():
    body = {
        "operation": "ticket.assign_tickets",
        "dry_run": False,
        "result": {"items": [{"id": "a", "status": "failed"}], "succeeded": 0, "failed": 1},
    }
    result = classify(
        httpx.Response(207, json=body), "ticket.assign_tickets", {"ticket_ids": ["a"]}
    )
    assert result.status == "compensation_required"
    assert result.code == "batch_all_failed" and not result.outcome_unknown


def test_oversized_paid_plan_and_duplicate_batch_ids_request_clarification():
    facts = OnboardingFacts(
        employees=[{"name": "员工", "username": f"user{i}"} for i in range(7)],
        paid_item="editor",
        unit_price_cents=500,
        assignment_ticket_ids=["t1", "t1"],
    )
    assert any("40" in q for q in facts.questions())
    assert any("重复" in q for q in facts.questions())
