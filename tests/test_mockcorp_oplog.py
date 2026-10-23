"""操作日志：每一次进入事务的写尝试都应留下一条，且能与业务状态交叉核对。

这组测试的存在理由——日志如果不是每次都写，就无法承担"Agent 自报 vs 系统实际"
的交叉验证职责；而交叉验证是 CaseFlow「可信」卖点唯一硬的证据形态。
"""

from __future__ import annotations

from uuid import uuid4

from tests.test_mockcorp import OTHER_TOKEN, write


def oplog(client, **params):
    response = client.get("/v1/oplog", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_committed_write_is_logged_with_result_id(context):
    client, _, _ = context
    created = write(client, "/v1/code/repos", {"name": "frontend"}, key="log-commit").json()[
        "result"
    ]

    entry = oplog(client)["items"][-1]
    assert entry["operation"] == "code.create_repo"
    assert entry["outcome"] == "committed"
    assert entry["status_code"] == 201
    assert entry["result_id"] == created["id"]
    assert entry["actor"] == "operator"
    assert entry["dry_run"] is False
    assert entry["replayed"] is False
    assert entry["idempotency_key"] == "log-commit"


def test_dry_run_is_logged_but_does_not_touch_state(context):
    client, store, _ = context
    before = store.snapshot("acme")
    write(client, "/v1/code/repos", {"name": "preview"}, dry_run=True, expected=200)

    entry = oplog(client)["items"][-1]
    assert entry["outcome"] == "dry_run"
    assert entry["dry_run"] is True
    assert entry["status_code"] == 200
    assert entry["result_id"] is not None
    assert store.snapshot("acme") == before


def test_replay_is_logged_with_flag(context):
    client, _, _ = context
    body = {"name": "replayed"}
    write(client, "/v1/code/repos", body, key="log-replay")
    write(client, "/v1/code/repos", body, key="log-replay")

    entries = oplog(client)["items"]
    first, second = entries[-2:]
    assert first["outcome"] == "committed" and first["replayed"] is False
    assert second["outcome"] == "committed" and second["replayed"] is True
    assert first["result_id"] == second["result_id"]


def test_business_rejection_is_logged_with_error_code(context):
    client, _, _ = context
    # 金额合法但超过余额：通过 Pydantic、进入事务、被业务规则拒绝（409）。
    # 注意 422 是请求体校验失败，在进事务前就被拒，不会出现在日志里（见 ADR-0004 的边界说明）。
    write(
        client,
        "/v1/money/orders",
        {"item": "editor", "quantity": 1, "unit_price_cents": 99_999_999},
        expected=409,
    )

    entry = oplog(client)["items"][-1]
    assert entry["outcome"] == "rejected"
    assert entry["status_code"] == 409
    assert entry["error_code"] == "insufficient_funds"
    assert entry["result_id"] is None


def test_transient_fault_is_logged_and_retry_still_succeeds(context):
    client, _, _ = context
    body = {"name": "rate_limited"}
    key = uuid4().hex
    write(client, "/v1/code/repos", body, key=key, fault="429", expected=429)
    write(client, "/v1/code/repos", body, key=key, expected=201)

    failed, retried = oplog(client)["items"][-2:]
    assert failed["outcome"] == "rejected"
    assert failed["error_code"] == "injected_429"
    assert failed["status_code"] == 429
    assert retried["outcome"] == "committed"


def test_oplog_cursor_is_monotonic_and_incremental(context):
    client, _, _ = context
    for index in range(3):
        write(client, "/v1/code/repos", {"name": f"repo-{index}"})

    page = oplog(client, limit=2)
    assert len(page["items"]) == 2
    assert page["next_after_id"] == page["items"][-1]["id"]

    rest = oplog(client, after_id=page["next_after_id"])
    assert len(rest["items"]) == 1
    assert rest["next_after_id"] is None
    assert rest["items"][0]["id"] > page["items"][-1]["id"]


def test_oplog_is_tenant_scoped(context):
    client, _, _ = context
    write(client, "/v1/code/repos", {"name": "only-acme"})

    other = client.get("/v1/oplog", headers={"Authorization": f"Bearer {OTHER_TOKEN}"})
    assert other.json()["items"] == []
    assert len(oplog(client)["items"]) >= 1


def test_oplog_does_not_leak_password(context, seeded):
    client, _, _ = context
    password = "another-sensitive-password"
    write(
        client,
        f"/v1/identity/users/{seeded['alice']['id']}/password",
        {"new_password": password},
        key="password-log",
        expected=200,
    )
    assert password not in client.get("/v1/oplog").text


def test_oplog_records_actor_from_credential_not_request(context):
    """actor 只能来自服务端凭据映射，请求体里的任何字段都不能改写它。"""
    client, _, _ = context
    write(client, "/v1/code/repos", {"name": "actor-check"})
    entry = oplog(client)["items"][-1]
    assert entry["actor"] == "operator"
