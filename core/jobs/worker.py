"""Run with python -m core.jobs.worker; PostgreSQL rebuilds disposable Redis wake-ups."""

import asyncio
import contextlib
import json
import logging
import signal
import sys
import time

from apps.api.settings import Settings
from arq import Worker
from arq.connections import RedisSettings

from core.db.pools import approval_pool
from core.graph.approval_service import ApprovalService
from core.jobs.service import BackgroundJobs
from core.obs.runtime import Runtime
from core.policy.opa_client import PolicyClient
from core.tools.payload import PlanCipher

QUEUE = "caseflow:background"
logger = logging.getLogger(__name__)


def serialize(value):
    return json.dumps(value).encode()


async def dispatch_due(jobs, redis):
    """Periodic IDs avoid stale Redis result/in-progress keys blocking crash recovery."""
    slot = int(time.time() // 5)
    ids = await jobs.due()
    for request_id in ids:
        await redis.enqueue_job(
            "resume_execution",
            request_id,
            _queue_name=QUEUE,
            _job_id=f"background:{request_id}:{slot}",
            _expires=60,
        )
    return len(ids)


async def refill(ctx):
    while True:
        try:
            await dispatch_due(ctx["jobs"], ctx["redis"])
        except Exception:
            # A storage outage leaves durable intent untouched; do not log connection strings.
            logger.warning("background_dispatch_unavailable")
        await asyncio.sleep(2)


async def startup(ctx):
    settings = Settings()
    pool = approval_pool(settings.database_url)
    await pool.open()
    policy = PolicyClient(settings.opa_url, timeout=settings.opa_timeout_s)
    runtime = Runtime(settings)
    service = ApprovalService(
        settings.database_url,
        policy,
        settings.approval_ttl_hours,
        cipher=PlanCipher(settings.execution_encryption_key.get_secret_value())
        if settings.execution_encryption_key.get_secret_value()
        else None,
        pool=pool,
    )
    ctx.update(
        pool=pool,
        policy=policy,
        runtime=runtime,
        jobs=BackgroundJobs(service, Settings, runtime=runtime),
    )
    ctx["refill"] = asyncio.create_task(refill(ctx))


async def shutdown(ctx):
    if "refill" in ctx:
        ctx["refill"].cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ctx["refill"]
    for name in ("pool", "policy", "runtime"):
        if name in ctx:
            closer = ctx[name].aclose if name == "policy" else ctx[name].close
            await closer()


async def resume_execution(ctx, request_id):
    await ctx["jobs"].run(request_id)


async def close_worker(worker):
    # arq 0.26 Worker.close uses SIGUSR1, which does not exist on Windows. Close
    # owned tasks/resources explicitly; shutdown must also run while Redis is down.
    worker.handle_sig(signal.SIGINT)
    await asyncio.gather(*worker.tasks.values(), return_exceptions=True)
    if worker.on_shutdown:
        await worker.on_shutdown(worker.ctx)
    if worker._pool is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(worker.pool.delete(worker.health_check_key), timeout=3)
        await worker.pool.aclose(close_connection_pool=True)


async def main():
    while True:
        settings = Settings()
        worker = Worker(
            [resume_execution],
            redis_settings=RedisSettings.from_dsn(settings.redis_url),
            queue_name=QUEUE,
            on_startup=startup,
            on_shutdown=shutdown,
            job_serializer=serialize,
            job_deserializer=json.loads,
            max_jobs=4,
            job_timeout=120,
            max_tries=1,
            keep_result=0,
            retry_jobs=False,
            handle_signals=False,
        )
        try:
            await worker.async_run()
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception:
            logger.warning("background_worker_reconnecting")
        finally:
            with contextlib.suppress(Exception):
                await close_worker(worker)
        await asyncio.sleep(2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(main())
