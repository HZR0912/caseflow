import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from core.graph.contracts import Plan, PlanStep
from core.tools.execution import RetryPolicy, idempotency_key
from core.tools.gateway import HttpToolGateway, classify, retry_after
from core.tools.payload import PlanCipher
from cryptography.fernet import Fernet


def user_plan():
    return Plan(
        steps=[
            PlanStep(
                id="user",
                operation="identity.create_user",
                arguments={"username": "alice", "display_name": "张三"},
                risk="medium",
                reversibility="compensable",
                rollback="disable",
            )
        ]
    )


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (429, "injected_429", "retryable"),
        (408, "", "retryable"),
        (500, "", "retryable"),
        (502, "", "retryable"),
        (503, "", "retryable"),
        (504, "injected_timeout_after_commit", "retryable"),
        (400, "", "human_required"),
        (401, "", "human_required"),
        (403, "", "human_required"),
        (422, "", "human_required"),
        (409, "idempotency_key_conflict", "human_required"),
        (403, "domain_policy", "compensation_required"),
        (409, "seat_limit", "compensation_required"),
        (409, "duplicate_name", "compensation_required"),
        (409, "insufficient_funds", "compensation_required"),
        (207, "", "human_required"),
        (302, "", "human_required"),
    ],
)
def test_failure_classes(status, code, expected):
    response = httpx.Response(status, json={"error": {"code": code, "retryable": True}})
    assert classify(response, "identity.create_user").status == expected


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"result": {"id": "u1"}},
        {"error": {"code": []}},
        {"operation": "identity.create_user", "dry_run": True, "result": {"id": "u1"}},
    ],
)
def test_untrusted_receipts_never_mark_success(body):
    result = classify(httpx.Response(200, json=body), "identity.create_user")
    assert result.status == "human_required" and result.outcome_unknown


def test_retry_after_and_backoff_bounds():
    assert retry_after("3") == 3
    assert retry_after("-2") == retry_after("nan") == retry_after("garbage") == 0
    assert 58 <= retry_after(format_datetime(datetime.now(UTC) + timedelta(seconds=60))) <= 60
    policy = RetryPolicy()
    for attempt in range(1, 8):
        ceiling = min(8, 0.5 * 2 ** (attempt - 1))
        assert ceiling * 0.75 <= policy.delay(attempt) <= ceiling
    assert policy.delay(1, 30) == 30
    assert (
        classify(httpx.Response(429, headers={"Retry-After": "1e100"}), "identity.create_user").code
        == "retry_after_too_large"
    )


def test_key_is_stable_and_scoped_to_tenant_plan_step_target():
    step = user_plan().steps[0]
    args = step.arguments
    key = idempotency_key("t", "r", "p", step, args)
    assert key == idempotency_key("t", "r", "p", step, dict(reversed(list(args.items()))))
    assert (
        len(
            {
                key,
                idempotency_key("other", "r", "p", step, args),
                idempotency_key("t", "other", "p", step, args),
                idempotency_key("t", "r", "other", step, args),
                idempotency_key("t", "r", "p", step.model_copy(update={"id": "other"}), args),
                idempotency_key("t", "r", "p", step, {**args, "username": "bob"}),
            }
        )
        == 6
    )


def test_encryption_authenticates_plan_and_context():
    key = Fernet.generate_key().decode()
    cipher = PlanCipher(key)
    token = cipher.seal(user_plan(), "t", "r")
    assert "alice" not in token and "张三" not in token
    assert PlanCipher(key).open(token, "t", "r") == user_plan()
    for tenant, request in [("x", "r"), ("t", "x")]:
        with pytest.raises(ValueError, match="execution_payload_invalid"):
            cipher.open(token, tenant, request)
    with pytest.raises(ValueError):
        PlanCipher(Fernet.generate_key().decode()).open(token, "t", "r")


async def test_http_adapter_reuses_key_and_handles_transport_failure():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("secret content must not enter journal")
        return httpx.Response(
            201,
            json={
                "operation": "identity.create_user",
                "dry_run": False,
                "result": {"id": "u1", "display_name": "张三"},
            },
            headers={"Idempotency-Replayed": "true"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = HttpToolGateway("http://mock", "Bearer test", client)
        step = user_plan().steps[0]
        first = await gateway.call(step, step.arguments, "stable")
        second = await gateway.call(step, step.arguments, "stable")
    assert first.status == "retryable" and first.outcome_unknown
    assert second.status == "succeeded" and second.replayed
    assert [r.headers["Idempotency-Key"] for r in calls] == ["stable", "stable"]
    assert json.loads(calls[0].content) == step.arguments
    assert "张三" not in str(second)


def test_execution_http_requires_authentication():
    from apps.api.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/approval-requests/00000000-0000-0000-0000-000000000001/execute"
            ).status_code
            == 401
        )
