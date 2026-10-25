"""W6 real HTTP + OPA + PostgreSQL demo, with forced API restart and an interactive mode."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from apps.api.settings import get_settings
from apps.mockcorp.models import MockSettings
from apps.mockcorp.store import PostgresStore

from scripts.w5_plan_demo import DEMO_TEXT, TODAY


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def cleanup(url, tenant):
    with psycopg.connect(url) as conn:
        ids = conn.execute("SELECT id FROM approval_requests WHERE tenant=%s", (tenant,)).fetchall()
        for (request_id,) in ids:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                conn.execute(f"DELETE FROM {table} WHERE thread_id=%s", ("approval:" + request_id,))
        conn.execute("DELETE FROM approval_requests WHERE tenant=%s", (tenant,))
        for table in ("mockcorp_receipts", "mockcorp_oplog", "mockcorp_state"):
            conn.execute(f"DELETE FROM {table} WHERE tenant=%s", (tenant,))


def run(args):
    settings = get_settings()
    tenant = "w6-demo-" + uuid4().hex
    tokens = {actor: uuid4().hex for actor in ("requester", "reviewer", "outsider", "intern")}
    credentials = [
        {
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "user_id": actor,
            "role": "intern" if actor == "intern" else "admin",
            "tenant": tenant + "-other" if actor == "outsider" else tenant,
        }
        for actor, token in tokens.items()
    ]
    api_port = args.port if args.serve else available_port()
    mock_port = available_port()
    api_url, mock_url = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{mock_port}"
    env = {
        **os.environ,
        "DATABASE_URL": settings.database_url,
        "OPA_URL": settings.opa_url,
        "AUTH_CREDENTIALS": json.dumps(credentials),
        "MOCKCORP_URL": mock_url,
        "PLANNING_PROVIDERS": "[]",
        "PYTHONUTF8": "1",
    }
    store = PostgresStore(settings.database_url, MockSettings(_env_file=None))
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

    def wait_ready(client, url, proc):
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError("Demo service exited; inspect tmp/w6-demo-*.log")
            try:
                if client.get(url + "/healthz").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("Demo service startup timeout")

    try:
        mock = launch(
            "uvicorn",
            ["apps.mockcorp.main:app", "--host", "127.0.0.1", "--port", str(mock_port)],
            "mock",
        )
        api = launch("scripts.serve_api", ["--port", str(api_port)], "api")
        with httpx.Client(timeout=40) as client:
            wait_ready(client, mock_url, mock)
            wait_ready(client, api_url, api)

            def request(method, path, actor="requester", body=None, expected=200, **headers):
                response = client.request(
                    method,
                    api_url + path,
                    json=body,
                    headers={"Authorization": "Bearer " + tokens[actor], **headers},
                )
                if response.status_code != expected:
                    raise AssertionError(
                        f"{path}: expected {expected}, got {response.status_code}: {response.text}"
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
            project = seed("/v1/ticket/projects", {"key": "ONBOARD", "name": "入职准备"})
            payload = {
                "text": DEMO_TEXT.format(repo=repo, project=project),
                "today": TODAY.isoformat(),
            }
            before = store.snapshot(tenant)
            key = str(uuid4())
            first = request(
                "POST", "/v1/approval-requests", body=payload, **{"Idempotency-Key": key}
            )
            assert first["status"] == "awaiting_approval" and first["interrupted"], first
            second = request(
                "POST", "/v1/approval-requests", body=payload, **{"Idempotency-Key": str(uuid4())}
            )
            base = "/v1/approval-requests/" + first["id"]
            print(f"[interrupt] {first['id']} · next={first['next']} · 12 步预演已冻结", flush=True)
            original_pid = api.pid
            api.kill()
            api.wait(timeout=10)
            api = launch("scripts.serve_api", ["--port", str(api_port)], "api-restarted")
            wait_ready(client, api_url, api)
            restored = request("GET", base)
            assert restored["checkpoint_id"] == first["checkpoint_id"]
            assert restored["next"] == ["approval"] and restored["interrupted"]
            print(f"[restart] API PID {original_pid} → {api.pid} · checkpoint 未丢失", flush=True)
            if args.serve:
                print(f"\n审批台：{api_url}/approvals")
                print(f"申请人凭据：{tokens['requester']}")
                print(f"审批人凭据：{tokens['reviewer']}")
                print("两个临时凭据仅用于本次演示。以审批人连接后可分别批准和拒绝两份申请。")
                print("Ctrl+C 停止服务并清理本次临时租户。", flush=True)
                while True:
                    time.sleep(1)

            checks = ["interrupt_persisted", "api_killed_and_restarted_same_checkpoint"]
            if args.browser_check:
                browser_tmp = Path("tmp/browser-temp").resolve()
                browser_tmp.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["node", "apps/web/scripts/check-live.mjs"],
                    check=True,
                    env={
                        **os.environ,
                        "W6_BROWSER_URL": api_url,
                        "W6_BROWSER_TOKEN": tokens["reviewer"],
                        "W6_BROWSER_REQUEST": first["id"],
                        "TEMP": str(browser_tmp),
                        "TMP": str(browser_tmp),
                        "TMPDIR": str(browser_tmp),
                    },
                    timeout=60,
                )
                checks.append("live_browser_desktop_mobile")
            command = {
                "decision": "approve",
                "reason": "已核对影响与回滚限制",
                "plan_hash": first["plan_hash"],
                "operation_id": str(uuid4()),
            }
            request("POST", base + "/decision", body=command, expected=403)
            request("POST", base + "/decision", actor="intern", body=command, expected=403)
            request("GET", base, actor="outsider", expected=404)
            request(
                "POST",
                base + "/decision",
                actor="reviewer",
                body={**command, "plan_hash": "f" * 64},
                expected=409,
            )
            checks += [
                "self_approval_403",
                "unauthorized_403",
                "cross_tenant_404",
                "stale_plan_409",
            ]
            replay = request(
                "POST", "/v1/approval-requests", body=payload, **{"Idempotency-Key": key}
            )
            assert replay["id"] == first["id"]
            done = request("POST", base + "/decision", actor="reviewer", body=command)
            assert done["status"] == "approved_ready" and done["next"] == []
            assert done["trace"] == ["review", "approval:approve", "report"]
            assert (
                request("POST", base + "/decision", actor="reviewer", body=command)["decision"]
                == done["decision"]
            )
            request(
                "POST",
                base + "/decision",
                actor="reviewer",
                body={**command, "decision": "reject"},
                expected=409,
            )
            checks += [
                "creation_idempotent",
                "approve_resumes_without_replanning",
                "decision_idempotent",
                "opposing_decision_409",
            ]
            reject = {
                **command,
                "decision": "reject",
                "plan_hash": second["plan_hash"],
                "operation_id": str(uuid4()),
            }
            second_base = "/v1/approval-requests/" + second["id"]
            request(
                "POST",
                second_base + "/decision",
                actor="reviewer",
                body={**reject, "reason": ""},
                expected=422,
            )
            denied = request("POST", second_base + "/decision", actor="reviewer", body=reject)
            assert denied["status"] == "rejected" and not denied["next"]
            assert store.snapshot(tenant) == before
            with psycopg.connect(store.url) as conn:
                logs_count = conn.execute(
                    "SELECT count(*) FROM mockcorp_oplog "
                    "WHERE tenant=%s AND operation='plan.preview'",
                    (tenant,),
                ).fetchone()[0]
                assert logs_count == 2, "恢复或重复提交不应重跑预演"
            checks += [
                "reject_reason_required_422",
                "reject_resumes",
                "business_unchanged",
                "only_two_previews",
            ]
            if Path("apps/web/dist/index.html").exists():
                assert client.get(api_url + "/approvals").status_code == 200
                checks.append("console_served")
            report = {
                "checks": checks,
                "passed": len(checks),
                "old_pid": original_pid,
                "new_pid": api.pid,
                "before": {"status": first["status"], "next": first["next"]},
                "after": {"status": done["status"], "next": done["next"], "trace": done["trace"]},
                "reject_status": denied["status"],
                "preview_count": logs_count,
                "business_unchanged": True,
            }
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"[approve] {done['status']} · trace={done['trace']}")
            print(f"[reject] {denied['status']} · 未执行")
            print(f"W6 验收通过：{len(checks)} 项断言组，业务数据未改变。报告：{args.output}")
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        cleanup(store.url, tenant)
        print("本次演示进程与临时租户已清理。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--check", action="store_true", help="默认即自动验收")
    parser.add_argument(
        "--browser-check", action="store_true", help="需要已安装 Playwright Chromium"
    )
    parser.add_argument("--port", type=int, default=18000, help="交互演示端口")
    parser.add_argument("--output", default="tmp/w6-check.json")
    args = parser.parse_args()
    with suppress(KeyboardInterrupt):
        run(args)


if __name__ == "__main__":
    main()
