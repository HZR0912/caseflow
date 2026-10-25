"""W11 release evaluation: isolated database, durable receipts, explicit baseline."""

import argparse
import asyncio
import hashlib
import html
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
import psycopg
from apps.api.settings import get_settings
from core.db.pools import approval_pool, supplier_pool
from core.policy.opa_client import PolicyClient
from eval.gate import PROFILE, PROTOCOL, REPEATS, evaluate
from eval.harness import run_case
from eval.scenarios import load_suite, suite_digest, validate_release_suite
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "eval/baselines/rules-v1.json"


def suites():
    return [load_suite(ROOT / f"eval/suites/{name}.yaml") for name in ("business", "adversarial")]


def digest_tree(directory, pattern):
    # Universal newline decoding keeps a Windows baseline comparable with Linux CI.
    entries = {
        p.relative_to(directory).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(directory.glob(pattern))
    }
    if not entries:
        raise ValueError("empty fingerprint input")
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def verify_live_policy(opa_url):
    response = httpx.get(opa_url.rstrip("/") + "/v1/policies", timeout=10)
    response.raise_for_status()
    live = sorted(item["raw"].replace("\r\n", "\n").strip() for item in response.json()["result"])
    local = sorted(
        p.read_text(encoding="utf-8").strip() for p in (ROOT / "policies").glob("*.rego")
    )
    if live != local:
        raise ValueError("live OPA modules differ from policies/*.rego; reload OPA")


@contextmanager
def isolated_database(url):
    url = url.replace("postgresql+psycopg://", "postgresql://")
    params = conninfo_to_dict(url)
    name = "caseflow_eval_" + uuid4().hex
    admin = make_conninfo(**{**params, "dbname": "postgres"})
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        isolated = urlunsplit(urlsplit(url)._replace(path="/" + name))
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env={**os.environ, "DATABASE_URL": isolated},
            check=True,
        )
        yield isolated
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        print("Isolated evaluation database removed.", flush=True)


async def collect(cases, url, opa_url, repeats, concurrency, journal, storage_metrics=None):
    semaphore = asyncio.Semaphore(concurrency)
    rows = []
    # Match the API lifespan: share the transport pool, never cache decisions.
    policy_client = PolicyClient(opa_url)
    supplier_connections = supplier_pool(url)
    approval_connections = approval_pool(url)

    async def one(case, repeat):
        async with semaphore:
            row = await asyncio.wait_for(
                run_case(
                    case,
                    repeat,
                    url,
                    opa_url,
                    policy_client=policy_client,
                    supplier_pool=supplier_connections,
                    approval_pool=approval_connections,
                ),
                timeout=180,
            )
            rows.append(row)
            journal.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            journal.flush()
            if not row["passed"]:
                print(
                    f"FAIL {case.id}/{repeat}: {row['errors']} status={row['status']}", flush=True
                )
            if len(rows) % 25 == 0:
                print(f"Completed {len(rows)}/{len(cases) * repeats}", flush=True)

    try:
        with supplier_connections:
            async with approval_connections:
                # Cancel and await sibling trials on error before closing shared resources.
                async with asyncio.TaskGroup() as group:
                    for repeat in range(1, repeats + 1):
                        for case in cases:
                            group.create_task(one(case, repeat))
                if storage_metrics is not None:
                    storage_metrics.update(
                        supplier=supplier_connections.get_stats(),
                        approval=approval_connections.get_stats(),
                    )
    finally:
        await policy_client.aclose()
        if storage_metrics is not None:
            storage_metrics["pools_closed"] = (
                supplier_connections.closed and approval_connections.closed
            )
    return sorted(rows, key=lambda row: (row["scenario_id"], row["repeat"]))


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    gate = report["gate"]
    metric_rows = "".join(
        f"<tr><th>{html.escape(key)}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in gate.get("metrics", {}).items()
    )
    failures = "".join(
        f"<tr><td>{html.escape(row['scenario_id'])}/{row['repeat']}</td>"
        f"<td>{html.escape(row['status'])}</td>"
        f"<td>{html.escape(', '.join(row['errors']))}</td></tr>"
        for row in report["trials"]
        if not row["passed"]
    )
    checks = "".join(
        f"<li>{html.escape(key)}: {'PASS' if value else 'FAIL'}</li>"
        for key, value in gate.get("checks", {}).items()
    )
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>W11 评测报告</title>
<style>body{{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;
color:#192c40;background:#f7f9fc}}table{{border-collapse:collapse;width:100%;background:white}}
th,td{{text-align:left;padding:9px;border-bottom:1px solid #ddd}}
.result{{font-size:32px;color:{"#127650" if gate["passed"] else "#b3261e"}}}
code{{overflow-wrap:anywhere}}</style>
<h1>CaseFlow · W11 评测</h1><p class="result">{"PASS" if gate["passed"] else "FAIL"}
· {html.escape(gate["mode"])}</p>
<p>规则模式 / 真实 PostgreSQL / HTTP OPA / 本地 MockCorp 适配器。
内部已知回归集；不代表真实大模型效果。</p>
<p>运行：{html.escape(report["run_id"])} · {html.escape(report["started_at"])}</p>
<p>60 业务 + 60 对抗，每项 {html.escape(str(report["repeats"]))} 次；
pass^5 只计五次全部通过的业务场景。
restart 是提交后服务重建。</p>
<h2>指标</h2><table>{metric_rows}</table><h2>门禁</h2><ul>{checks}</ul>
<p>{html.escape("; ".join(gate.get("errors", [])))}</p><h2>失败明细</h2>
<table><tr><th>场景/轮次</th><th>状态</th><th>断言</th></tr>{failures}</table>
<h2>可追溯性</h2><p>场景 SHA-256：<code>{html.escape(report["suite_digest"])}</code></p>
<p>完整逐例结果、收据及基线比较见同名 JSON；每完成一例即写入同名 JSONL。</p></html>"""
    path.with_suffix(".html").write_text(page, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "eval/results/w11-current.json")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="Explicit initial baseline candidate; never installs it",
    )
    parser.add_argument("--diagnostic", action="store_true", help="One trial each, no release gate")
    parser.add_argument(
        "--check-report", type=Path, help="Recompute gate offline, distrust saved scores"
    )
    parser.add_argument("--concurrency", type=int, choices=range(1, 9), default=4)
    args = parser.parse_args()
    outputs = {args.output.with_suffix(suffix).resolve() for suffix in (".json", ".html", ".jsonl")}
    if args.output.suffix != ".json" or args.baseline.resolve() in outputs:
        print("Output must be .json and must not overwrite baseline", file=sys.stderr)
        return 1
    corpus = suites()
    cases = validate_release_suite(corpus)
    report = dict(
        protocol=PROTOCOL,
        profile=PROFILE,
        repeats=REPEATS,
        suite_digest=suite_digest(corpus),
        corpus_digest=digest_tree(ROOT / "data/policies", "*.md"),
        policy_digest=digest_tree(ROOT / "policies", "*.rego"),
        implementation_digest={
            folder: digest_tree(ROOT / folder, "**/*.py") for folder in ("core", "apps", "eval")
        },
        run_id=uuid4().hex,
        started_at=datetime.now(UTC).isoformat(),
        trials=[],
        provenance=dict(
            interpreter="rules",
            model_calls=0,
            attack_set="known internal regression",
            supplier="local adapter with real PostgreSQL",
            restart="service reconstruction after commit",
            concurrency=args.concurrency,
            policy_transport="run-scoped HTTP pool; no decision cache",
            database_transport="lifespan pools (8 each); fresh direct restart probes",
        ),
        storage_metrics={},
    )
    stage = "read_baseline"
    try:
        if args.bootstrap and args.diagnostic:
            raise ValueError("bootstrap and diagnostic are mutually exclusive")
        baseline = (
            None
            if args.bootstrap or args.diagnostic
            else json.loads(args.baseline.read_text(encoding="utf-8"))
        )
        if baseline is not None:
            report["baseline"] = {
                "path": str(args.baseline),
                "run_id": baseline.get("run_id"),
            }
        if args.check_report:
            stage = "check_report"
            report = json.loads(args.check_report.read_text(encoding="utf-8"))
            # Reject stale inputs as well as non-comparable baseline pairs.
            for key, expected in (
                ("corpus_digest", digest_tree(ROOT / "data/policies", "*.md")),
                ("policy_digest", digest_tree(ROOT / "policies", "*.rego")),
            ):
                if report[key] != expected:
                    raise ValueError("report inputs differ: " + key)
        else:
            settings = get_settings()
            stage = "verify_live_policy"
            verify_live_policy(settings.opa_url)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            stage = "collect_trials"
            with (
                isolated_database(settings.database_url) as url,
                args.output.with_suffix(".jsonl").open("w", encoding="utf-8") as journal,
            ):
                factory = (
                    asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
                )
                with asyncio.Runner(loop_factory=factory) as runner:
                    report["trials"] = runner.run(
                        collect(
                            cases,
                            url,
                            settings.opa_url,
                            1 if args.diagnostic else REPEATS,
                            args.concurrency,
                            journal,
                            report["storage_metrics"],
                        )
                    )
        stage = "evaluate_gate"
        report["gate"] = evaluate(report, corpus, baseline, bootstrap=args.bootstrap)
        if args.diagnostic:
            report["repeats"] = 1
            report["gate"] = dict(
                passed=False,
                mode="diagnostic",
                metrics={},
                checks={},
                errors=["Diagnostic run cannot authorize release"],
            )
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_report(args.output, report)
        print(json.dumps(report["gate"], ensure_ascii=False, indent=2))
        if args.diagnostic:
            return 0 if all(row["passed"] for row in report["trials"]) else 1
        return 0 if report["gate"]["passed"] else 1
    except (Exception, KeyboardInterrupt) as exc:
        # Avoid leaking connection strings or upstream exception messages.
        report["gate"] = dict(
            passed=False,
            mode="invalid",
            metrics={},
            checks={},
            errors=[stage + ":" + type(exc).__name__],
        )
        write_report(args.output, report)
        print(
            "Evaluation failed at "
            + stage
            + ": "
            + type(exc).__name__
            + "; inspect inputs/services and JSONL.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
