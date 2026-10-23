from __future__ import annotations

import json
from copy import deepcopy
from uuid import uuid4

import pytest

from tests.conftest import OTHER_TOKEN, write


def cases(seed):
    a, b, c, r, p, t, o = (
        seed[k] for k in ("alice", "bob", "carol", "repo", "project", "tickets", "order")
    )
    return [
        ("POST", "/v1/identity/users", {"username": "dave", "display_name": "Dave"}, 201),
        ("POST", "/v1/identity/mailboxes", {"user_id": b["id"], "email": "bob@example.com"}, 201),
        (
            "POST",
            f"/v1/identity/users/{a['id']}/password",
            {"new_password": "test-password-123"},
            200,
        ),
        ("POST", f"/v1/identity/users/{a['id']}/disable", {}, 200),
        ("POST", "/v1/code/repos", {"name": "backend"}, 201),
        (
            "POST",
            f"/v1/code/repos/{r['id']}/members",
            {"user_id": c["id"], "permission": "write"},
            201,
        ),
        ("PATCH", f"/v1/code/repos/{r['id']}/members/{a['id']}", {"permission": "admin"}, 200),
        ("POST", "/v1/ticket/projects", {"key": "NEW", "name": "New"}, 201),
        (
            "POST",
            "/v1/ticket/tickets",
            {"project_id": p["id"], "title": "New task", "assignee_id": a["id"]},
            201,
        ),
        (
            "POST",
            "/v1/ticket/tickets/assign",
            {"ticket_ids": [x["id"] for x in t], "user_id": a["id"]},
            200,
        ),
        (
            "POST",
            "/v1/money/orders",
            {"item": "editor", "quantity": 1, "unit_price_cents": 500},
            201,
        ),
        ("POST", "/v1/money/seats", {"order_id": o["id"], "user_id": b["id"]}, 201),
        ("POST", "/v1/money/refunds", {"order_id": o["id"], "amount_cents": 200}, 201),
    ]


@pytest.mark.parametrize("index", range(13))
def test_every_write_preview_execute_replay(context, seeded, index):
    client, store, _ = context
    method, path, payload, status = cases(seeded)[index]
    before = store.snapshot("acme")
    receipts = deepcopy(store.receipts)
    key = uuid4().hex
    preview = write(client, path, payload, method=method, key=key, dry_run=True, expected=200)
    assert preview.json()["dry_run"] is True
    assert preview.json()["diff"]
    assert store.snapshot("acme") == before
    assert store.receipts == receipts
    first = write(client, path, payload, method=method, key=key, expected=status)
    after = store.snapshot("acme")
    assert after != before
    replay = write(client, path, payload, method=method, key=key, expected=status)
    assert replay.json() == first.json()
    assert replay.headers["idempotency-replayed"] == "true"
    assert store.snapshot("acme") == after
    assert len(store.receipts) == len(receipts) + 1


@pytest.mark.parametrize("index", range(13))
def test_every_write_transient_failure_has_no_effect_and_can_retry(context, seeded, index):
    client, store, _ = context
    method, path, payload, status = cases(seeded)[index]
    before, receipts = store.snapshot("acme"), deepcopy(store.receipts)
    key = uuid4().hex
    failure = write(client, path, payload, method=method, key=key, fault="500", expected=500)
    assert failure.json()["error"]["retryable"] is True
    assert store.snapshot("acme") == before
    assert store.receipts == receipts
    write(client, path, payload, method=method, key=key, expected=status)


def test_key_conflicts_and_tenant_isolation(context):
    client, store, _ = context
    body = {"username": "alice", "display_name": "Alice"}
    first = write(client, "/v1/identity/users", body, key="same")
    write(
        client, "/v1/identity/users", {**body, "display_name": "changed"}, key="same", expected=409
    )
    write(client, "/v1/code/repos", {"name": "repo"}, key="same", expected=409)
    other = client.post(
        "/v1/identity/users",
        json=body,
        headers={
            "Authorization": f"Bearer {OTHER_TOKEN}",
            "Idempotency-Key": "same",
            "X-Tenant": "acme",
        },
    )
    assert other.status_code == 201
    assert other.json()["result"]["id"] != first.json()["result"]["id"]
    assert len(store.snapshot("acme")["users"]) == len(store.snapshot("other")["users"]) == 1
    assert client.get(
        "/v1/state", headers={"Authorization": f"Bearer {OTHER_TOKEN}"}
    ).json() == store.snapshot("other")


def test_password_never_persisted_and_distinct_passwords_conflict(context, seeded):
    client, store, _ = context
    path = f"/v1/identity/users/{seeded['alice']['id']}/password"
    password = "my-sensitive-test-password"
    response = write(client, path, {"new_password": password}, key="password", expected=200)
    assert password not in response.text
    assert password not in json.dumps(store.snapshot("acme"))
    assert password not in json.dumps(list(store.receipts.values()))
    write(client, path, {"new_password": "a-different-password"}, key="password", expected=409)


def test_partial_success_is_persisted_and_preview_is_read_only(context, seeded):
    client, store, _ = context
    path = "/v1/ticket/tickets/assign"
    ids = [x["id"] for x in seeded["tickets"]]
    body = {"ticket_ids": ids, "user_id": seeded["alice"]["id"]}
    before = store.snapshot("acme")
    write(client, path, body, dry_run=True, fault="partial_success", expected=200)
    assert store.snapshot("acme") == before
    first = write(client, path, body, key="partial", fault="partial_success", expected=207)
    result = first.json()["result"]
    assert result["succeeded"] == result["failed"] == 1
    replay = write(client, path, body, key="partial", expected=207)
    assert replay.json() == first.json()
    assert store.snapshot("acme")["tickets"][ids[1]]["assignee_id"] is None
    write(client, path, {**body, "ticket_ids": [ids[1]]}, expected=200)
    assert all(
        t["assignee_id"] == body["user_id"] for t in store.snapshot("acme")["tickets"].values()
    )


def test_batch_invalid_target_rolls_back_whole_batch(context, seeded):
    client, store, _ = context
    before = store.snapshot("acme")
    write(
        client,
        "/v1/ticket/tickets/assign",
        {"ticket_ids": [seeded["tickets"][0]["id"], "missing"], "user_id": seeded["alice"]["id"]},
        expected=404,
    )
    assert store.snapshot("acme") == before


@pytest.mark.parametrize(
    "fault,status", [("429", 429), ("timeout", 504), ("approval_timeout", 504)]
)
def test_retryable_faults_leave_no_state(context, fault, status):
    client, store, _ = context
    response = write(
        client,
        "/v1/money/orders",
        {"item": "editor", "quantity": 1, "unit_price_cents": 100},
        fault=fault,
        expected=status,
    )
    assert response.json()["error"]["retryable"] is True
    if status == 429:
        assert response.headers["retry-after"] == "1"
    assert store.states == store.receipts == {}


def test_timeout_after_commit_and_replay_precedes_fault(context):
    client, store, settings = context
    body = {"item": "editor", "quantity": 1, "unit_price_cents": 100}
    write(client, "/v1/money/orders", body, key="lost", fault="timeout_after_commit", expected=504)
    assert store.snapshot("acme")["balance_cents"] == settings.initial_balance_cents - 100
    replay = write(client, "/v1/money/orders", body, key="lost", fault="500")
    assert replay.headers["idempotency-replayed"] == "true"
    assert len(store.snapshot("acme")["orders"]) == 1


def test_slow_response_still_commits_once(context, monkeypatch):
    client, store, settings = context
    delays = []
    monkeypatch.setattr("apps.mockcorp.service.time.sleep", delays.append)
    body = {"name": "slow-repo"}
    write(client, "/v1/code/repos", body, key="slow", fault="slow")
    write(client, "/v1/code/repos", body, key="slow", fault="slow")
    assert delays == [settings.fault_delay_s]
    assert len(store.snapshot("acme")["repos"]) == 1


def test_faults_disabled_unsupported_and_preview_after_commit_fault(context):
    client, store, settings = context
    body = {"name": "repo"}
    settings.faults_enabled = False
    write(client, "/v1/code/repos", body, fault="500", expected=403)
    settings.faults_enabled = True
    write(client, "/v1/code/repos", body, fault="partial_success", expected=422)
    write(client, "/v1/code/repos", body, fault="unknown", expected=422)
    write(client, "/v1/code/repos", body, dry_run=True, fault="timeout_after_commit", expected=422)
    assert store.states == store.receipts == {}


def test_business_rules_and_money_accounting(context, seeded):
    client, store, settings = context
    a, b, c, r, _, _, o = (
        seeded[k] for k in ("alice", "bob", "carol", "repo", "project", "tickets", "order")
    )
    write(
        client,
        "/v1/identity/users",
        {"username": "ALICE", "display_name": "Different"},
        expected=409,
    )
    write(
        client,
        "/v1/identity/mailboxes",
        {"user_id": b["id"], "email": "bob@evil.test"},
        expected=403,
    )
    write(client, f"/v1/code/repos/{r['id']}/members", {"user_id": b["id"]}, expected=409)
    write(client, f"/v1/code/repos/{r['id']}/members", {"user_id": c["id"]})
    write(client, "/v1/identity/mailboxes", {"user_id": b["id"], "email": "bob@example.com"})
    write(client, f"/v1/code/repos/{r['id']}/members", {"user_id": b["id"]}, expected=409)
    write(
        client,
        "/v1/money/orders",
        {"item": "expensive", "quantity": 1, "unit_price_cents": settings.initial_balance_cents},
        expected=409,
    )
    write(client, "/v1/money/refunds", {"order_id": o["id"], "amount_cents": 201}, expected=409)
    write(client, "/v1/money/refunds", {"order_id": o["id"], "amount_cents": 50})
    write(client, "/v1/money/refunds", {"order_id": o["id"], "amount_cents": 150})
    state = store.snapshot("acme")
    assert state["balance_cents"] == settings.initial_balance_cents
    assert all(not seat["active"] for seat in state["seats"].values())
    write(client, "/v1/money/refunds", {"order_id": o["id"], "amount_cents": 1}, expected=409)
    write(client, "/v1/money/seats", {"order_id": o["id"], "user_id": b["id"]}, expected=409)
    write(client, f"/v1/identity/users/{a['id']}/disable", {}, expected=200)
    assert not store.snapshot("acme")["users"][a["id"]]["active"]
    assert all(
        not m["active"]
        for m in store.snapshot("acme")["mailboxes"].values()
        if m["user_id"] == a["id"]
    )
    write(
        client,
        f"/v1/identity/users/{a['id']}/password",
        {"new_password": "another-test-password"},
        expected=409,
    )


@pytest.mark.parametrize(
    "index,fault,status",
    [
        (0, "duplicate_name", 409),
        (1, "domain_policy", 403),
        (5, "seat_limit", 409),
        (5, "invite_rate_limit", 429),
        (10, "insufficient_funds", 409),
    ],
)
def test_specific_faults_are_reproducible_without_changes(context, seeded, index, fault, status):
    client, store, _ = context
    method, path, body, _ = cases(seeded)[index]
    before = store.snapshot("acme")
    write(client, path, body, method=method, fault=fault, expected=status)
    assert store.snapshot("acme") == before


def test_auth_validation_and_reads(context):
    client, store, _ = context
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/state", headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.post("/v1/code/repos", json={"name": "repo"}).status_code == 400
    write(client, "/v1/code/repos", {"name": "repo", "tenant": "other"}, expected=422)
    write(
        client,
        "/v1/money/orders",
        {"item": "bad", "quantity": True, "unit_price_cents": 1},
        expected=422,
    )
    write(
        client,
        "/v1/money/orders",
        {"item": "bad", "quantity": 1, "unit_price_cents": -1},
        expected=422,
    )
    assert store.states == {}
    result = write(client, "/v1/code/repos", {"name": "repo"}).json()["result"]
    assert client.get("/v1/code/repos").json()["items"] == [result]
    assert client.get(f"/v1/code/repos/{result['id']}").json() == result
    assert client.get("/v1/identity/repos").status_code == 404
    assert client.get("/v1/code/repos/missing").status_code == 404
