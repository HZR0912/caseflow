"""单测替身只模拟事务接口；持久化与并发保证另用真实 PostgreSQL 验证。"""

from copy import deepcopy
from threading import Lock

from apps.mockcorp.domain import initial_state


class MemoryStore:
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

    def transact(self, tenant, key, handler, *, operation="", actor=None):
        with self.lock:
            state = self.snapshot(tenant)
            try:
                outcome = handler(state, deepcopy(self.receipts.get((tenant, key))))
            except Exception:
                self._log(tenant, key, operation, actor, {"status": 500}, "error", "internal_error")
                raise
            if outcome.persist:
                self.states[tenant] = deepcopy(state)
                self.receipts[tenant, key] = {
                    "fingerprint": outcome.fingerprint,
                    "status": outcome.reply.status,
                    "body": deepcopy(outcome.reply.body),
                }
            self._log(
                tenant,
                key,
                operation,
                actor,
                outcome.reply,
                outcome.outcome_of(),
                outcome.log.error_code,
                outcome.log.result_id,
                outcome.fingerprint,
                outcome.log.dry_run,
                outcome.log.replayed,
            )
            return outcome.reply

    def _log(
        self,
        tenant,
        key,
        operation,
        actor,
        reply,
        outcome,
        error_code=None,
        result_id=None,
        fingerprint=None,
        dry_run=False,
        replayed=False,
    ):
        rows = self.logs.setdefault(tenant, [])
        status = reply.get("status") if isinstance(reply, dict) else reply.status
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
