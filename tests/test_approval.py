from uuid import uuid4

import pytest
from apps.api.main import app
from core.graph.approval import ApprovalDecision, build_approval_graph
from core.graph.approval_service import digest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import ValidationError


@pytest.mark.parametrize("decision,status", [("approve", "approved_ready"), ("reject", "rejected")])
def test_interrupt_resume_new_graph_and_never_execute(decision, status):
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": str(uuid4())}}
    graph = build_approval_graph(saver)
    first = graph.invoke({"request_id": "r", "plan_hash": "a" * 64, "snapshot": {}}, config)
    assert first["__interrupt__"] and first["status"] == "awaiting_approval"
    assert graph.get_state(config).next == ("approval",)
    resumed = build_approval_graph(saver).invoke(
        Command(
            resume={
                "decision": decision,
                "plan_hash": "a" * 64,
                "actor": "reviewer",
                "reason": "checked",
            }
        ),
        config,
    )
    assert resumed["status"] == status
    assert resumed["trace"] == ["review", "approval:" + decision, "report"]
    assert build_approval_graph(saver).get_state(config).next == ()


def test_resume_plan_hash_mismatch_does_not_approve():
    graph = build_approval_graph(InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    graph.invoke({"request_id": "r", "plan_hash": "a" * 64, "snapshot": {}}, config)
    with pytest.raises(ValueError, match="approval_plan_mismatch"):
        graph.invoke(Command(resume={"decision": "approve", "plan_hash": "b" * 64}), config)


def test_decision_requires_reason_on_rejection_and_forbids_forged_actor():
    data = {"decision": "reject", "plan_hash": "a" * 64, "operation_id": str(uuid4())}
    with pytest.raises(ValidationError, match="rejection_reason_required"):
        ApprovalDecision(**data)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ApprovalDecision(**data, reason="reason", actor="admin")


def test_digest_binds_full_snapshot_but_is_key_order_independent():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"plan": {}, "preview": {"ok": True}}) != digest(
        {"plan": {}, "preview": {"ok": False}}
    )


def test_approval_endpoints_require_auth_before_touching_db():
    with TestClient(app) as client:
        assert client.get("/v1/approval-requests").status_code == 401
        assert client.get("/v1/approval-requests/identity").status_code == 401
        assert client.get("/v1/approval-requests/" + str(uuid4())).status_code == 401
        assert client.post("/v1/approval-requests", json={"text": "入职"}).status_code == 401
        assert client.post("/v1/approval-requests/" + str(uuid4()) + "/recover").status_code == 401
