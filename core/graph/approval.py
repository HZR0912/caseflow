"""Durable W6 graph: inputs are frozen, redacted W5 results, never raw requests."""

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import Field, model_validator

from core.graph.contracts import StrictModel
from core.obs.runtime import node


class ApprovalDecision(StrictModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=1000)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f-]{27}$")

    @model_validator(mode="after")
    def rejection_reason(self):
        self.reason = self.reason.strip()
        if self.decision == "reject" and not self.reason:
            raise ValueError("rejection_reason_required")
        return self


class ApprovalState(TypedDict, total=False):
    request_id: str
    snapshot: dict
    plan_hash: str
    status: str
    trace: list[str]
    approval: dict
    report: str


def build_approval_graph(checkpointer):
    def review(state):
        return {"status": "awaiting_approval", "trace": ["review"]}

    def approval(state):
        # This node restarts on resume. No IO or other side effects before interrupt.
        choice = interrupt({"request_id": state["request_id"], "plan_hash": state["plan_hash"]})
        if choice.get("plan_hash") != state["plan_hash"]:
            raise ValueError("approval_plan_mismatch")
        if choice.get("decision") not in {"approve", "reject"}:
            raise ValueError("invalid_approval_decision")
        return {"approval": choice, "trace": [*state["trace"], "approval:" + choice["decision"]]}

    def report(state):
        approved = state["approval"]["decision"] == "approve"
        return {
            "status": "approved_ready" if approved else "rejected",
            "report": "审批通过；审批本身未修改业务数据，实际进度请查看执行记录。"
            if approved
            else "申请已拒绝，未执行。",
            "trace": [*state["trace"], "report"],
        }

    graph = StateGraph(ApprovalState)
    graph.add_node("review", node("review", review))
    graph.add_node("approval", node("approval", approval))
    graph.add_node("report", node("report", report))
    graph.add_edge(START, "review")
    graph.add_edge("review", "approval")
    graph.add_edge("approval", "report")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=checkpointer)
