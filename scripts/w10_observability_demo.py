"""W10 real HTTP + Postgres + OTel + optional local Langfuse and browser verification."""

import argparse
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from dotenv import dotenv_values

from scripts.w7_execution_demo import run


class ModelFixture(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        data = json.dumps(
            {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def scenario(
    *,
    args,
    request,
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
    **_,
):
    checks, evidence = [], {}

    def faults(values, delay="0.05"):
        nonlocal mock
        mock.terminate()
        mock.wait(timeout=10)
        mock = start_mock(values, delay)

    def submit(suffix, paid=False):
        row = request(
            "POST",
            "/v1/approval-requests",
            body={
                "text": "请为一位新员工准备入职环境。",
                "today": "2026-10-25",
                "facts": {
                    "employees": [{"name": "测试员工", "username": "trace" + suffix}],
                    "quantity": 1,
                    "role": "developer",
                    "start_date": "2026-10-26",
                    "end_date": "2026-12-31",
                    "email_domain": "example.com",
                    "repo_id": repo,
                    "project_id": project,
                    "permission": "read",
                    **({"paid_item": "editor", "unit_price_cents": 500} if paid else {}),
                },
            },
            **{"Idempotency-Key": str(uuid4())},
        )
        assert row["status"] == "awaiting_approval", row
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

    def replay(row):
        listing = request("GET", "/v1/traces?request_id=" + row["id"])["items"]
        assert len(listing) == 1 and listing[0]["owner_id"] == "requester"
        return request("GET", "/v1/traces/" + listing[0]["trace_id"])

    first = submit("retry")
    result = execute(first)
    assert result["status"] == "succeeded"
    trace = replay(first)
    names = {f["name"] for f in trace["frames"]}
    assert {
        "graph.understand",
        "graph.retrieve",
        "graph.plan",
        "graph.authorize",
        "graph.dry_run",
        "graph.review",
        "graph.approval",
        "graph.report",
        "model.call",
        "tool.call",
        "policy.authorize",
    } <= names
    calls = [f for f in trace["frames"] if f["name"] == "tool.call"]
    assert sum(f["output"]["status"] == "retryable" for f in calls) >= 2
    assert any(f["output"]["replayed"] for f in calls)
    assert any(f["status"] == "waiting" for f in trace["frames"])
    assert trace["metrics"]["input_tokens"] == 100
    assert trace["metrics"]["output_tokens"] == 20
    assert abs(trace["metrics"]["cost_usd"] - 0.00028) < 1e-10
    assert any(f["output"].get("evidence") for f in trace["frames"])
    serialized = json.dumps(trace, ensure_ascii=False)
    assert all(
        s not in serialized for s in ["测试员工", "traceretry", "example.com", *tokens.values()]
    )
    checks += [
        "planning_approval_execution_same_trace",
        "retry_and_response_loss_visible",
        "approval_interrupt_is_waiting",
        "retrieval_chunk_ids_present",
        "fixture_model_usage_and_estimated_cost",
        "local_trace_privacy",
    ]
    tid = trace["trace_id"]
    request("GET", "/v1/traces/" + tid, actor="outsider", expected=404)
    request("GET", "/v1/traces/" + tid, actor="employee", expected=404)
    assert request("GET", "/v1/traces", actor="employee")["items"] == []
    request("GET", "/v1/traces/" + tid, actor="intern", expected=403)
    assert request("GET", "/v1/traces/" + tid, actor="reviewer")["trace_id"] == tid
    request("POST", "/v1/traces/" + tid, expected=405)
    assert client.get(api_url + "/v1/traces").status_code == 401
    checks += ["tenant_owner_role_and_auth_isolation", "replay_has_no_write_endpoint"]

    def effects():
        with psycopg.connect(database_url) as conn:
            return conn.execute(
                "SELECT count(*) FROM mockcorp_oplog WHERE tenant=%s", (tenant,)
            ).fetchone()[0]

    count = effects()
    for _ in range(3):
        replay(first)
    assert effects() == count
    api.terminate()
    api.wait(timeout=10)
    api = start_api()
    assert replay(first)["frames"] == trace["frames"]
    assert execute(first) == result and effects() == count
    checks += ["read_only_replay_zero_effects", "restart_retains_trace_and_idempotency"]

    faults({"/v1/money/orders": ["insufficient_funds"]})
    second = submit("saga", paid=True)
    saga = execute(second)
    assert saga["status"] == "partial_success"
    saga_trace = replay(second)
    assert any(f["name"] == "saga.compensate" for f in saga_trace["frames"])
    assert any(f["output"].get("status") == "partial_success" for f in saga_trace["frames"])
    checks.append("partial_success_and_compensation_visible")

    faults({"/v1/identity/users": ["timeout_after_commit"]}, "6")
    third = submit("crash")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            client.post,
            api_url + f"/v1/approval-requests/{third['id']}/execute",
            headers={"Authorization": "Bearer " + tokens["requester"]},
        )
        committed = False
        for _ in range(150):
            with psycopg.connect(database_url) as conn:
                row = conn.execute(
                    "SELECT journal FROM execution_runs WHERE request_id=%s", (third["id"],)
                ).fetchone()
                key = row[0]["steps"][0].get("key") if row else None
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
        assert committed
        api.kill()
        api.wait(timeout=10)
        with suppress(httpx.HTTPError):
            pending.result(timeout=10)
    api = start_api()
    incomplete = replay(third)
    assert incomplete["metrics"]["unfinished_count"] >= 2
    assert execute(third)["status"] == "succeeded"
    recovered = replay(third)
    assert recovered["trace_id"] == incomplete["trace_id"]
    assert recovered["metrics"]["unfinished_count"] >= 2
    assert any(f["output"].get("replayed") for f in recovered["frames"])
    checks += ["kill_leaves_honest_unfinished_frames", "recovery_appends_same_trace"]

    if args.browser_check:
        browser_tmp = Path("tmp/browser-temp").resolve()
        browser_tmp.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["node", "apps/web/scripts/check-traces.mjs"],
            check=True,
            timeout=90,
            env={
                **os.environ,
                "W10_BROWSER_URL": api_url,
                "W10_BROWSER_TOKEN": tokens["requester"],
                "W10_TRACE_ID": tid,
                "TEMP": str(browser_tmp),
                "TMP": str(browser_tmp),
                "TMPDIR": str(browser_tmp),
            },
        )
        checks.append("browser_replay_controls_mobile_logout_get_only")

    if args.langfuse_check:
        values = dotenv_values("tmp/observability.env")
        auth = (values["LANGFUSE_PUBLIC_KEY"], values["LANGFUSE_SECRET_KEY"])
        base = values["LANGFUSE_BASE_URL"]
        # Ingestion acknowledgement alone is insufficient: poll actual stored observations.
        remote = None
        for _ in range(30):
            response = client.get(base + "/api/public/traces/" + tid, auth=auth)
            if response.status_code == 200:
                remote = response.json()
                if len(remote.get("observations", [])) >= len(trace["frames"]):
                    break
            time.sleep(1)
        assert remote and remote["id"] == tid, "Langfuse trace not materialized"
        observations = remote["observations"]
        assert {f["span_id"] for f in trace["frames"]} <= {s["id"] for s in observations}
        generation = next(s for s in observations if s["name"] == "model.call")
        assert generation["usage"]["input"] == 100 and generation["usage"]["output"] == 20
        assert all(
            s not in json.dumps(remote, ensure_ascii=False)
            for s in ["测试员工", "traceretry", "example.com", *tokens.values()]
        )
        checks += [
            "real_langfuse_otlp_persisted_all_spans",
            "langfuse_generation_usage_and_privacy",
        ]
        evidence["langfuse"] = {
            "trace_id": tid,
            "observations": len(observations),
            "usage": generation["usage"],
        }
    evidence.update(trace=trace, saga=saga_trace, recovered=recovered)
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "passed": len(checks),
        "checks": checks,
        "evidence": evidence,
        "model_note": "Local HTTP fixture: 100 input / 20 output tokens; no paid model call.",
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"W10 passed: {len(checks)} checks. Report: {args.output}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--browser-check", action="store_true")
    parser.add_argument("--langfuse-check", action="store_true")
    parser.add_argument("--output", default="tmp/w10-check.json")
    args = parser.parse_args()
    args.serve, args.port = False, 18000
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    args.api_env = {
        "OBSERVATION_ENABLED": "true",
        "PLANNING_PROVIDERS": json.dumps(
            [
                {
                    "name": "verification_fixture",
                    "model": "fixture-model",
                    "api_key": "fixture-only",
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                    "input_usd_per_million": 2,
                    "output_usd_per_million": 4,
                }
            ]
        ),
    }
    if args.langfuse_check:
        args.api_env.update(
            {
                k: v
                for k, v in dotenv_values("tmp/observability.env").items()
                if k.startswith("LANGFUSE_")
            }
        )
    try:
        run(args, scenario=scenario)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
