"""Fail-closed receipt contracts and supplier evidence classification."""

import hashlib
import json

import httpx
import pytest
from apps.mockcorp.reconciliation import EvidenceTarget, receipt_evidence
from core.graph.approval_service import ApprovalError
from core.tools.compensation import compensation_key
from core.tools.reconciliation import HttpEvidenceGateway, ReconciliationService


def target():
    return EvidenceTarget(
        step_id="order", operation="money.create_order", key="source-key", fingerprint="f" * 64
    )


def test_matching_undo_receipt_proves_refund_without_exposing_source_pii():
    item = target()
    source = {
        "fingerprint": "f" * 64,
        "status": 201,
        "body": {
            "operation": "money.create_order",
            "dry_run": False,
            "diff": [
                {"collection": "orders", "id": "o1", "after": {"email": "private@example.com"}}
            ],
        },
    }
    undo = {
        "fingerprint": hashlib.sha256(json.dumps([item.key, item.operation]).encode()).hexdigest(),
        "status": 200,
        "body": {
            "operation": "saga.compensate",
            "dry_run": False,
            "diff": [
                {"collection": "orders", "id": "o1", "after": {"refunded": True}},
                {"collection": "balance", "before": 0, "after": 100},
            ],
        },
    }
    receipts = {item.key: source, compensation_key(item.key): undo}
    result = receipt_evidence(
        {"orders": {"o1": {"refunded": True}}, "balance_cents": 999}, receipts, item
    )
    assert result["state"] == "compensated"
    assert "private@example.com" not in json.dumps(result)
    undo["fingerprint"] = "wrong-source"
    assert receipt_evidence({"orders": {}}, receipts, item)["state"] == "unconfirmed"


@pytest.mark.parametrize(
    "body",
    [
        {"tenant": "other", "items": []},
        {"tenant": "acme", "items": []},
        {"tenant": "acme", "items": [{"step_id": "wrong"}]},
        [],
    ],
)
async def test_gateway_rejects_invalid_scope_or_receipt(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        gateway = HttpEvidenceGateway("http://supplier", "Bearer opaque", client)
        with pytest.raises(ApprovalError, match="reconciliation_invalid_supplier_evidence"):
            await gateway.inspect("acme", [target()])


async def test_gateway_rejects_semantically_impossible_receipt_state():
    item = target().model_dump()
    body = {
        "tenant": "acme",
        "items": [
            {
                **item,
                "forward": "missing",
                "compensation": "missing",
                "state": "compensated",
                "receipt_hash": "a" * 64,
                "state_hash": "b" * 64,
            }
        ],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        gateway = HttpEvidenceGateway("http://supplier", "Bearer opaque", client)
        with pytest.raises(ApprovalError, match="reconciliation_invalid_supplier_evidence"):
            await gateway.inspect("acme", [target()])


@pytest.mark.parametrize("phase", ["forward", "undo", "missing_success"])
async def test_missing_receipts_never_close_unknown_or_contradict_recorded_success(phase):
    class Evidence:
        async def inspect(self, tenant, targets):
            return [receipt_evidence({}, {}, t) for t in targets]

    entry = {
        "step_id": "order",
        "operation": "money.create_order",
        "key": "source-key",
        "status": "failed",
        "outcome_unknown": phase == "forward",
    }
    journal = {"steps": [entry]}
    if phase == "undo":
        journal["saga"] = {"steps": [{"step_id": "order", "outcome_unknown": True}]}
    if phase == "missing_success":
        entry["status"] = "succeeded"
    result = await ReconciliationService(None, Evidence()).collect(journal, "acme", [target()])
    assert result["items"][0]["state"] == "unconfirmed"
    assert not result["can_accept_observed_state"]
