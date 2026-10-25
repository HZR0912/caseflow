"""单测替身只模拟事务接口；持久化与并发保证另用真实 PostgreSQL 验证。"""

from copy import deepcopy
from threading import Lock

from apps.mockcorp.domain import initial_state
from apps.mockcorp.store import LogEntry, Outcome, Reply


class MemoryStore:
    """单测替身。**outcome 一律由生产代码的 `Outcome.outcome_of()` 决定**——
    替身自己手写一套分类（早期版本硬编码了 "error"）会让替身与生产实现漂移，
    于是生产代码的 bug 在单测里被掩盖、只在真库集成测试里才炸。"""

    def __init__(self, settings):
        self.settings = settings
        self.states = {}
        self.receipts = {}
        self.logs = {}
        self.lock = Lock()

    def health(self):
        pass

    def snapshot(self, tenant):
        return deepcopy(self.states.get(tenant, initial_state(self.settings)))

    def oplog(self, tenant, after_id=0, limit=100):
        rows = [row for row in self.logs.get(tenant, []) if row["id"] > after_id]
        items = deepcopy(rows[:limit])
        return {
            "items": items,
            "next_after_id": items[-1]["id"] if len(rows) > limit else None,
        }

    def transact(self, tenant, key, handler, *, operation="", actor=None, source_key=None):
        with self.lock:
            state = self.snapshot(tenant)
            try:
                receipt = deepcopy(self.receipts.get((tenant, key)))
                if source_key is not None:
                    source = deepcopy(self.receipts.get((tenant, source_key)))
                    outcome = handler(state, receipt, source["body"] if source else None)
                else:
                    outcome = handler(state, receipt)
            except Exception:
                # 与 PostgresStore 保持一致：走生产代码的 Outcome 分类，不手写字面量
                failure = Outcome(
                    reply=Reply(500, {}),
                    log=LogEntry(operation=operation, actor=actor, error_code="internal_error"),
                )
                self._record(tenant, key, failure, operation, actor)
                raise
            if outcome.persist:
                self.states[tenant] = deepcopy(state)
                self.receipts[tenant, key] = {
                    "fingerprint": outcome.fingerprint,
                    "status": outcome.reply.status,
                    "body": deepcopy(outcome.reply.body),
                }
            self._record(tenant, key, outcome, operation, actor)
            return outcome.reply

    def _record(self, tenant, key, outcome, operation, actor):
        self._log(
            tenant,
            key,
            outcome.log.operation or operation,
            outcome.log.actor if outcome.log.actor is not None else actor,
            outcome.reply.status,
            outcome.outcome_of(),
            outcome.log.error_code,
            outcome.log.result_id,
            outcome.log.dry_run,
            outcome.log.replayed,
        )

    def _log(
        self,
        tenant,
        key,
        operation,
        actor,
        status,
        outcome,
        error_code=None,
        result_id=None,
        dry_run=False,
        replayed=False,
    ):
        rows = self.logs.setdefault(tenant, [])
        rows.append(
            {
                "id": len(rows) + 1,
                "operation": operation,
                "actor": actor,
                "dry_run": dry_run,
                "replayed": replayed,
                "status_code": status,
                "outcome": outcome,
                "error_code": error_code,
                "result_id": result_id,
                "idempotency_key": key,
                "created_at": None,
            }
        )
