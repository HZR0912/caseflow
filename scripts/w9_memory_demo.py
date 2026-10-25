"""W9: three memory types with actual HTTP, OPA, pgvector and API restart."""

import argparse
import json
import os
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg

from scripts.w7_execution_demo import run


def scenario(
    *,
    args,
    request,
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
    execute,
    **_,
):
    checks, evidence = [], {}
    mock.terminate()
    mock.wait(timeout=10)
    start_mock({})
    prefix = "/v1/memory"
    org = {
        "expected_version": 0,
        "departments": [
            {"id": "eng", "name": "Engineering"},
            {"id": "frontend", "name": "Frontend", "parent_id": "eng"},
        ],
        "members": [
            {"id": "reviewer", "department_id": "eng", "is_lead": True},
            {
                "id": "requester",
                "department_id": "frontend",
                "manager_id": "reviewer",
                "mentor_id": "reviewer",
            },
            {"id": "employee", "department_id": "frontend", "is_lead": True},
        ],
        "systems": [
            {"department_id": dept, "kind": key, "resource_id": value, "owner_id": "reviewer"}
            for dept, key, value in (
                ("eng", "email_domain", "example.com"),
                ("frontend", "repo_id", repo),
                ("frontend", "project_id", project),
            )
        ],
    }
    request("PUT", prefix + "/organization", body=org, actor="employee", expected=403)
    request("PUT", prefix + "/organization", body={**org, "tenant": "other"}, expected=422)
    assert request("PUT", prefix + "/organization", body=org)["version"] == 1
    context = request("GET", prefix + "/organization/context")
    assert [r["id"] for r in context["ancestors"]] == ["frontend", "eng"]
    assert context["defaults"]["email_domain"] == "example.com"
    assert context["mentor_id"] == "reviewer" and context["manager_chain"] == ["reviewer"]
    checks += [
        "organization_write_role_and_tenant_spoof_blocked",
        "recursive_org_defaults_and_mentor",
    ]
    pref = {"key": "permission", "value": "read"}
    assert request("PUT", prefix + "/preferences", body=pref)["status"] == "not_saved"
    pref["confirmed"] = True
    first = request("PUT", prefix + "/preferences", body=pref)["memory"]
    assert request("PUT", prefix + "/preferences", body=pref)["status"] == "unchanged"
    checks.append("explicit_confirmation_and_dedup")
    sid = request("POST", prefix + "/sessions")["id"]
    turn = {
        "turn_id": str(uuid4()),
        "expected_version": 0,
        "request": {
            "text": "请准备入职环境",
            "today": "2026-10-25",
            "facts": {
                "employees": [{"name": "测试员工", "username": "memoryuser"}],
                "role": "frontend",
                "start_date": "2026-10-26",
                "end_date": "2026-12-31",
            },
        },
    }
    saved = request("POST", prefix + f"/sessions/{sid}/turns", body=turn)
    assert "测试员工" not in json.dumps(saved, ensure_ascii=False)
    old_pid = api.pid
    api.kill()
    api.wait(timeout=10)
    api = start_api()
    assert api.pid != old_pid
    assert request("GET", prefix + f"/sessions/{sid}") == saved
    assert request("POST", prefix + f"/sessions/{sid}/turns", body=turn) == saved
    request(
        "POST",
        prefix + f"/sessions/{sid}/turns",
        body={**turn, "turn_id": str(uuid4())},
        expected=409,
    )
    checks += [
        "api_kill_restart_session_restored",
        "turn_replay_and_stale_conflict",
        "session_pii_redacted",
    ]
    evidence["restart"] = {
        "old_pid": old_pid,
        "new_pid": api.pid,
        "session_version": saved["version"],
    }
    for actor in ("reviewer", "outsider"):
        request("GET", prefix + f"/sessions/{sid}", actor=actor, expected=404)
        request("DELETE", prefix + f"/sessions/{sid}", actor=actor, expected=404)
        assert request("GET", prefix + "/preferences", actor=actor)["items"] == []
    assert request("GET", prefix + "/organization", actor="outsider")["departments"] == []
    request("GET", prefix + "/preferences", actor="intern", expected=403)
    assert client.get(api_url + prefix + "/preferences").status_code == 401
    checks.append("owner_tenant_role_and_auth_isolation")
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE memory_preferences SET confirmed_at=confirmed_at-interval '7 days' "
            "WHERE tenant=%s AND owner_id='requester'",
            (tenant,),
        )
    week = request("GET", prefix + "/search?q=仓库权限")["items"][0]
    assert week["key"] == "permission" and week["score"] < first["score"]
    assert (
        request("GET", prefix + "/search?q=仓库权限")["items"][0]["confirmed_at"]
        == week["confirmed_at"]
    )
    checks.append("simulated_next_week_recall_decay_without_read_refresh")
    selection = {
        "session_id": sid,
        "session_version": 1,
        "use_preferences": True,
        "use_organization": True,
    }
    body = {"text": "请准备入职环境", "today": "2026-10-25", "memory": selection}
    row = request("POST", "/v1/approval-requests", body=body, **{"Idempotency-Key": str(uuid4())})
    assert row["status"] == "awaiting_approval", row
    sources = {r["field"]: r["source"] for r in row["snapshot"]["memory_context"]["sources"]}
    assert sources["employees"] == "session" and sources["permission"] == "preference"
    assert sources["repo_id"] == "organization"
    execute(row, expected=409)
    request(
        "POST",
        "/v1/approval-requests/" + row["id"] + "/decision",
        actor="reviewer",
        body={
            "decision": "approve",
            "plan_hash": row["plan_hash"],
            "operation_id": str(uuid4()),
        },
    )
    checks.append("three_memory_types_plan_preview_and_approval_gate")
    request("PUT", prefix + "/preferences", body={**pref, "value": "write", "expected_version": 1})
    org["expected_version"] = 1
    org["systems"][1]["resource_id"] = "r_changed"
    request("PUT", prefix + "/organization", body=org)
    frozen = request("GET", "/v1/approval-requests/" + row["id"])
    assert frozen["plan_hash"] == row["plan_hash"]
    assert frozen["snapshot"]["memory_context"] == row["snapshot"]["memory_context"]
    result = execute(row)
    assert result["status"] == "succeeded", result
    assert request("GET", prefix + "/history")["items"][0]["execution_status"] == "succeeded"
    assert (
        request("GET", prefix + "/history?department_id=frontend")["items"][0]["request_id"]
        == row["id"]
    )
    assert request("GET", prefix + "/history", actor="reviewer")["items"] == []
    checks += [
        "memory_changes_do_not_mutate_approved_plan",
        "original_plan_executes_and_scoped_history",
    ]
    evidence["approval"] = {
        "request_id": row["id"],
        "plan_hash": row["plan_hash"],
        "sources": sources,
        "execution_status": result["status"],
    }
    override = request("POST", "/v1/plans/preview", body={**body, "facts": {"permission": None}})
    assert override["status"] == "clarify", override
    request(
        "PUT",
        prefix + "/preferences",
        actor="employee",
        body={"key": "permission", "value": "admin", "confirmed": True},
    )
    denied = request(
        "POST",
        "/v1/plans/preview",
        actor="employee",
        body={
            "text": "请准备入职环境",
            "today": "2026-10-25",
            "facts": turn["request"]["facts"],
            "memory": {"use_preferences": True, "use_organization": True},
        },
    )
    assert denied["status"] == "denied", denied
    checks += [
        "explicit_null_requires_clarification",
        "org_lead_and_admin_preference_do_not_grant_role",
    ]
    if args.serve:
        print(
            f"记忆工作台：{api_url}/approvals\n申请人凭据：{tokens['requester']}\n会话 ID：{sid}",
            flush=True,
        )
        print("Ctrl+C 清理演示环境。", flush=True)
        while True:
            time.sleep(1)
    if args.browser_check:
        temp = Path("tmp/browser-temp").resolve()
        temp.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["node", "apps/web/scripts/check-memory.mjs"],
            check=True,
            timeout=120,
            env={
                **os.environ,
                "W9_BROWSER_URL": api_url,
                "W9_BROWSER_TOKEN": tokens["requester"],
                "W9_BROWSER_SESSION": sid,
                "W9_BROWSER_REQUEST": row["id"],
                "TEMP": str(temp),
                "TMP": str(temp),
                "TMPDIR": str(temp),
            },
        )
        checks.append("browser_memory_controls_provenance_desktop_mobile")
    request("DELETE", prefix + "/preferences/permission?expected_version=2")
    assert request("GET", prefix + "/preferences")["items"] == []
    request("PUT", prefix + "/preferences", body=pref)
    request("DELETE", prefix + "/preferences/permission?expected_version=1", expected=409)
    checks.append("forget_recreate_rejects_stale_delete")
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE memory_preferences SET expires_at=now()-interval '1 second' WHERE tenant=%s",
            (tenant,),
        )
        conn.execute(
            "UPDATE memory_sessions SET expires_at=now()-interval '1 second' WHERE id=%s", (sid,)
        )
    assert request("GET", prefix + "/search?q=权限")["items"] == []
    request("GET", prefix + f"/sessions/{sid}", expected=410)
    assert request("POST", prefix + "/purge-expired") == {"preferences": 1, "sessions": 1}
    request("GET", prefix + f"/sessions/{sid}", expected=404)
    with psycopg.connect(database_url) as conn:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            assert (
                conn.execute(
                    f"SELECT count(*) FROM {table} WHERE thread_id LIKE 'memory:%'"
                ).fetchone()[0]
                == 0
            )
        assert (
            conn.execute(
                "SELECT count(*) FROM memory_preferences WHERE tenant=%s AND owner_id='employee'",
                (tenant,),
            ).fetchone()[0]
            == 1
        )
    checks.append("expired_recall_blocked_purge_checkpoints_owner_scoped")
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "passed": len(checks),
        "checks": checks,
        "evidence": evidence,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"W9 验收通过：{len(checks)} 项，报告：{args.output}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--browser-check", action="store_true")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--output", default="tmp/w9-check.json")
    with suppress(KeyboardInterrupt):
        run(parser.parse_args(), scenario=scenario)


if __name__ == "__main__":
    main()
