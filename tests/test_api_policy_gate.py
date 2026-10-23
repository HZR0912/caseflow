"""API 层单测：验证"策略拒绝即 403"这条门是通的。

用 respx 打桩 OPA，不需要真的起容器 —— 这类测试在 CI 里必须零外部依赖。
"""

from __future__ import annotations

import httpx
import respx
from apps.api.main import app
from fastapi.testclient import TestClient

OPA = "http://127.0.0.1:8181"
PATH = "/v1/data/caseflow/authz"

INTERN_GRANTS_ADMIN = {
    "user_id": "u_3",
    "role": "intern",
    "action": "admin.grant",
    "resource_type": "account",
}


def test_healthz() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@respx.mock
def test_execute_action_forbidden_when_policy_denies() -> None:
    respx.post(f"{OPA}{PATH}").mock(
        return_value=httpx.Response(
            200, json={"result": {"allow": False, "reason": "self_escalation_forbidden"}}
        )
    )
    with TestClient(app) as client:
        response = client.post("/v1/actions/execute", json=INTERN_GRANTS_ADMIN)

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "self_escalation_forbidden"


@respx.mock
def test_execute_action_allowed_reaches_executor_gap() -> None:
    """放行后落到 501：W1 只证明门是通的，执行器在 W7。"""
    respx.post(f"{OPA}{PATH}").mock(
        return_value=httpx.Response(
            200, json={"result": {"allow": True, "reason": "allowed_by_role"}}
        )
    )
    payload = {**INTERN_GRANTS_ADMIN, "role": "admin"}
    with TestClient(app) as client:
        response = client.post("/v1/actions/execute", json=payload)

    assert response.status_code == 501


@respx.mock
def test_policy_check_returns_reason() -> None:
    respx.post(f"{OPA}{PATH}").mock(
        return_value=httpx.Response(
            200, json={"result": {"allow": False, "reason": "role_not_permitted"}}
        )
    )
    with TestClient(app) as client:
        response = client.post("/v1/policy/check", json=INTERN_GRANTS_ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["allow"] is False
    assert body["reason"] == "role_not_permitted"
