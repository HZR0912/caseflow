"""Real connections: reuse, bounded overload, lock ownership, cancellation and shutdown."""

import asyncio
import os
from uuid import uuid4

import psycopg
import pytest
from apps.mockcorp.models import MockSettings
from apps.mockcorp.store import PostgresStore
from core.db.pools import approval_pool, supplier_pool
from core.graph.approval_service import ApprovalError, ApprovalService
from psycopg_pool import PoolTimeout, TooManyRequests

from tests.test_approval_integration import ApprovalPolicy

pytestmark = pytest.mark.integration


@pytest.fixture
def url():
    value = os.getenv("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL required")
    return value


def test_supplier_reuses_one_connection_and_bounds_wait(url):
    with supplier_pool(url, max_size=1, timeout=0.1) as pool:
        store = PostgresStore(url, MockSettings(_env_file=None), pool=pool)
        for _ in range(100):
            assert store.snapshot("absent") == store.snapshot("absent")
        assert pool.get_stats()["connections_num"] == 1
        with pool.connection(), pytest.raises(PoolTimeout):
            store.snapshot("absent")
        store.health()
        assert pool.get_stats()["pool_available"] == 1
    assert pool.closed


@pytest.mark.parametrize("exit_kind", ["success", "error", "sql_error", "cancel"])
async def test_reused_approval_connection_releases_locks_on_every_exit(url, exit_kind):
    lock_id = uuid4().hex
    async with approval_pool(url, max_size=1) as pool:
        service = ApprovalService(url, ApprovalPolicy(), pool=pool)
        entered, finish = asyncio.Event(), asyncio.Event()
        backend = []

        async def operation():
            async with service.session(lock_id) as (conn, _):
                backend.append(conn.info.backend_pid)
                entered.set()
                await finish.wait()
                if exit_kind == "error":
                    raise ValueError("injected")
                if exit_kind == "sql_error":
                    await conn.execute("SELECT 1/0")

        task = asyncio.create_task(operation())
        await asyncio.wait_for(entered.wait(), 10)
        contender = ApprovalService(url, ApprovalPolicy())
        with pytest.raises(ApprovalError, match="approval_busy_retry"):
            async with contender.session(lock_id):
                pytest.fail("lock must exclude another backend")
        if exit_kind == "cancel":
            task.cancel()
        else:
            finish.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        expected = {
            "success": type(None),
            "error": ValueError,
            "sql_error": ApprovalError,
            "cancel": asyncio.CancelledError,
        }
        assert isinstance(result, expected[exit_kind])
        async with pool.connection() as conn:
            assert conn.info.backend_pid == backend[0], "must exercise physical reuse"
            count = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM pg_locks WHERE pid=pg_backend_pid() "
                    "AND locktype='advisory'"
                )
            ).fetchone()
            assert count["n"] == 0
        async with contender.session(lock_id):
            pass
        async with service.session(lock_id):
            pass
        assert pool.get_stats()["pool_available"] == 1
    assert pool.closed


async def test_pool_queue_is_bounded_and_fails_closed(url):
    async with approval_pool(url, max_size=1, timeout=0.2, max_waiting=1) as pool:
        service = ApprovalService(url, ApprovalPolicy(), pool=pool)

        async def borrow():
            async with service.session():
                pytest.fail("exhausted pool must not admit work")

        async with service.session():
            waiting = asyncio.create_task(borrow())
            for _ in range(100):
                if pool.get_stats()["requests_waiting"] == 1:
                    break
                await asyncio.sleep(0.001)
            assert pool.get_stats()["requests_waiting"] == 1
            with pytest.raises(ApprovalError) as full:
                await borrow()
            assert full.value.status == 503
            assert isinstance(full.value.__cause__, TooManyRequests)
            with pytest.raises(ApprovalError) as timed_out:
                await waiting
            assert isinstance(timed_out.value.__cause__, PoolTimeout)
        async with service.session():
            pass


async def test_broken_connection_is_discarded_without_replaying_work(url):
    async with approval_pool(url, max_size=1) as pool:
        service = ApprovalService(url, ApprovalPolicy(), pool=pool)
        calls = []
        with pytest.raises(ApprovalError, match="approval_storage_unavailable"):
            async with service.session(uuid4().hex) as (conn, _):
                pid = conn.info.backend_pid
                calls.append("work")
                async with await psycopg.AsyncConnection.connect(url, autocommit=True) as admin:
                    await admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
                await conn.execute("SELECT 1")
        async with service.session() as (conn, _):
            assert conn.info.backend_pid != pid
        assert calls == ["work"]


async def test_cancellation_during_unlock_discards_connection(url, monkeypatch):
    async with approval_pool(url, max_size=1) as pool:
        service = ApprovalService(url, ApprovalPolicy(), pool=pool)
        original = psycopg.AsyncConnection.execute
        cleanup_started = asyncio.Event()
        backend = []

        async def delayed_unlock(conn, query, *args, **kwargs):
            if query == "SELECT pg_advisory_unlock_all()":
                cleanup_started.set()
                await asyncio.Event().wait()
            return await original(conn, query, *args, **kwargs)

        async def operation():
            async with service.session(uuid4().hex) as (conn, _):
                backend.append(conn.info.backend_pid)

        with monkeypatch.context() as patch:
            patch.setattr(psycopg.AsyncConnection, "execute", delayed_unlock)
            task = asyncio.create_task(operation())
            await asyncio.wait_for(cleanup_started.wait(), 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with service.session() as (conn, _):
            assert conn.info.backend_pid != backend[0]


async def test_api_lifespan_owns_and_closes_approval_pool(url, monkeypatch):
    from apps.api.main import app, lifespan
    from apps.api.settings import Settings

    monkeypatch.setattr(
        "apps.api.main.get_settings", lambda: Settings(_env_file=None, database_url=url)
    )
    async with lifespan(app):
        async with app.state.approvals.session():
            pass
        pool = app.state.approvals.pool
        assert not pool.closed
    assert pool.closed


def test_supplier_lifespan_owns_and_closes_pool(url, monkeypatch):
    from apps.api.settings import Settings
    from apps.mockcorp.main import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        "apps.mockcorp.main.get_settings", lambda: Settings(_env_file=None, database_url=url)
    )
    app = create_app(settings=MockSettings(_env_file=None))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        pool = app.state.mockcorp.store.pool
        assert not pool.closed
    assert pool.closed
