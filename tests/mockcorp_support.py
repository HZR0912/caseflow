"""单测替身只模拟事务接口；持久化与并发保证另用真实 PostgreSQL 验证。"""

from copy import deepcopy
from threading import Lock

from apps.mockcorp.domain import initial_state


class MemoryStore:
    def __init__(self, settings):
        self.settings = settings
        self.states = {}
        self.receipts = {}
        self.lock = Lock()

    def health(self):
        pass

    def snapshot(self, tenant):
        return deepcopy(self.states.get(tenant, initial_state(self.settings)))

    def transact(self, tenant, key, handler):
        with self.lock:
            state = self.snapshot(tenant)
            outcome = handler(state, deepcopy(self.receipts.get((tenant, key))))
            if outcome.persist:
                self.states[tenant] = deepcopy(state)
                self.receipts[tenant, key] = {
                    "fingerprint": outcome.fingerprint,
                    "status": outcome.reply.status,
                    "body": deepcopy(outcome.reply.body),
                }
            return outcome.reply
