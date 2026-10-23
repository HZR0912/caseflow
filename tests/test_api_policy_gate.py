"""走真实鉴权依赖，仅打桩 OPA；确认身份不可从请求体伪造。"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
import respx
from apps.api.main import app
from apps.api.settings import Settings, get_settings
from fastapi.testclient import TestClient

OPA = "http://opa.test"
PATH = "/v1/data/caseflow/authz/decision"
ENDPOINTS = ["/v1/actions/execute", "/v1/policy/check"]
INTERN_TOKEN = "test-only-intern-token"
ADMIN_TOKEN = "test-only-admin-token"
PAYLOAD = {"action": "admin.grant", "resource_type": "account"}


def credential(token: str, user_id: str, role: str, tenant: str) -> dict:
    return {
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "user_id": user_id,
        "role": role,
        "tenant": tenant,
    }


@pytest.fixture(autouse=True)
def auth_settings(monkeypatch):
    monkeypatch.setenv("OPA_URL", OPA)
    monkeypatch.setenv(
        "AUTH_CREDENTIALS",
        json.dumps(
            [
                credential(INTERN_TOKEN, "u_intern", "intern", "tenant_a"),
                credential(ADMIN_TOKEN, "u_admin", "admin", "tenant_b"),
            ]
        ),
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def auth_header(token=INTERN_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_healthz() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    "headers",
    [{}, auth_header("unknown"), {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}],
)
@respx.mock
def test_invalid_identity_never_reaches_opa(endpoint, headers) -> None:
    with TestClient(app) as client:
        response = client.post(endpoint, json=PAYLOAD, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert not respx.calls


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@respx.mock
def test_empty_credentials_fail_closed(endpoint, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_CREDENTIALS", "[]")
    get_settings.cache_clear()
    with TestClient(app) as client:
        response = client.post(endpoint, json=PAYLOAD, headers=auth_header())
    assert response.status_code == 401
    assert not respx.calls


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize(
    "field,value", [("role", "admin"), ("user_id", "u_admin"), ("tenant", "tenant_b")]
)
@respx.mock
def test_self_reported_identity_is_rejected(endpoint, field, value) -> None:
    with TestClient(app) as client:
        response = client.post(endpoint, json={**PAYLOAD, field: value}, headers=auth_header())
    assert response.status_code == 422
    assert not respx.calls


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@respx.mock
def test_server_identity_is_sent_to_opa(endpoint) -> None:
    route = respx.post(f"{OPA}{PATH}").mock(
        return_value=httpx.Response(
            200, json={"result": {"allow": False, "reason": "self_escalation_forbidden"}}
        )
    )
    with TestClient(app) as client:
        response = client.post(
            endpoint,
            json=PAYLOAD,
            headers={
                **auth_header(),
                "X-User-Id": "u_admin",
                "X-Role": "admin",
                "X-Tenant": "tenant_b",
            },
        )
    assert response.status_code == (403 if endpoint.endswith("execute") else 200)
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content)["input"]["user"] == {
        "id": "u_intern",
        "role": "intern",
        "tenant": "tenant_a",
    }
    body = response.json()
    decision = body["detail"] if response.status_code == 403 else body
    assert decision["reason"] == "self_escalation_forbidden"


@respx.mock
def test_valid_admin_reaches_executor_gap() -> None:
    route = respx.post(f"{OPA}{PATH}").mock(
        return_value=httpx.Response(
            200, json={"result": {"allow": True, "reason": "allowed_by_role"}}
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/actions/execute", json=PAYLOAD, headers=auth_header(ADMIN_TOKEN)
        )
    assert response.status_code == 501
    assert json.loads(route.calls.last.request.content)["input"]["user"] == {
        "id": "u_admin",
        "role": "admin",
        "tenant": "tenant_b",
    }


@pytest.mark.parametrize("body", [b"not json", b'{"result":{"allow":"false","reason":"bad"}}'])
@respx.mock
def test_invalid_opa_response_blocks_execution(body) -> None:
    respx.post(f"{OPA}{PATH}").mock(return_value=httpx.Response(200, content=body))
    with TestClient(app) as client:
        response = client.post("/v1/actions/execute", json=PAYLOAD, headers=auth_header())
    assert response.status_code == 403
    assert response.json()["detail"]["degraded"] is True
    assert response.json()["detail"]["reason"] == "policy_engine_invalid_response"


def test_duplicate_token_mapping_is_invalid() -> None:
    identities = [
        credential(INTERN_TOKEN, "a", "intern", "a"),
        credential(INTERN_TOKEN, "b", "admin", "b"),
    ]
    with pytest.raises(ValueError, match="duplicate token digests"):
        Settings(_env_file=None, auth_credentials=identities)


@pytest.mark.parametrize("changes", [{"token_sha256": "password"}, {"role": ""}, {"tenant": ""}])
def test_invalid_identity_configuration_rejected(changes) -> None:
    identity = {**credential(INTERN_TOKEN, "a", "intern", "a"), **changes}
    with pytest.raises(ValueError):
        Settings(_env_file=None, auth_credentials=[identity])
