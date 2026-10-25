"""Read-only receipt reconciliation; never replay or compensate a write."""

import hashlib
import json
from typing import Literal

from core.graph.approval_service import digest
from core.graph.contracts import Operation, StrictModel
from core.tools.compensation import compensation_key
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, model_validator

from apps.api.auth import SubjectDep

router = APIRouter()


class EvidenceTarget(StrictModel):
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")
    operation: Operation
    key: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,200}$")
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class EvidenceQuery(StrictModel):
    targets: list[EvidenceTarget] = Field(min_length=1, max_length=40)


class ReceiptEvidence(StrictModel):
    step_id: str
    operation: Operation
    key: str
    fingerprint: str
    forward: Literal["committed", "missing", "invalid"]
    compensation: Literal["committed", "missing", "invalid"]
    state: Literal["compensated", "retained", "changed", "unconfirmed"]
    receipt_hash: str
    state_hash: str

    @model_validator(mode="after")
    def state_matches_receipts(self):
        if self.compensation == "committed" and self.forward != "committed":
            raise ValueError("compensation_requires_forward_receipt")
        if self.forward == "invalid" or self.compensation == "invalid":
            if self.state != "unconfirmed":
                raise ValueError("invalid_receipt_must_be_unconfirmed")
        elif self.state == "retained" and not (
            self.forward == "committed" and self.compensation == "missing"
        ):
            raise ValueError("retained_requires_forward_receipt")
        elif self.state == "compensated" and not (
            self.forward == "committed" and self.compensation == "committed"
        ):
            raise ValueError("compensated_requires_both_receipts")
        elif self.state == "changed" and self.forward != "committed":
            raise ValueError("changed_requires_receipt")
        return self


def receipt_evidence(state, receipts, target):
    """Only hashes and status leave the supplier; raw before-images can contain PII."""
    source = receipts.get(target.key)
    undo = receipts.get(compensation_key(target.key))

    def valid(receipt, operation):
        return bool(
            receipt
            and 200 <= receipt["status"] < 300
            and receipt["body"].get("operation") == operation
            and receipt["body"].get("dry_run") is False
            and isinstance(receipt["body"].get("diff"), list)
        )

    forward = (
        "missing"
        if source is None
        else (
            "committed"
            if valid(source, target.operation) and source["fingerprint"] == target.fingerprint
            else "invalid"
        )
    )
    expected = hashlib.sha256(json.dumps([target.key, target.operation]).encode()).hexdigest()
    compensated = (
        forward == "committed"
        and valid(undo, "saga.compensate")
        and undo["fingerprint"] == expected
    )
    compensation = "missing" if undo is None else "committed" if compensated else "invalid"
    selected = undo if compensated else source if forward == "committed" else None
    observed = []
    matches = selected is not None
    if selected:
        for change in selected["body"]["diff"]:
            # Shared account balance legitimately changes for unrelated orders. Order and
            # refund rows prove this refund; comparing the global balance would be spurious.
            if change["collection"] == "balance":
                continue
            current = state.get(change["collection"], {}).get(change["id"])
            observed.append([change["collection"], change["id"], current])
            matches = matches and current == change["after"]
    status = (
        "unconfirmed"
        if "invalid" in {forward, compensation} or selected is None
        else "changed"
        if not matches
        else "compensated"
        if compensated
        else "retained"
    )
    return ReceiptEvidence(
        **target.model_dump(),
        forward=forward,
        compensation=compensation,
        state=status,
        receipt_hash=digest([source, undo]),
        state_hash=digest(observed),
    ).model_dump()


@router.post("/v1/reconciliation/evidence", tags=["inspection"])
def evidence(body: EvidenceQuery, subject: SubjectDep, request: Request):
    # The caller-facing API also authorizes approval.reconcile/resolve through OPA.
    # Supplier access uses the same server-authenticated identity, never a tenant body field.
    if subject.role != "admin":
        raise HTTPException(403, "reconciliation_policy_denied")
    store = request.app.state.mockcorp.store
    return {
        "tenant": subject.tenant,
        "items": store.reconciliation_evidence(subject.tenant, body.targets),
    }
