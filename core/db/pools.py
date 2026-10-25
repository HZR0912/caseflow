"""Bounded, lifespan-owned pools. Transactions and business retries stay with callers."""

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool


def supplier_pool(url, *, max_size=8, timeout=5, max_waiting=32):
    return ConnectionPool(
        url.replace("postgresql+psycopg://", "postgresql://"),
        min_size=0,
        max_size=max_size,
        timeout=timeout,
        max_waiting=max_waiting,
        open=False,
        kwargs={"connect_timeout": 5, "application_name": "caseflow-supplier"},
        check=ConnectionPool.check_connection,
        name="supplier",
    )


def approval_pool(url, *, max_size=8, timeout=5, max_waiting=32):
    return AsyncConnectionPool(
        url.replace("postgresql+psycopg://", "postgresql://"),
        min_size=0,
        max_size=max_size,
        timeout=timeout,
        max_waiting=max_waiting,
        open=False,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "prepare_threshold": 0,
            "connect_timeout": 5,
            "application_name": "caseflow-approval",
        },
        check=AsyncConnectionPool.check_connection,
        name="approval",
    )
