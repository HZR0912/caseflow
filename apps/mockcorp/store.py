"""Postgres 事务存储；同租户串行执行，业务状态和幂等结果原子提交。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url

from apps.mockcorp.domain import initial_state
from apps.mockcorp.models import MockSettings


@dataclass
class Reply:
    status: int
    body: dict
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Outcome:
    reply: Reply
    persist: bool = False
    fingerprint: str = ""


Transaction = Callable[[dict, dict | None], Outcome]


class Store(Protocol):
    def transact(self, tenant: str, key: str | None, handler: Transaction) -> Reply: ...
    def snapshot(self, tenant: str) -> dict: ...
    def health(self) -> None: ...


class PostgresStore:
    def __init__(self, database_url: str, settings: MockSettings):
        self.url = (
            make_url(database_url)
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
        self.settings = settings

    def health(self) -> None:
        with psycopg.connect(self.url, connect_timeout=5) as conn:
            conn.execute("SELECT 1 FROM mockcorp_state LIMIT 1")
            conn.execute("SELECT 1 FROM mockcorp_receipts LIMIT 1")

    def snapshot(self, tenant: str) -> dict:
        with psycopg.connect(self.url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT state FROM mockcorp_state WHERE tenant = %s", (tenant,)
            ).fetchone()
            return row[0] if row else initial_state(self.settings)

    def transact(self, tenant: str, key: str | None, handler: Transaction) -> Reply:
        with psycopg.connect(self.url, connect_timeout=5) as conn:
            # 即使租户首次出现，也由唯一键冲突等待和行锁保护并发初始化。
            conn.execute(
                "INSERT INTO mockcorp_state (tenant, state) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (tenant, Jsonb(initial_state(self.settings))),
            )
            state = conn.execute(
                "SELECT state FROM mockcorp_state WHERE tenant = %s FOR UPDATE", (tenant,)
            ).fetchone()[0]
            receipt = None
            if key is not None:
                row = conn.execute(
                    "SELECT fingerprint, status_code, response FROM mockcorp_receipts "
                    "WHERE tenant = %s AND key = %s",
                    (tenant, key),
                ).fetchone()
                if row:
                    receipt = {"fingerprint": row[0], "status": row[1], "body": row[2]}
            outcome = handler(state, receipt)
            if outcome.persist:
                assert key is not None
                conn.execute(
                    "UPDATE mockcorp_state SET state = %s WHERE tenant = %s", (Jsonb(state), tenant)
                )
                conn.execute(
                    "INSERT INTO mockcorp_receipts "
                    "(tenant, key, fingerprint, status_code, response) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        tenant,
                        key,
                        outcome.fingerprint,
                        outcome.reply.status,
                        Jsonb(outcome.reply.body),
                    ),
                )
            else:
                # dry-run、业务错误、瞬态故障以及重放都不提交任何变更。
                conn.rollback()
            return outcome.reply
