"""W8: real HTTP/OPA/Postgres Saga, partial batches and compensation crash recovery."""

import argparse
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg

from scripts.w7_execution_demo import run


def scenario(
    *,
    args,
    request,
    seed,
    execute,
    repo,
    project,
    start_mock,
    start_api,
    api,
    mock,
    database_url,
    tenant,
    tokens,
    api_url,
    client,
):
    checks, evidence = [], {}

    def faults(values, delay="0.05"):
        nonlocal mock
        mock.terminate()
        mock.wait(timeout=10)
        mock = start_mock(values, delay)

    def state():
        with psycopg.connect(database_url) as conn:
            return conn.execute(
                "SELECT state FROM mockcorp_state WHERE tenant=%s", (tenant,)
            ).fetchone()[0]

    def submit(suffix, batch=None, approve=True):
        row = request(
            "POST",
            "/v1/approval-requests",
            body={
                "text": "请为一位新员工准备入职环境。",
                "today": "2026-10-24",
                "facts": {
                    "employees": [{"name": "测试员工", "username": "saga" + suffix}],
                    "quantity": 1,
                    "role": "developer",
                    "start_date": "2026-10-26",
                    "end_date": "2026-12-31",
                    "email_domain": "example.com",
                    "repo_id": repo,
                    "project_id": project,
                    "permission": "read",
                    **(
                        {"assignment_ticket_ids": batch}
                        if batch
                        else {
                            "paid_item": "editor",
                            "unit_price_cents": 500,
                        }
                    ),
                },
            },
            **{"Idempotency-Key": str(uuid4())},
        )
        assert row["status"] == "awaiting_approval", row
        assert row["snapshot"]["plan"]["saga_version"] == 1
        if approve:
            request(
                "POST",
                f"/v1/approval-requests/{row['id']}/decision",
                actor="reviewer",
                body={
                    "decision": "approve",
                    "plan_hash": row["plan_hash"],
                    "operation_id": str(uuid4()),
                },
            )
        return row

    faults({"/v1/money/orders": ["insufficient_funds"]})
    if args.serve:
        submit("interactive", approve=False)
        print(
            f"W8 审批与补偿演示：{api_url}/approvals\n"
            f"申请人凭据：{tokens['requester']}\n审批人凭据：{tokens['reviewer']}\n"
            "批准并执行后，余额不足将触发成员撤回，保留账号和邮箱。Ctrl+C 清理。",
            flush=True,
        )
        while True:
            time.sleep(1)

    first = submit("funds")
    result = execute(first)
    assert result["status"] == "partial_success"
    assert result["summary"]["retained"] == ["p1_user", "p1_mail"]
    assert result["summary"]["compensated"] == ["p1_invite"]
    current = state()
    assert not current["members"] and not current["orders"]
    assert all(u["active"] for u in current["users"].values())
    assert all(m["active"] for m in current["mailboxes"].values())
    assert execute(first) == result
    checks += [
        "insufficient_funds_precise_membership_undo",
        "identity_retained",
        "terminal_replay_no_calls",
    ]
    evidence["funds"] = result
    print("[funds] 成员已撤回，账号和邮箱保留。", flush=True)

    # Exhausted 429 is known not committed: safely compensate the preceding paid steps.
    faults(
        {"/v1/ticket/tickets": ["429"] * 4, "/v1/compensations": [None, "timeout_after_commit"]},
        "6",
    )
    second = submit("crash")
    pool = ThreadPoolExecutor(max_workers=1)
    pending = pool.submit(
        client.post,
        f"{api_url}/v1/approval-requests/{second['id']}/execute",
        headers={"Authorization": "Bearer " + tokens["requester"]},
    )
    committed, key = False, None
    try:
        for _ in range(400):
            with psycopg.connect(database_url) as conn:
                saved = conn.execute(
                    "SELECT journal FROM execution_runs WHERE request_id=%s", (second["id"],)
                ).fetchone()
                items = saved[0].get("saga", {}).get("steps", []) if saved else []
                item = next((i for i in items if i["step_id"] == "p1_order"), None)
                key = item["key"] if item else None
                committed = (
                    key
                    and conn.execute(
                        "SELECT count(*) FROM mockcorp_receipts WHERE tenant=%s AND key=%s",
                        (tenant, key),
                    ).fetchone()[0]
                )
            if committed:
                break
            time.sleep(0.05)
        assert committed, "refund commit was not observed"
        old_pid = api.pid
        api.kill()
        api.wait(timeout=10)
        with suppress(httpx.HTTPError):
            response = pending.result(timeout=10)
            assert response.status_code >= 500
    finally:
        pool.shutdown(wait=True)
    api = start_api()
    result = execute(second)
    assert result["status"] == "partial_success"
    assert [i["step_id"] for i in result["saga"]["steps"]] == ["p1_seat", "p1_order", "p1_invite"]
    refund = result["saga"]["steps"][1]
    assert refund["key"] == key and refund["attempts"] == 2
    assert refund["events"][-1]["replayed"]
    current = state()
    assert current["balance_cents"] == 100000 and len(current["refunds"]) == 1
    assert not current["members"] and all(not s["active"] for s in current["seats"].values())
    checks += [
        "reverse_unbind_refund_remove",
        "kill_after_refund_before_local_receipt",
        "restart_uses_same_compensation_key",
        "exactly_one_refund_balance_restored",
    ]
    evidence["refund_crash"] = {"old_pid": old_pid, "new_pid": api.pid, "execution": result}
    print("[kill] 退款提交后强杀 API，重启按原键恢复，只有一笔退款。", flush=True)

    faults({})
    ids = [
        seed("/v1/ticket/tickets", {"project_id": project, "title": f"Batch {i}"}) for i in range(4)
    ]
    third = submit("batch", batch=ids)
    before = {tid: state()["tickets"][tid] for tid in ids}
    faults(
        {
            "/v1/ticket/tickets/assign": ["partial_success"],
            "/v1/compensations": ["429", "timeout_after_commit"],
        }
    )
    result = execute(third)
    assert result["status"] == "partial_success"
    batch = next(i for i in result["steps"] if i["step_id"] == "assign_tickets")
    assert [i["status"] for i in batch["items"]] == ["assigned", "assigned", "failed", "failed"]
    assert result["summary"]["compensated"] == ["assign_tickets"]
    assert {tid: state()["tickets"][tid] for tid in ids} == before
    undo = result["saga"]["steps"][0]
    assert undo["attempts"] == 3 and undo["events"][-1]["replayed"]
    assert undo["events"][0]["delay_seconds"] >= 1
    checks += [
        "207_exact_item_results",
        "partial_success_subset_restored",
        "batch_failed_items_untouched",
        "compensation_429_retry_after",
        "compensation_response_loss_replayed",
    ]
    evidence["batch"] = result
    print("[batch] 2 成功 / 2 失败；只恢复成功项，补偿限流与丢响应均恢复。", flush=True)

    faults({"/v1/money/orders": ["insufficient_funds"], "/v1/compensations": ["500"] * 4})
    fourth = submit("manual")
    result = execute(fourth)
    assert result["status"] == "compensation_failed"
    assert result["summary"]["manual"] == ["p1_invite"]
    assert result["summary"]["compensation_unknown"] == ["p1_invite"]
    assert result["saga"]["steps"][0]["attempts"] == 4
    with psycopg.connect(database_url) as conn:
        logs = conn.execute(
            "SELECT count(*) FROM mockcorp_oplog WHERE tenant=%s", (tenant,)
        ).fetchone()[0]
    assert execute(fourth) == result
    with psycopg.connect(database_url) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM mockcorp_oplog WHERE tenant=%s", (tenant,)
            ).fetchone()[0]
            == logs
        )
        duplicates = conn.execute(
            "SELECT idempotency_key FROM mockcorp_oplog WHERE tenant=%s AND outcome='committed' "
            "AND NOT replayed AND NOT dry_run GROUP BY idempotency_key HAVING count(*)>1",
            (tenant,),
        ).fetchall()
    assert not duplicates
    checks += [
        "undo_retry_budget_exhausted_manual",
        "unknown_undo_explicit",
        "terminal_undo_budget_not_reset",
        "supplier_no_duplicate_commits",
    ]
    evidence["manual"] = result

    if args.browser_check:
        browser_tmp = Path("tmp/browser-temp").resolve()
        browser_tmp.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["node", "apps/web/scripts/check-saga.mjs"],
            check=True,
            timeout=120,
            env={
                **os.environ,
                "W8_BROWSER_URL": api_url,
                "W8_BROWSER_TOKEN": tokens["requester"],
                "W8_BROWSER_FUNDS": first["id"],
                "W8_BROWSER_BATCH": third["id"],
                "W8_BROWSER_MANUAL": fourth["id"],
                "TEMP": str(browser_tmp),
                "TMP": str(browser_tmp),
                "TMPDIR": str(browser_tmp),
            },
        )
        checks.append("browser_scope_items_manual_desktop_mobile")
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "passed": len(checks),
        "checks": checks,
        "duplicate_commits": len(duplicates),
        "evidence": evidence,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"W8 验收通过：{len(checks)} 项，报告：{args.output}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--browser-check", action="store_true")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--output", default="tmp/w8-check.json")
    with suppress(KeyboardInterrupt):
        run(parser.parse_args(), scenario=scenario)


if __name__ == "__main__":
    main()
