"""W1 验收脚本：跨进程断点恢复（杀掉进程后从 checkpoint 继续）。

用法（两次调用是两个独立进程，中间什么都不保留在内存里）：

    python -m scripts.w1_checkpoint_demo start     # 走到审批节点 -> interrupt -> 进程退出
    python -m scripts.w1_checkpoint_demo resume    # 新进程按 thread_id 恢复并跑完
    python -m scripts.w1_checkpoint_demo status    # 直接查 checkpoint 表
    python -m scripts.w1_checkpoint_demo reset     # 清掉本 thread 的 checkpoint

这个脚本就是 W1 的验收证据：恢复靠的是 Postgres 里的 checkpoint，不是进程内存。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from core.graph.demo_graph import build_demo_graph
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

DEFAULT_THREAD = "w1-demo-thread"


def _conn_string() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    from apps.api.settings import get_settings

    return get_settings().database_url


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _print_state(result: dict) -> None:
    printable = {k: v for k, v in result.items() if k != "__interrupt__"}
    print(json.dumps(printable, ensure_ascii=False, indent=2, default=str))
    if interrupts := result.get("__interrupt__"):
        payload = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts
        print(f"\n[interrupt] {json.dumps(payload, ensure_ascii=False)}")


def cmd_start(thread_id: str) -> int:
    with PostgresSaver.from_conn_string(_conn_string()) as saver:
        saver.setup()  # 幂等：建 checkpoint 相关表
        graph = build_demo_graph(saver)
        result = graph.invoke(
            {"task": "给 3 名前端实习生开环境（含仓库写权限）"}, _config(thread_id)
        )
        # get_state 必须在连接关闭前调用：PostgresSaver 的连接随 with 退出即释放
        pending = graph.get_state(_config(thread_id)).next

    _print_state(result)
    print(f"\n[next] {pending}    ← 非空表示图已挂起，等外部事件")
    print("[note] 进程已退出。继续走 resume 命令，恢复只依赖 Postgres。")
    return 0


def cmd_resume(thread_id: str, decision: str) -> int:
    with PostgresSaver.from_conn_string(_conn_string()) as saver:
        saver.setup()
        graph = build_demo_graph(saver)

        before = graph.get_state(_config(thread_id))
        if not before.next:
            print(f"[error] thread {thread_id!r} 没有挂起的节点，先跑 start", file=sys.stderr)
            return 1
        print(f"[resume] 断点处待执行节点：{before.next}")

        result = graph.invoke(Command(resume=decision), _config(thread_id))
        pending = graph.get_state(_config(thread_id)).next

    _print_state(result)
    print(f"\n[next] {pending}    ← 空列表表示已跑完")
    return 0


def cmd_status(thread_id: str) -> int:
    with PostgresSaver.from_conn_string(_conn_string()) as saver:
        saver.setup()
        graph = build_demo_graph(saver)
        snapshot = graph.get_state(_config(thread_id))

    print(f"thread_id : {thread_id}")
    print(f"next      : {snapshot.next}")
    print(f"values    : {json.dumps(snapshot.values, ensure_ascii=False, default=str)}")
    return 0


def cmd_reset(thread_id: str) -> int:
    """清掉本 thread 的 checkpoint（不是业务数据），方便重复演示。"""
    import psycopg

    with psycopg.connect(_conn_string()) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,))
        cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (thread_id,))
        cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,))
    print(f"[reset] {thread_id} 的 checkpoint 已清空")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="W1 断点恢复验证")
    parser.add_argument("command", choices=["start", "resume", "status", "reset"])
    parser.add_argument("--thread", default=DEFAULT_THREAD)
    parser.add_argument("--decision", default="approved", help="审批结果（resume 用）")
    args = parser.parse_args(argv)

    if args.command == "start":
        return cmd_start(args.thread)
    if args.command == "resume":
        return cmd_resume(args.thread, args.decision)
    if args.command == "status":
        return cmd_status(args.thread)
    return cmd_reset(args.thread)


if __name__ == "__main__":
    raise SystemExit(main())
