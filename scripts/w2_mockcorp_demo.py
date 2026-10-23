"""W2 真实 HTTP 验收；需独立演示租户、默认模拟额度和已开启故障注入。"""

from __future__ import annotations

import argparse
import os
from uuid import uuid4

import httpx


def demonstrate(client: httpx.Client) -> None:
    prefix = uuid4().hex[:10]
    operations = set()

    def state():
        response = client.get("/v1/state")
        response.raise_for_status()
        return response.json()

    def call(path, body, *, key=None, params=None, method="POST", expected=201):
        response = client.request(
            method,
            path,
            json=body,
            headers={"Idempotency-Key": key} if key else {},
            params=params,
        )
        assert response.status_code == expected, (path, response.status_code, response.text)
        return response

    def perform(path, body, *, method="POST", expected=201):
        key = prefix + ":" + uuid4().hex
        before = state()
        preview = call(path, body, params={"dry_run": "true"}, method=method, expected=200)
        assert preview.json()["dry_run"] is True
        assert state() == before, "dry-run changed business state"
        first = call(path, body, key=key, method=method, expected=expected)
        after = state()
        replay = call(path, body, key=key, method=method, expected=expected)
        assert replay.headers["idempotency-replayed"] == "true"
        assert replay.json() == first.json()
        assert state() == after, "replay changed business state"
        operations.add(first.json()["operation"])
        return first.json()["result"]

    user = perform("/v1/identity/users", {"username": "demo_" + prefix, "display_name": "W2 Demo"})[
        "id"
    ]
    perform("/v1/identity/mailboxes", {"user_id": user, "email": f"{prefix}@example.com"})
    perform(
        f"/v1/identity/users/{user}/password",
        {"new_password": "Mock-only-" + uuid4().hex},
        expected=200,
    )
    repo = perform("/v1/code/repos", {"name": "repo_" + prefix})["id"]
    perform(f"/v1/code/repos/{repo}/members", {"user_id": user})
    perform(
        f"/v1/code/repos/{repo}/members/{user}",
        {"permission": "write"},
        method="PATCH",
        expected=200,
    )
    project = perform("/v1/ticket/projects", {"key": "D" + prefix.upper(), "name": "W2"})["id"]
    tickets = [
        perform("/v1/ticket/tickets", {"project_id": project, "title": f"Task {i}"})["id"]
        for i in range(2)
    ]
    perform("/v1/ticket/tickets/assign", {"ticket_ids": tickets, "user_id": user}, expected=200)
    order = perform(
        "/v1/money/orders", {"item": "demo_" + prefix, "quantity": 1, "unit_price_cents": 100}
    )["id"]
    perform("/v1/money/seats", {"order_id": order, "user_id": user})
    perform("/v1/money/refunds", {"order_id": order, "amount_cents": 100})
    print("PASS: 12 operations preview, commit and replay; preview/replay leave state unchanged")

    before = state()
    body = {"name": "rate_" + prefix}
    key = prefix + ":rate"
    limited = call("/v1/code/repos", body, key=key, params={"fault": "429"}, expected=429)
    assert limited.headers["retry-after"] == "1"
    assert state() == before
    success = call("/v1/code/repos", body, key=key)
    replay = call("/v1/code/repos", body, key=key)
    assert success.json() == replay.json()
    call("/v1/code/repos", {"name": "different"}, key=key, expected=409)
    print("PASS: 429 has no effect; same-key retry succeeds; changed payload returns 409")

    extra = [
        perform("/v1/ticket/tickets", {"project_id": project, "title": f"Partial {i}"})["id"]
        for i in range(2)
    ]
    body = {"ticket_ids": extra, "user_id": user}
    key = prefix + ":partial"
    first = call(
        "/v1/ticket/tickets/assign",
        body,
        key=key,
        params={"fault": "partial_success"},
        expected=207,
    )
    after = state()
    replay = call("/v1/ticket/tickets/assign", body, key=key, expected=207)
    assert first.json() == replay.json() and state() == after
    failed = [item["id"] for item in first.json()["result"]["items"] if item["status"] == "failed"]
    assert len(failed) == 1
    perform("/v1/ticket/tickets/assign", {"ticket_ids": failed, "user_id": user}, expected=200)
    print("PASS: 207 preserves partial result; new key retries only failed items")

    before = state()
    key = prefix + ":lost"
    body = {"item": "lost_" + prefix, "quantity": 1, "unit_price_cents": 100}
    call("/v1/money/orders", body, key=key, params={"fault": "timeout_after_commit"}, expected=504)
    after = state()
    assert len(after["orders"]) == len(before["orders"]) + 1
    assert after["balance_cents"] == before["balance_cents"] - 100
    replay = call("/v1/money/orders", body, key=key)
    assert replay.headers["idempotency-replayed"] == "true" and state() == after
    print("PASS: post-commit 504 recovers original order without charging twice")

    perform(f"/v1/identity/users/{user}/disable", {}, expected=200)
    assert len(operations) == 13
    print("PASS: all 13 write operations across Identity, Code, Ticket and Money")
    print("Demo data remains in this tenant; repeat with a fresh tenant for default limits")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    token = os.getenv("MOCKCORP_DEMO_TOKEN")
    if not token:
        parser.error("set MOCKCORP_DEMO_TOKEN to the configured tenant's Bearer token")
    with httpx.Client(
        base_url=args.base_url,
        headers={"Authorization": "Bearer " + token},
        timeout=30,
        trust_env=False,
    ) as client:
        demonstrate(client)


if __name__ == "__main__":
    main()
