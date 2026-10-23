"""集成测试：断点恢复真的跨连接/跨"进程"生效。

为什么必须用真 Postgres 跑：这一层要证明的是"状态在数据库里，不在内存里"。
用内存 saver 测这件事等于什么都没证明。

本地跑法：

    docker compose up -d postgres
    $env:DATABASE_URL="postgresql://caseflow:caseflow_dev_pw@127.0.0.1:5432/caseflow"
    .venv\\Scripts\\python.exe -m pytest -m integration -q

CI 默认只跑 `-m "not integration"`；带 postgres service 的 job 单独跑这组。
"""

from __future__ import annotations

import os
import uuid

import pytest
from core.graph.demo_graph import build_demo_graph
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

pytestmark = pytest.mark.integration

DATABASE_URL = os.getenv("DATABASE_URL")


def _require_db() -> str:
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL 未设置：集成测试需要真实 postgres")
    return DATABASE_URL


def test_interrupt_then_resume_in_separate_connection() -> None:
    url = _require_db()
    thread_id = f"it-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}

    # —— 第一个连接：跑到审批中断，然后关连接（等价于进程退出）
    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()
        graph = build_demo_graph(saver)
        first = graph.invoke({"task": "开环境"}, config)

        assert "__interrupt__" in first
        assert graph.get_state(config).next == ("approval",)

    # —— 第二个连接：全新 saver + graph 实例，只靠 thread_id 恢复
    with PostgresSaver.from_conn_string(url) as saver:
        graph = build_demo_graph(saver)
        resumed = graph.invoke(Command(resume="approved"), config)

        assert resumed["approval"] == "approved"
        assert resumed["trace"] == ["echo", "approval:approved", "report"]
        assert graph.get_state(config).next == ()

    # 清理，避免本地反复跑时堆积
    import psycopg

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            cur.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))
