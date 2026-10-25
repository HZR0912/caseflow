"""Run eval.runner with connection-attempt counters; never record DSNs or raw errors.

Usage: python -m scripts.verify_database_connections --concurrency 4 --output PATH.json
The sidecar includes admin connections and explicit restart probes, but not migration
subprocess connections. Instrumentation observes calls; it never retries them.
"""

import argparse
import json
import sys
import threading
from collections import Counter
from pathlib import Path
from time import perf_counter

import psycopg
from eval import runner


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--output", type=Path, default=runner.ROOT / "eval/results/w11-current.json"
    )
    args, _ = parser.parse_known_args()
    counts, failures = Counter(), []
    lock = threading.Lock()
    sync_original = psycopg.Connection.connect.__func__
    async_original = psycopg.AsyncConnection.connect.__func__

    def record(kind, exc=None):
        with lock:
            counts[kind] += 1
            if exc is not None:
                message = str(exc).lower()
                category = next(
                    (
                        name
                        for name in (
                            "address already in use",
                            "too many clients",
                            "connection refused",
                            "timeout expired",
                            "connection reset",
                            "connection failed",
                        )
                        if name in message
                    ),
                    "unclassified",
                )
                failures.append(
                    {
                        "kind": kind,
                        "type": type(exc).__name__,
                        "category": category,
                        "winsock_10048": "10048" in message,
                    }
                )

    @classmethod
    def connect(cls, *args, **kwargs):
        record("sync_attempts")
        try:
            return sync_original(cls, *args, **kwargs)
        except psycopg.Error as exc:
            record("sync_errors", exc)
            raise

    @classmethod
    async def async_connect(cls, *args, **kwargs):
        record("async_attempts")
        try:
            return await async_original(cls, *args, **kwargs)
        except psycopg.Error as exc:
            record("async_errors", exc)
            raise

    psycopg.Connection.connect = connect
    psycopg.connect = psycopg.Connection.connect
    psycopg.AsyncConnection.connect = async_connect
    started = perf_counter()
    try:
        result = runner.main()
    finally:
        psycopg.Connection.connect = classmethod(sync_original)
        psycopg.connect = psycopg.Connection.connect
        psycopg.AsyncConnection.connect = classmethod(async_original)
    sidecar = dict(counts=counts, failures=failures, elapsed_s=perf_counter() - started)
    args.output.with_suffix(".connections.json").write_text(
        json.dumps(sidecar, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(sidecar))
    return result


if __name__ == "__main__":
    sys.exit(main())
