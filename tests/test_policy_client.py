"""策略客户端单测：不依赖真实 OPA，用 respx 拦截 HTTP。

覆盖的三种情况正好对应生产里的三个真实故障：
1. 正常放行 / 正常拒绝
2. OPA 500 → 必须拒绝（fail-closed）
3. OPA 超时 → 必须拒绝，且标记 degraded
4. 策略 undefined（result 为空）→ 必须拒绝
"""

from __future__ import annotations

import httpx
import pytest
import respx
from core.policy.opa_client import (
    PolicyClient,
    PolicyQuery,
    PolicyResource,
    PolicySubject,
)

OPA = "http://opa.test"
PATH = "/v1/data/caseflow/authz/decision"


def make_query() -> PolicyQuery:
    return PolicyQuery(
        subject=PolicySubject(id="u_1", role="intern"),
        action="admin.grant",
        resource=PolicyResource(type="account", id="acc_1"),
    )


def test_opa_input_shape_matches_rego() -> None:
    payload = make_query().to_opa_input()
    assert payload == {
        "user": {"id": "u_1", "role": "intern", "tenant": "default"},
        "action": "admin.grant",
        "resource": {"type": "account", "id": "acc_1"},
    }


@respx.mock
async def test_denied_by_policy() -> None:
    respx.post(f"{OPA}{PATH}").mock(
        return_value=httpx.Response(
            200, json={"result": {"allow": False, "reason": "role_not_permitted"}}
        )
    )
    client = PolicyClient(OPA)
    try:
        decision = await client.authorize(make_query())
    finally:
        await client.aclose()

    assert decision.allow is False
    assert decision.reason == "role_not_permitted"
    assert decision.degraded is False


@respx.mock
async def test_allowed_by_policy() -> None:
    respx.post(f"{OPA}{PATH}").mock(
        return_value=httpx.Response(
            200, json={"result": {"allow": True, "reason": "allowed_by_role"}}
        )
    )
    client = PolicyClient(OPA)
    try:
        decision = await client.authorize(make_query())
    finally:
        await client.aclose()

    assert decision.allow is True
    assert decision.reason == "allowed_by_role"


@respx.mock
async def test_opa_500_fails_closed() -> None:
    respx.post(f"{OPA}{PATH}").mock(return_value=httpx.Response(500))
    client = PolicyClient(OPA)
    try:
        decision = await client.authorize(make_query())
    finally:
        await client.aclose()

    assert decision.allow is False
    assert decision.degraded is True
    assert decision.reason.startswith("policy_engine_unavailable")


@respx.mock
async def test_opa_timeout_fails_closed() -> None:
    respx.post(f"{OPA}{PATH}").mock(side_effect=httpx.ConnectTimeout("boom"))
    client = PolicyClient(OPA)
    try:
        decision = await client.authorize(make_query())
    finally:
        await client.aclose()

    assert decision.allow is False
    assert decision.degraded is True


@respx.mock
async def test_undefined_policy_fails_closed() -> None:
    respx.post(f"{OPA}{PATH}").mock(return_value=httpx.Response(200, json={}))
    client = PolicyClient(OPA)
    try:
        decision = await client.authorize(make_query())
    finally:
        await client.aclose()

    assert decision.allow is False
    assert decision.reason == "policy_undefined"


@pytest.mark.parametrize("status_code", [401, 403])
@respx.mock
async def test_opa_auth_errors_fail_closed(status_code: int) -> None:
    respx.post(f"{OPA}{PATH}").mock(return_value=httpx.Response(status_code))
    client = PolicyClient(OPA)
    try:
        decision = await client.authorize(make_query())
    finally:
        await client.aclose()

    assert decision.allow is False
    assert decision.degraded is True


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"",
        b"[]",
        b"null",
        b'{"result": null}',
        b'{"result": []}',
        b'{"result": "allowed"}',
        b'{"result": {}}',
        b'{"result": {"allow": "false", "reason": "bad"}}',
        b'{"result": {"allow": "true", "reason": "bad"}}',
        b'{"result": {"allow": 1, "reason": "bad"}}',
        b'{"result": {"allow": 0, "reason": "bad"}}',
        b'{"result": {"allow": null, "reason": "bad"}}',
        b'{"result": {"reason": "bad"}}',
        b'{"result": {"allow": true}}',
        b'{"result": {"allow": true, "reason": 42}}',
        b'{"result": {"allow": true, "reason": ""}}',
        b'{"result": {"allow": true, "reason": "ok", "unexpected": true}}',
    ],
)
@respx.mock
async def test_malformed_response_fails_closed(body: bytes) -> None:
    respx.post(f"{OPA}{PATH}").mock(return_value=httpx.Response(200, content=body))
    client = PolicyClient(OPA)
    try:
        decision = await client.authorize(make_query())
    finally:
        await client.aclose()

    assert decision.allow is False
    assert decision.degraded is True
    assert decision.reason == "policy_engine_invalid_response"
