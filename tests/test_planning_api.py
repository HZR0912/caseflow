from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from apps.api.main import app, get_policy_client, get_preview_client
from apps.api.settings import PlanningProvider, get_settings
from core.graph.onboarding import OnboardingFacts, PlanningRequest
from core.graph.providers import FallbackInterpreter
from fastapi.testclient import TestClient

from tests.test_planning import TEXT, TODAY, AllowPolicy, LocalPreview, prepared_store

TOKEN = "planning-test-only"


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv(
        "AUTH_CREDENTIALS",
        json.dumps(
            [
                {
                    "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                    "user_id": "planner",
                    "role": "admin",
                    "tenant": "test",
                }
            ]
        ),
    )
    monkeypatch.setenv("PLANNING_PROVIDERS", "[]")
    get_settings.cache_clear()
    preview = LocalPreview(prepared_store())
    policy = AllowPolicy()
    app.dependency_overrides[get_policy_client] = lambda: policy
    app.dependency_overrides[get_preview_client] = lambda: preview
    try:
        with TestClient(app) as client:
            yield client, preview, policy
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_plan_api_requires_auth_and_forbids_identity_in_body(api):
    client, preview, _ = api
    body = {"text": TEXT, "today": TODAY.isoformat()}
    assert client.post("/v1/plans/preview", json=body).status_code == 401
    headers = {"Authorization": f"Bearer {TOKEN}"}
    for field in ("tenant", "role", "subject", "steps", "dry_run"):
        response = client.post("/v1/plans/preview", headers=headers, json={**body, field: "admin"})
        assert response.status_code == 422
    assert not preview.calls


def test_missing_auth_with_real_preview_dependency(api):
    client, _, _ = api
    del app.dependency_overrides[get_preview_client]
    assert client.post("/v1/plans/preview", json={"text": TEXT}).status_code == 401


def test_plan_api_complete_response_uses_trusted_subject(api):
    client, preview, policy = api
    response = client.post(
        "/v1/plans/preview",
        json={"text": TEXT, "today": TODAY.isoformat()},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "awaiting_approval"
    assert len(result["plan"]["steps"]) == 12 and preview.calls == 1
    assert all(q.subject.id == "planner" and q.subject.tenant == "test" for q in policy.calls)
    assert "张三" not in response.text and "zhangsan" not in response.text


def providers():
    return [
        PlanningProvider(
            name=name, base_url=f"http://{name}.test", model="test-model", api_key="fake"
        )
        for name in ("qwen", "deepseek")
    ]


@pytest.mark.asyncio
async def test_provider_timeout_falls_back_to_second_provider_and_masks_pii():
    calls = []

    def respond(req):
        calls.append(req)
        if req.url.host == "qwen.test":
            raise httpx.ReadTimeout("test timeout", request=req)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"role":"前端"}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        interpreter = FallbackInterpreter(providers(), client)
        facts = await interpreter.understand(PlanningRequest(text=TEXT, today=TODAY))
    assert len(facts.employees) == 3 and interpreter.name == "deepseek"
    assert len(calls) == 2
    for req in calls:
        prompt = json.loads(req.content)["messages"][1]["content"]
        assert "张三" not in prompt and "zhangsan" not in prompt
        assert "[PERSON_1]" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"permission":"admin"}',
        '{"role":"编造部门"}',
        '{"employees":[{"name":"陌生人","username":"invented"}]}',
    ],
)
async def test_model_cannot_invent_facts_or_escalate_permission(content):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        )
    ) as client:
        interpreter = FallbackInterpreter(providers(), client)
        facts = await interpreter.understand(PlanningRequest(text=TEXT, today=TODAY))
    assert facts.permission == "read" and facts.role == "前端"
    assert facts.employees[0].username == "zhangsan"


@pytest.mark.asyncio
async def test_model_cannot_override_explicitly_confirmed_fields():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, json={"choices": [{"message": {"content": '{"permission":"write"}'}}]}
            )
        )
    ) as client:
        interpreter = FallbackInterpreter(providers(), client)
        facts = await interpreter.understand(
            PlanningRequest(
                text=TEXT + " 原计划是 write，现已改为只读",
                today=TODAY,
                facts=OnboardingFacts(permission="read"),
            )
        )
    assert facts.permission == "read"


@pytest.mark.asyncio
async def test_model_cannot_replace_parsed_permission_with_mentioned_admin():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, json={"choices": [{"message": {"content": '{"permission":"admin"}'}}]}
            )
        )
    ) as client:
        facts = await FallbackInterpreter(providers(), client).understand(
            PlanningRequest(text=TEXT + " 请管理员检查计划。", today=TODAY)
        )
    assert facts.permission == "read"
