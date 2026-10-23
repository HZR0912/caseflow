from __future__ import annotations

import hashlib
import json
import time

from apps.mockcorp.domain import BusinessError, apply
from apps.mockcorp.models import MockSettings
from apps.mockcorp.store import Outcome, Reply, Store

COMMON_FAULTS = {"500", "429", "timeout", "slow", "timeout_after_commit"}
BUSINESS_FAULTS = {
    "duplicate_name": ({"identity.create_user"}, 409),
    "domain_policy": ({"identity.create_mailbox"}, 403),
    "seat_limit": ({"code.invite_member"}, 409),
    "invite_rate_limit": ({"code.invite_member"}, 429),
    "insufficient_funds": ({"money.create_order"}, 409),
    "approval_timeout": ({"money.create_order", "money.bind_seat", "money.refund"}, 504),
    "partial_success": ({"ticket.assign_tickets"}, 207),
}


def error_reply(status: int, code: str, *, retryable: bool = False) -> Reply:
    return Reply(
        status,
        {"error": {"code": code, "retryable": retryable}},
        {"Retry-After": "1"} if status == 429 else {},
    )


class MockCorp:
    def __init__(self, store: Store, settings: MockSettings):
        self.store = store
        self.settings = settings

    def execute(
        self,
        tenant: str,
        operation: str,
        payload: dict,
        *,
        key: str | None,
        dry_run: bool = False,
        fault: str | None = None,
    ) -> Reply:
        if not dry_run and key is None:
            return error_reply(400, "idempotency_key_required")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"operation": operation, "payload": payload},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        delay = False
        lost_response = False

        def handle(state: dict, receipt: dict | None) -> Outcome:
            nonlocal delay, lost_response
            if receipt is not None:
                if receipt["fingerprint"] != fingerprint:
                    return Outcome(error_reply(409, "idempotency_key_conflict"))
                return Outcome(
                    Reply(receipt["status"], receipt["body"], {"Idempotency-Replayed": "true"})
                )
            try:
                if fault:
                    if not self.settings.faults_enabled:
                        raise BusinessError(403, "fault_injection_disabled")
                    special = BUSINESS_FAULTS.get(fault)
                    if fault not in COMMON_FAULTS and (
                        special is None or operation not in special[0]
                    ):
                        raise BusinessError(422, "unsupported_fault")
                    if dry_run and fault == "timeout_after_commit":
                        raise BusinessError(422, "fault_requires_commit")
                    delay = fault in {"slow", "timeout", "timeout_after_commit", "approval_timeout"}
                    if fault in {"500", "429", "timeout"}:
                        code = {"500": 500, "429": 429, "timeout": 504}[fault]
                        raise BusinessError(code, f"injected_{fault}", retryable=True)
                    if special and fault != "partial_success":
                        raise BusinessError(special[1], fault, retryable=special[1] in {429, 504})
                result, diff, status = apply(
                    state, operation, payload, partial=fault == "partial_success"
                )
                lost_response = fault == "timeout_after_commit"
                return Outcome(
                    Reply(
                        200 if dry_run else status,
                        {
                            "operation": operation,
                            "dry_run": dry_run,
                            "result": result,
                            "diff": diff,
                        },
                        {"Idempotency-Replayed": "false"},
                    ),
                    persist=not dry_run,
                    fingerprint=fingerprint,
                )
            except BusinessError as exc:
                return Outcome(error_reply(exc.status, exc.code, retryable=exc.retryable))

        reply = self.store.transact(tenant, None if dry_run else key, handle)
        # 不在睡眠期间持有数据库锁；提交后响应丢失可由另一个请求立即重放恢复。
        if delay:
            time.sleep(self.settings.fault_delay_s)
        if lost_response:
            return error_reply(504, "injected_timeout_after_commit", retryable=True)
        return reply
