"""W7: real HTTP/OPA/Postgres, 429 backoff, response loss and forced API kill."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import psycopg
from apps.api.settings import get_settings
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from scripts.w5_plan_demo import DEMO_TEXT, TODAY
from scripts.w6_approval_demo import available_port


def run(args, scenario=None):
    settings = get_settings()
    with httpx.Client(timeout=5) as client:
        client.get(settings.opa_url + "/health").raise_for_status()
    url = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
    admin = make_conninfo(**{**conninfo_to_dict(url), "dbname": "postgres"})
    database = "caseflow_w7_demo_" + uuid4().hex
    demo_url = urlunsplit(urlsplit(url)._replace(path="/" + database))
    tenant = "w7-demo-" + uuid4().hex
    tokens = {
        actor: uuid4().hex for actor in ("requester", "reviewer", "outsider", "intern", "employee")
    }
    credentials = [
        {
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "user_id": actor,
            "role": actor if actor in {"intern", "employee"} else "admin",
            "tenant": tenant + "-other" if actor == "outsider" else tenant,
        }
        for actor, token in tokens.items()
    ]
    api_port, mock_port = args.port if args.serve else available_port(), available_port()
    api_url, mock_url = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{mock_port}"
    env = {
        **os.environ,
        "DATABASE_URL": demo_url,
        "OPA_URL": settings.opa_url,
        "AUTH_CREDENTIALS": json.dumps(credentials),
        "MOCKCORP_URL": mock_url,
        "PLANNING_PROVIDERS": "[]",
        "PYTHONUTF8": "1",
        "EXECUTION_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "MOCKCORP_FAULTS_ENABLED": "true",
        "MOCKCORP_CODE_SEAT_LIMIT": "100",
        "MOCKCORP_FAULT_DELAY_S": "0.05",
    }
    env.update(getattr(args, "api_env", {}))
    processes = []
    Path("tmp").mkdir(exist_ok=True)

    def launch(module, extra, name):
        with open(Path("tmp") / f"{tenant}-{name}.log", "ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", module, *extra],
                env=env,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        processes.append(proc)
        return proc

    def wait_ready(url, proc):
        with httpx.Client(timeout=1) as probe:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError("Demo service exited; see tmp/w7-demo-*.log")
                try:
                    if probe.get(url + "/healthz").is_success:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise RuntimeError("Demo service startup timeout")

    def start_mock(faults, delay="0.05"):
        env["W7_DEMO_FAULTS"] = json.dumps(faults)
        env["MOCKCORP_FAULT_DELAY_S"] = delay
        proc = launch(
            "uvicorn",
            ["scripts.w7_mock_server:app", "--host", "127.0.0.1", "--port", str(mock_port)],
            "mock",
        )
        wait_ready(mock_url, proc)
        return proc

    def start_api():
        proc = launch("scripts.serve_api", ["--port", str(api_port)], "api")
        wait_ready(api_url, proc)
        return proc

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True)
        mock = start_mock(
            {
                "/v1/identity/users": ["429", "429"],
                "/v1/identity/mailboxes": ["timeout_after_commit"],
            }
        )
        api = start_api()
        checks, evidence = [], {}
        with httpx.Client(timeout=90) as client:

            def request(method, path, actor="requester", body=None, expected=200, **headers):
                response = client.request(
                    method,
                    api_url + path,
                    json=body,
                    headers={"Authorization": "Bearer " + tokens[actor], **headers},
                )
                assert response.status_code == expected, (
                    f"{path}: {response.status_code} {response.text}"
                )
                return response.json()

            def seed(path, body):
                response = client.post(
                    mock_url + path,
                    json=body,
                    headers={
                        "Authorization": "Bearer " + tokens["requester"],
                        "Idempotency-Key": uuid4().hex,
                    },
                )
                response.raise_for_status()
                return response.json()["result"]["id"]

            repo = seed("/v1/code/repos", {"name": "frontend"})
            project = seed("/v1/ticket/projects", {"key": "ONBOARD", "name": "入职"})

            def submit(suffix, approve=True):
                text = DEMO_TEXT.format(repo=repo, project=project)
                for name in ("zhangsan", "lisi", "wangwu"):
                    text = text.replace(name, name + suffix)
                row = request(
                    "POST",
                    "/v1/approval-requests",
                    body={"text": text, "today": TODAY.isoformat()},
                    **{"Idempotency-Key": str(uuid4())},
                )
                assert row["status"] == "awaiting_approval", row
                if approve:
                    request(
                        "POST",
                        "/v1/approval-requests/" + row["id"] + "/decision",
                        actor="reviewer",
                        body={
                            "decision": "approve",
                            "plan_hash": row["plan_hash"],
                            "operation_id": str(uuid4()),
                            "reason": "已核对",
                        },
                    )
                return row

            def execute(row, **kwargs):
                return request("POST", "/v1/approval-requests/" + row["id"] + "/execute", **kwargs)

            if scenario is not None:
                return scenario(
                    args=args,
                    request=request,
                    seed=seed,
                    execute=execute,
                    repo=repo,
                    project=project,
                    start_mock=start_mock,
                    start_api=start_api,
                    api=api,
                    mock=mock,
                    database_url=demo_url,
                    tenant=tenant,
                    tokens=tokens,
                    api_url=api_url,
                    client=client,
                )

            if args.serve:
                submit("demo", approve=False)
                print(
                    f"审批与执行台：{api_url}/approvals\n申请人凭据：{tokens['requester']}\n审批人凭据：{tokens['reviewer']}",
                    flush=True,
                )
                print(
                    "先批准，再点击“执行已批准计划”；前两次开户注入 429，首次邮箱提交后丢失响应。"
                    "Ctrl+C 清理本次演示数据库。",
                    flush=True,
                )
                while True:
                    time.sleep(1)

            first = submit("retry", approve=False)
            execute(first, expected=409)
            checks.append("unapproved_execution_blocked")
            request(
                "POST",
                "/v1/approval-requests/" + first["id"] + "/decision",
                actor="reviewer",
                body={
                    "decision": "approve",
                    "plan_hash": first["plan_hash"],
                    "operation_id": str(uuid4()),
                },
            )
            execute(first, actor="intern", expected=403)
            execute(first, actor="outsider", expected=404)
            checks += ["execution_role_denied", "execution_tenant_isolation"]
            if args.browser_check:
                browser_tmp = Path("tmp/browser-temp").resolve()
                browser_tmp.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["node", "apps/web/scripts/check-execution.mjs"],
                    check=True,
                    timeout=120,
                    env={
                        **os.environ,
                        "W7_BROWSER_URL": api_url,
                        "W7_BROWSER_TOKEN": tokens["requester"],
                        "W7_BROWSER_REQUEST": first["id"],
                        "TEMP": str(browser_tmp),
                        "TMP": str(browser_tmp),
                        "TMPDIR": str(browser_tmp),
                    },
                )
                result = request("GET", "/v1/approval-requests/" + first["id"])["execution"]
                checks.append("live_browser_execute_retry_results_desktop_mobile")
            else:
                result = execute(first)
            assert result["status"] == "succeeded" and len(result["steps"]) == 12
            attempts = result["steps"][0]["events"]
            assert [e["status"] for e in attempts] == ["retryable", "retryable", "succeeded"]
            assert all(e["delay_seconds"] >= 1 for e in attempts[:2])
            for previous, following in pairwise(attempts):
                assert (
                    datetime.fromisoformat(following["started_at"])
                    - datetime.fromisoformat(previous["started_at"])
                ).total_seconds() >= previous["delay_seconds"]
            assert any(e["replayed"] for s in result["steps"] for e in s["events"])
            with psycopg.connect(demo_url) as conn:
                count = conn.execute(
                    "SELECT count(*) FROM mockcorp_oplog WHERE tenant=%s", (tenant,)
                ).fetchone()[0]
            assert execute(first) == result
            with psycopg.connect(demo_url) as conn:
                assert (
                    conn.execute(
                        "SELECT count(*) FROM mockcorp_oplog WHERE tenant=%s", (tenant,)
                    ).fetchone()[0]
                    == count
                )
            checks += [
                "twelve_step_execution",
                "429_backoff_observed",
                "retry_after_honored",
                "response_loss_replayed",
                "completed_execution_no_additional_calls",
            ]
            evidence["retry"] = result
            print(
                "[429] 两次限流后成功，Retry-After 与实际调用间隔已核对；"
                "提交后丢失响应由原键重放恢复。",
                flush=True,
            )

            mock.terminate()
            mock.wait(timeout=10)
            mock = start_mock({"/v1/identity/users": ["timeout_after_commit"]}, "6")
            second = submit("crash")
            pool = ThreadPoolExecutor(max_workers=1)
            pending = pool.submit(
                client.post,
                api_url + "/v1/approval-requests/" + second["id"] + "/execute",
                headers={"Authorization": "Bearer " + tokens["requester"]},
            )
            try:
                for _ in range(100):
                    with psycopg.connect(demo_url) as conn:
                        saved = conn.execute(
                            "SELECT journal FROM execution_runs WHERE request_id=%s",
                            (second["id"],),
                        ).fetchone()
                        key = saved[0]["steps"][0].get("key") if saved else None
                        committed = (
                            conn.execute(
                                "SELECT count(*) FROM mockcorp_receipts WHERE tenant=%s AND key=%s",
                                (tenant, key),
                            ).fetchone()[0]
                            if key
                            else 0
                        )
                    if committed:
                        break
                    time.sleep(0.05)
                assert committed, "remote commit was not observed"
                old_pid = api.pid
                api.kill()
                api.wait(timeout=10)
                with suppress(httpx.HTTPError):
                    interrupted = pending.result(timeout=10)
                    assert interrupted.status_code >= 500
            finally:
                pool.shutdown(wait=True)
            api = start_api()
            restored = request("GET", "/v1/approval-requests/" + second["id"])
            assert restored["execution"]["steps"][0]["status"] == "running"
            resumed = execute(second)
            assert resumed["status"] == "succeeded"
            assert resumed["steps"][0]["events"][1]["replayed"]
            assert resumed["steps"][0]["key"] == key
            checks += [
                "kill_after_remote_commit_before_local_receipt",
                "new_process_reuses_key",
                "restart_finishes_without_duplicate_effects",
            ]
            evidence["crash"] = {"old_pid": old_pid, "new_pid": api.pid, "execution": resumed}
            print(
                f"[kill] API {old_pid} → {api.pid}；提交后强杀，新进程按原键恢复完成。", flush=True
            )

            mock.terminate()
            mock.wait(timeout=10)
            mock = start_mock({f"/v1/code/repos/{repo}/members": ["seat_limit"]})
            third = submit("business")
            failed = execute(third)
            assert failed["status"] == "partial_success"
            assert len(failed["completed_steps"]) == 6 and failed["compensation_executed"] is False
            assert all(s["attempts"] == 0 for s in failed["steps"][7:])
            assert execute(third) == failed
            checks += [
                "business_rejection_stops_once",
                "completed_steps_preserved_outside_failed_group",
                "later_steps_not_started",
            ]
            evidence["business_failure"] = failed
            with psycopg.connect(demo_url) as conn:
                state = conn.execute(
                    "SELECT state FROM mockcorp_state WHERE tenant=%s", (tenant,)
                ).fetchone()[0]
                commits = conn.execute(
                    "SELECT count(*) FROM mockcorp_oplog WHERE tenant=%s "
                    "AND outcome='committed' AND NOT replayed "
                    "AND operation NOT IN ('code.create_repo','ticket.create_project')",
                    (tenant,),
                ).fetchone()[0]
            assert (
                len(state["users"]) == 9
                and len(state["mailboxes"]) == 9
                and len(state["tickets"]) == 6
            )
            assert commits == 30
            checks.append("supplier_independent_count_30_effects")
            report = {
                "checked_at": datetime.now(UTC).isoformat(),
                "passed": len(checks),
                "checks": checks,
                "committed_effects": commits,
                "evidence": evidence,
            }
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                f"W7 验收通过：{len(checks)} 组检查，30 次业务副作用，无重复。报告：{args.output}"
            )
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
        print("本次演示进程与独立数据库已清理。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="自动验收（默认）")
    parser.add_argument("--serve", action="store_true", help="交互演示")
    parser.add_argument(
        "--browser-check", action="store_true", help="浏览器执行、重试结果与移动端检查"
    )
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--output", default="tmp/w7-check.json")
    with suppress(KeyboardInterrupt):
        run(parser.parse_args())


if __name__ == "__main__":
    main()
