"""Postgres 事务存储；同租户串行执行，业务状态、幂等收据与操作日志一致落库。

日志与业务的关系（ADR-0004）：
- 成功路径：业务状态 + 幂等收据 + 日志行在同一事务提交，三者要么都在、要么都不在。
- 失败/预演/重放路径：业务变更回滚，但日志行**单独提交**——日志的职责是记录
  "系统收到过这次尝试"，尝试没落地不代表没发生。这是它和业务表唯一的不对称。
- 处理过程中抛出未预期异常：先补一条 outcome=error 的日志再抛出，
  既保留现场，又不掩盖异常本身。
"""

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
class LogEntry:
    """一次写尝试的日志事实，由业务层填写、存储层负责落库。"""

    operation: str = ""
    actor: str | None = None
    dry_run: bool = False
    replayed: bool = False
    error_code: str | None = None
    result_id: str | None = None


@dataclass
class Outcome:
    reply: Reply
    persist: bool = False
    fingerprint: str = ""
    log: LogEntry = field(default_factory=LogEntry)

    def outcome_of(self) -> str:
        # 未预期异常走 error；其余按状态码分。这个分支必须显式写出来：
        # 最初只有三个返回值，处理器抛异常时落库的是 rejected，而单测替身
        # MemoryStore 手写硬编码了 "error"，两者不一致导致生产代码的 bug 被掩盖。
        if self.reply.status >= 500 and self.log.error_code in (None, "internal_error"):
            return "error"
        if self.log.replayed:
            return "committed" if self.persist or self.reply.status < 400 else "rejected"
        if 200 <= self.reply.status < 300:
            return "dry_run" if self.log.dry_run else "committed"
        return "rejected"


Transaction = Callable[[dict, dict | None], Outcome]


class Store(Protocol):
    def transact(
        self,
        tenant: str,
        key: str | None,
        handler: Transaction,
        *,
        operation: str = "",
        actor: str | None = None,
        source_key: str | None = None,
    ) -> Reply: ...
    def snapshot(self, tenant: str) -> dict: ...
    def oplog(self, tenant: str, after_id: int = 0, limit: int = 100) -> dict: ...
    def health(self) -> None: ...


class PostgresStore:
    def __init__(self, database_url: str, settings: MockSettings, *, pool=None):
        self.url = (
            make_url(database_url)
            .set(drivername="postgresql")
            .render_as_string(hide_password=False)
        )
        self.settings = settings
        self.pool = pool

    def _connect(self):
        # The app/evaluation owns the pool. One-shot scripts may use a direct connection.
        if self.pool is not None:
            return self.pool.connection()
        return psycopg.connect(self.url, connect_timeout=5)

    def health(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT 1 FROM mockcorp_state LIMIT 1")
            conn.execute("SELECT 1 FROM mockcorp_receipts LIMIT 1")
            conn.execute("SELECT 1 FROM mockcorp_oplog LIMIT 1")

    def snapshot(self, tenant: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state FROM mockcorp_state WHERE tenant = %s", (tenant,)
            ).fetchone()
            return row[0] if row else initial_state(self.settings)

    def oplog(self, tenant: str, after_id: int = 0, limit: int = 100) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, operation, actor, dry_run, replayed, status_code, outcome, "
                "error_code, result_id, idempotency_key, created_at "
                "FROM mockcorp_oplog WHERE tenant = %s AND id > %s ORDER BY id LIMIT %s",
                (tenant, after_id, limit + 1),
            ).fetchall()
        items = [
            dict(
                zip(
                    (
                        "id",
                        "operation",
                        "actor",
                        "dry_run",
                        "replayed",
                        "status_code",
                        "outcome",
                        "error_code",
                        "result_id",
                        "idempotency_key",
                        "created_at",
                    ),
                    row,
                    strict=True,
                )
            )
            for row in rows[:limit]
        ]
        return {
            "items": [jsonable(item) for item in items],
            "next_after_id": items[-1]["id"] if len(rows) > limit else None,
        }

    def _write_log(
        self,
        conn,
        tenant: str,
        key: str | None,
        outcome: Outcome,
        *,
        operation: str = "",
        actor: str | None = None,
    ) -> None:
        log = outcome.log
        conn.execute(
            "INSERT INTO mockcorp_oplog (tenant, operation, actor, dry_run, replayed, "
            "status_code, outcome, error_code, result_id, fingerprint, idempotency_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                tenant,
                log.operation or operation,
                log.actor if log.actor is not None else actor,
                log.dry_run,
                log.replayed,
                outcome.reply.status,
                outcome.outcome_of(),
                log.error_code,
                log.result_id,
                outcome.fingerprint or None,
                key,
            ),
        )

    def transact(
        self,
        tenant: str,
        key: str | None,
        handler: Transaction,
        *,
        operation: str = "",
        actor: str | None = None,
        source_key: str | None = None,
    ) -> Reply:
        with self._connect() as conn:
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
            try:
                if source_key is not None:
                    source = conn.execute(
                        "SELECT response FROM mockcorp_receipts WHERE tenant=%s AND key=%s",
                        (tenant, source_key),
                    ).fetchone()
                    outcome = handler(state, receipt, source[0] if source else None)
                else:
                    outcome = handler(state, receipt)
            except Exception:
                # 未预期异常：业务回滚，但留下一条 error 日志再抛出。
                entry = Outcome(
                    reply=Reply(500, {}),
                    log=LogEntry(operation=operation, actor=actor, error_code="internal_error"),
                )
                conn.rollback()
                self._write_log(conn, tenant, key, entry, operation=operation, actor=actor)
                conn.commit()
                raise
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
                self._write_log(conn, tenant, key, outcome, operation=operation, actor=actor)
                # 由上下文管理器提交：业务、收据、日志同生共死。
            else:
                # dry-run、业务错误、瞬态故障以及重放都不改业务状态，
                # 但日志要独立提交——记录的是"尝试发生过"。
                conn.rollback()
                self._write_log(conn, tenant, key, outcome, operation=operation, actor=actor)
                conn.commit()
            return outcome.reply


def jsonable(value):
    """created_at 等类型转成可 JSON 序列化的形式（供 HTTP 层直接返回）。"""
    from datetime import date, datetime

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
