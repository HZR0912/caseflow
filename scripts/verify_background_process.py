"""Real API/worker hard kills after supplier commits; isolated PostgreSQL and Redis queue."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from apps.api.settings import Settings
from cryptography.fernet import Fernet
from redis import Redis

from scripts.w5_plan_demo import DEMO_TEXT, TODAY
from scripts.w7_execution_demo import run as run_isolated


def verify(ctx):
    settings = Settings()
    request = ctx["request"]
    database_url, tenant = ctx["database_url"], ctx["tenant"]
    queue = "caseflow:verify-background:" + uuid4().hex
    processes = []
    request_id = None
    redis = Redis.from_url(settings.redis_url)
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "REDIS_URL": settings.redis_url,
        "OPA_URL": settings.opa_url,
        "MOCKCORP_URL": ctx["mock_url"],
        "EXECUTION_ENCRYPTION_KEY": ctx["args"].api_env["EXECUTION_ENCRYPTION_KEY"],
        "AUTH_CREDENTIALS": json.dumps(
            [
                {
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "user_id": actor,
                    "role": "admin",
                    "tenant": tenant,
                }
                for actor, token in ctx["tokens"].items()
                if actor in {"requester", "reviewer"}
            ]
        ),
        "OBSERVATION_ENABLED": "false",
        "PYTHONUTF8": "1",
    }

    def start_worker():
        # Only the queue name differs from the production module entry point. All
        # scheduling, authentication, HTTP IO and PostgreSQL logic are unchanged.
        bootstrap = (
            "import asyncio,logging,sys; from core.jobs import worker; "
            f"worker.QUEUE={queue!r}; logging.basicConfig(level=logging.INFO); "
            "factory=asyncio.SelectorEventLoop if sys.platform=='win32' "
            "else asyncio.new_event_loop; "
            "runner=asyncio.Runner(loop_factory=factory); runner.run(worker.main())"
        )
        with (Path("tmp") / f"{tenant}-background-worker.log").open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-c", bootstrap],
                env=env,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        processes.append(process)
        return process

    def stop(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    def users():
        with psycopg.connect(database_url) as conn:
            row = conn.execute(
                "SELECT state FROM mockcorp_state WHERE tenant=%s", (tenant,)
            ).fetchone()
            return len(row[0]["users"]) if row else 0

    def job():
        with psycopg.connect(database_url) as conn:
            return conn.execute(
                "SELECT b.status,b.credential,b.runs,e.journal FROM background_jobs b "
                "LEFT JOIN execution_runs e ON e.request_id=b.request_id WHERE b.request_id=%s",
                (request_id,),
            ).fetchone()

    def until(predicate, process, seconds=50):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            assert process.poll() is None, "background_process_exited_see_tmp_log"
            result = predicate()
            if result:
                return result
            time.sleep(0.1)
        raise AssertionError("background_process_progress_timeout")

    try:
        redis.ping()
        ctx["mock"].kill()
        ctx["mock"].wait(timeout=10)
        mock = ctx["start_mock"]({"/v1/identity/users": ["timeout_after_commit"]}, delay="10")
        row = request(
            "POST",
            "/v1/approval-requests",
            body={
                "text": DEMO_TEXT.format(repo=ctx["repo"], project=ctx["project"]),
                "today": TODAY.isoformat(),
            },
            **{"Idempotency-Key": str(uuid4())},
        )
        request_id = row["id"]
        assert row["status"] == "awaiting_approval"
        request(
            "POST",
            f"/v1/approval-requests/{request_id}/decision",
            actor="reviewer",
            body={
                "decision": "approve",
                "plan_hash": row["plan_hash"],
                "operation_id": str(uuid4()),
            },
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                ctx["client"].post,
                ctx["api_url"] + f"/v1/approval-requests/{request_id}/execute",
                headers={"Authorization": "Bearer " + ctx["tokens"]["requester"]},
            )
            until(lambda: users() == 1, ctx["api"], seconds=20)
            stop(ctx["api"])
            try:
                response = pending.result(timeout=15)
            except httpx.TransportError:
                pass
            else:
                # A local proxy may translate the killed upstream connection to 502.
                assert response.status_code >= 500, "API returned success before hard kill"
        assert job()[0] == "queued" and job()[3]["steps"][0]["outcome_unknown"]
        print("API hard-killed after first supplier commit; durable consent retained", flush=True)

        stop(mock)
        mock = ctx["start_mock"]({"/v1/identity/users": [None, "timeout_after_commit"]}, delay="10")
        first_worker = start_worker()
        until(lambda: users() == 2, first_worker, seconds=25)
        stop(first_worker)
        interrupted = job()
        assert interrupted[0] == "running" and interrupted[3]["steps"][1]["outcome_unknown"]
        print("Worker hard-killed after second supplier commit; restarting normally", flush=True)
        stop(mock)
        ctx["start_mock"]({})
        second_worker = start_worker()
        completed = until(
            lambda: value if (value := job())[0] == "completed" else None, second_worker
        )
        journal = completed[3]
        assert journal["status"] == "succeeded" and completed[1] is None
        assert users() == 3 and len(journal["steps"]) == 12
        for entry in journal["steps"][:2]:
            assert entry["attempts"] == 2 and entry["events"][-1]["replayed"]
        with psycopg.connect(database_url) as conn:
            commits, replayed = conn.execute(
                "SELECT count(*) FILTER (WHERE NOT replayed AND outcome='committed'), "
                "count(*) FILTER (WHERE replayed) FROM mockcorp_oplog "
                "WHERE tenant=%s AND NOT dry_run AND operation NOT IN "
                "('code.create_repo','ticket.create_project')",
                (tenant,),
            ).fetchone()
        assert commits == 12 and replayed == 2
        report = {
            "status": "passed",
            "checks": [
                "api_hard_kill_after_remote_commit",
                "worker_hard_kill_after_remote_commit",
                "normal_due_time_automatic_restart_recovery",
                "same_idempotency_key_replayed_twice",
                "twelve_effects_no_duplicates",
                "completed_credential_erased",
            ],
            "api_pid": ctx["api"].pid,
            "killed_worker_pid": first_worker.pid,
            "restarted_worker_pid": second_worker.pid,
            "worker_runs": completed[2],
            "business_commits": commits,
            "replays": replayed,
            "steps": len(journal["steps"]),
            "due_time_accelerated": False,
        }
        return report
    finally:
        for process in processes:
            stop(process)
        # Remove only this verification's queue/IDs, never flush shared Redis.
        redis.delete(queue, queue + ":health-check")
        if request_id:
            for key in redis.scan_iter(match=f"*{request_id}*"):
                redis.delete(key)
        redis.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval/results/background-process.json"))
    args = parser.parse_args()
    args.serve, args.browser_check, args.port = False, False, 0
    args.api_env = {"EXECUTION_ENCRYPTION_KEY": Fernet.generate_key().decode()}

    def scenario(**ctx):
        # start_mock closes over its listen URL; the process arguments expose only
        # the ephemeral port, never credentials or connection strings.
        port = ctx["mock"].args[-1]
        ctx["mock_url"] = f"http://127.0.0.1:{port}"
        return verify(ctx)

    report = run_isolated(args, scenario)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
