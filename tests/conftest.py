"""MockCorp 测试的共享夹具与请求辅助。

`context` / `seeded` / `write` 被 test_mockcorp.py 与 test_mockcorp_oplog.py 共用，
所以放在 conftest 里。单测用 MemoryStore 替身；持久化与并发保证另由
test_mockcorp_integration.py 用真实 PostgreSQL 验证。
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from apps.api.settings import Settings, get_settings
from apps.mockcorp.main import create_app
from apps.mockcorp.models import MockSettings
from fastapi.testclient import TestClient

from tests.mockcorp_support import MemoryStore

TOKEN = "unit-test-only-token"
OTHER_TOKEN = "unit-test-other-tenant"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def context():
    settings = MockSettings(
        _env_file=None, faults_enabled=True, fault_delay_s=0.001, code_seat_limit=2
    )
    store = MemoryStore(settings)
    app = create_app(store, settings)
    identities = [
        {
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "user_id": "operator",
            "role": "employee",
            "tenant": tenant,
        }
        for token, tenant in [(TOKEN, "acme"), (OTHER_TOKEN, "other")]
    ]
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, auth_credentials=identities
    )
    with TestClient(app, headers=HEADERS) as client:
        yield client, store, settings


def write(client, path, body, *, key=None, method="POST", expected=201, **params):
    response = client.request(
        method, path, json=body, headers={"Idempotency-Key": key or uuid4().hex}, params=params
    )
    assert response.status_code == expected, response.text
    return response


@pytest.fixture
def seeded(context):
    client, _, _ = context
    alice = write(
        client, "/v1/identity/users", {"username": "alice", "display_name": "Alice"}
    ).json()["result"]
    bob = write(client, "/v1/identity/users", {"username": "bob", "display_name": "Bob"}).json()[
        "result"
    ]
    carol = write(
        client, "/v1/identity/users", {"username": "carol", "display_name": "Carol"}
    ).json()["result"]
    for user in [alice, carol]:
        write(
            client,
            "/v1/identity/mailboxes",
            {"user_id": user["id"], "email": user["username"] + "@example.com"},
        )
    repo = write(client, "/v1/code/repos", {"name": "frontend"}).json()["result"]
    write(client, f"/v1/code/repos/{repo['id']}/members", {"user_id": alice["id"]})
    project = write(client, "/v1/ticket/projects", {"key": "ONBOARD", "name": "Onboarding"}).json()[
        "result"
    ]
    tickets = [
        write(
            client, "/v1/ticket/tickets", {"project_id": project["id"], "title": f"Setup {i}"}
        ).json()["result"]
        for i in range(2)
    ]
    order = write(
        client, "/v1/money/orders", {"item": "design", "quantity": 2, "unit_price_cents": 100}
    ).json()["result"]
    write(client, "/v1/money/seats", {"order_id": order["id"], "user_id": alice["id"]})
    return {
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "repo": repo,
        "project": project,
        "tickets": tickets,
        "order": order,
    }
