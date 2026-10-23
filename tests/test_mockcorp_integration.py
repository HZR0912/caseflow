"""真实 PostgreSQL、并发 HTTP 和进程重启验证；不以测试替身证明持久化。"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import httpx
import psycopg
import pytest
from apps.mockcorp.models import MockSettings
from apps.mockcorp.service import MockCorp
from apps.mockcorp.store import PostgresStore

pytestmark = pytest.mark.integration


@pytest.fixture
def postgres_context():
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required for real PostgreSQL integration")
    settings = MockSettings(_env_file=None, faults_enabled=True, fault_delay_s=0.01)
    store = PostgresStore(url, settings)
    tenant = "mockcorp-test-" + uuid4().hex
    store.health()
    try:
        yield tenant, store, MockCorp(store, settings)
    finally:
        with psycopg.connect(store.url) as conn:
            conn.execute("DELETE FROM mockcorp_oplog WHERE tenant = %s", (tenant,))
            conn.execute("DELETE FROM mockcorp_state WHERE tenant = %s", (tenant,))


def test_concurrent_replay_is_one_committed_effect(postgres_context):
    tenant, store, service = postgres_context
    body = {"item": "editor", "quantity": 1, "unit_price_cents": 100}

    def purchase(_):
        return service.execute(tenant, "money.create_order", body, key="same")

    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(purchase, range(8)))
    assert all(r.status == 201 and r.body == replies[0].body for r in replies)
    assert sum(r.headers["Idempotency-Replayed"] == "false" for r in replies) == 1
    state = store.snapshot(tenant)
    assert len(state["orders"]) == 1
    assert state["balance_cents"] == 99_900
    with psycopg.connect(store.url) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM mockcorp_receipts WHERE tenant=%s", (tenant,)
            ).fetchone()[0]
            == 1
        )


def test_concurrent_different_keys_cannot_overspend(postgres_context):
    tenant, store, service = postgres_context

    def purchase(index):
        return service.execute(
            tenant,
            "money.create_order",
            {"item": "editor", "quantity": 1, "unit_price_cents": 75_000},
            key=str(index),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(purchase, range(2)))
    assert sorted(r.status for r in replies) == [201, 409]
    assert store.snapshot(tenant)["balance_cents"] == 25_000
    assert len(store.snapshot(tenant)["orders"]) == 1


def test_dry_run_and_exception_rollback_leave_no_rows(postgres_context):
    tenant, store, service = postgres_context
    response = service.execute(
        tenant,
        "identity.create_user",
        {"username": "preview", "display_name": "Preview"},
        key="preview",
        dry_run=True,
    )
    assert response.status == 200

    def fail(state, receipt):
        state["balance_cents"] = -100
        raise RuntimeError("failure before commit")

    with pytest.raises(RuntimeError):
        store.transact(tenant, "failure", fail, operation="identity.explode", actor="tester")
    with psycopg.connect(store.url) as conn:
        # 业务与幂等收据必须无残留；日志表则**应当**留下记录（ADR-0004）。
        for table in ("mockcorp_state", "mockcorp_receipts"):
            assert (
                conn.execute(f"SELECT count(*) FROM {table} WHERE tenant=%s", (tenant,)).fetchone()[
                    0
                ]
                == 0
            )
        failures = conn.execute(
            "SELECT operation, outcome, status_code, error_code, actor "
            "FROM mockcorp_oplog WHERE tenant=%s ORDER BY id",
            (tenant,),
        ).fetchall()
    assert failures == [("identity.explode", "error", 500, "internal_error", "tester")]


def test_oplog_persists_across_connections_and_replays(postgres_context):
    tenant, store, service = postgres_context
    body = {"item": "editor", "quantity": 1, "unit_price_cents": 100}
    first = service.execute(tenant, "money.create_order", body, key="log", actor="alice")
    assert first.status == 201
    replay = MockCorp(PostgresStore(store.url, store.settings)).execute(
        tenant, "money.create_order", body, key="log", actor="alice"
    )
    assert replay.headers["Idempotency-Replayed"] == "true"

    page = store.oplog(tenant)
    committed, replayed = page["items"]
    assert committed["outcome"] == "committed" and committed["replayed"] is False
    assert replayed["outcome"] == "committed" and replayed["replayed"] is True
    assert committed["result_id"] == replayed["result_id"]
    assert committed["actor"] == "alice"
    assert page["next_after_id"] is None


def test_partial_success_survives_new_store(postgres_context):
    tenant, store, service = postgres_context

    def perform(operation, payload, key=None, **options):
        return service.execute(tenant, operation, payload, key=key or uuid4().hex, **options)

    user = perform("identity.create_user", {"username": "alice", "display_name": "Alice"}).body[
        "result"
    ]
    project = perform("ticket.create_project", {"key": "TEST", "name": "Test"}).body["result"]
    tickets = [
        perform(
            "ticket.create_ticket",
            {"project_id": project["id"], "title": str(i), "assignee_id": None},
        ).body["result"]
        for i in range(4)
    ]
    body = {"ticket_ids": [t["id"] for t in tickets], "user_id": user["id"]}
    first = perform("ticket.assign_tickets", body, key="partial", fault="partial_success")
    assert first.status == 207
    other = MockCorp(PostgresStore(store.url, store.settings), store.settings)
    replay = other.execute(tenant, "ticket.assign_tickets", body, key="partial")
    assert replay.body == first.body
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert (
        sum(t["assignee_id"] == user["id"] for t in store.snapshot(tenant)["tickets"].values()) == 2
    )


@contextmanager
def live_server(environment):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "apps.mockcorp.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            ready = False
            with httpx.Client(timeout=0.5, trust_env=False) as client:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    try:
                        if client.get(base + "/healthz").status_code == 200:
                            ready = True
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
            if not ready:
                log.seek(0)
                pytest.fail(
                    "MockCorp failed to start: " + log.read().decode(errors="replace")[-2000:]
                )
            yield base
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def test_http_timeout_then_process_restart_replays_original_result(postgres_context):
    tenant, store, _ = postgres_context
    token = uuid4().hex + uuid4().hex
    environment = {
        **os.environ,
        "DATABASE_URL": store.url,
        "AUTH_CREDENTIALS": json.dumps(
            [
                {
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "user_id": "test",
                    "role": "employee",
                    "tenant": tenant,
                }
            ]
        ),
        "MOCKCORP_FAULTS_ENABLED": "true",
        "MOCKCORP_FAULT_DELAY_S": "2",
    }
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "lost-response"}
    body = {"item": "editor", "quantity": 1, "unit_price_cents": 100}
    with (
        live_server(environment) as base,
        httpx.Client(base_url=base, headers=headers, trust_env=False) as client,
    ):
        with pytest.raises(httpx.ReadTimeout):
            client.post("/v1/money/orders?fault=timeout_after_commit", json=body, timeout=0.15)
        # 等待可观测的已提交状态，随后由 context manager 强杀尚未返回响应的服务进程。
        deadline = time.monotonic() + 5
        while not store.snapshot(tenant)["orders"] and time.monotonic() < deadline:
            time.sleep(0.05)
        state = store.snapshot(tenant)
        assert len(state["orders"]) == 1
        original_id = next(iter(state["orders"]))
    with (
        live_server(environment) as base,
        httpx.Client(base_url=base, headers=headers, trust_env=False) as client,
    ):
        response = client.post("/v1/money/orders", json=body)
        assert response.status_code == 201
        assert response.headers["idempotency-replayed"] == "true"
        assert response.json()["result"]["id"] == original_id
        assert len(client.get("/v1/money/orders").json()["items"]) == 1
        assert client.get("/v1/state").json()["balance_cents"] == 99_900
