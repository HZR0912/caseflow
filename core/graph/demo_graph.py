"""W1 最小 LangGraph 图：echo -> approval(interrupt) -> report。

这张图的唯一目的是验证 W1 的三条硬要求：
1. 每步 checkpoint 落 Postgres（PostgresSaver）
2. `interrupt()` 能把执行挂起并持久化到进程之外
3. 新进程用同一个 thread_id 能从断点继续，而不是从头重跑

**关于 interrupt 的坑（现在就写进注释，W6 别踩第二次）：**
节点在恢复时会**从节点开头重新执行**，interrupt() 那一次调用只是"读回"恢复值。
所以 interrupt 之前的所有代码必须是幂等且无外部副作用的 —— 否则重跑一遍
就会重复发一封邮件、重复扣一次款。这正是 §2.2 里"恢复不是从头重跑"的实现细节。
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from core.graph.state import DemoState


def _with_step(state: DemoState, step: str) -> list[str]:
    """trace 用追加语义：恢复后能从 trace 看出哪些节点真的执行过。"""
    return [*state.get("trace", []), step]


def echo_node(state: DemoState) -> dict:
    return {"echo": f"echo:{state.get('task', '')}", "trace": _with_step(state, "echo")}


def approval_node(state: DemoState) -> dict:
    # interrupt 之前保持无副作用：恢复时本节点会从头重跑
    decision = interrupt(
        {
            "question": "是否允许对 repo frontend-web 授权写权限？",
            "task": state.get("task"),
            "risk": "high",
        }
    )
    return {"approval": str(decision), "trace": _with_step(state, f"approval:{decision}")}


def report_node(state: DemoState) -> dict:
    steps = state.get("trace", [])
    return {
        "report": f"task={state.get('task')} approval={state.get('approval')} steps={len(steps)}",
        "trace": _with_step(state, "report"),
    }


def build_demo_graph(checkpointer=None):
    """checkpointer 为 None 时图只能在同一进程内跑（用于纯单测）。"""
    builder = StateGraph(DemoState)
    builder.add_node("echo", echo_node)
    builder.add_node("approval", approval_node)
    builder.add_node("report", report_node)
    builder.add_edge(START, "echo")
    builder.add_edge("echo", "approval")
    builder.add_edge("approval", "report")
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)
