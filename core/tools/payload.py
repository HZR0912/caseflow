"""Encrypt executable plans separately from redacted approval/checkpoint data."""

import json
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from core.graph.contracts import Plan


@dataclass
class PreparedApproval:
    snapshot: dict
    plan: Plan


class PlanCipher:
    def __init__(self, key: str):
        self.fernet = Fernet(key.encode())

    def seal(self, plan: Plan, tenant: str, request_id: str) -> str:
        data = {"tenant": tenant, "request_id": request_id, "plan": plan.model_dump(mode="json")}
        return self.fernet.encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()

    def open(self, token: str, tenant: str, request_id: str) -> Plan:
        try:
            data = json.loads(self.fernet.decrypt(token.encode()))
            if data["tenant"] != tenant or data["request_id"] != request_id:
                raise ValueError("payload_binding_mismatch")
            return Plan.model_validate(data["plan"])
        except (InvalidToken, ValueError, KeyError, TypeError) as exc:
            raise ValueError("execution_payload_invalid") from exc
