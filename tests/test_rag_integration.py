"""W3 线上路径：真实 Postgres + pgvector + 全文索引。

离线测试（tests/test_rag.py）覆盖排序与版本逻辑，但它跑不到 SQL：
生成列 `tsv`、`<=>` 余弦算子、`to_tsquery` 的中文表现，都只有连库才能验证。
这组测试用 `HashingEmbedder` 而不调 API——测的是**存储与检索管线**，
不是 embedding 的语义质量，后者由 `eval/run_rag_eval.py --database-url` 单独报告。
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from core.rag.chunking import load_corpus
from core.rag.embedder import HashingEmbedder
from core.rag.store import PolicyStore

pytestmark = pytest.mark.integration

POLICIES = Path(__file__).resolve().parents[1] / "data" / "policies"
AS_OF = date(2026, 4, 1)
EARLY = date(2025, 6, 1)


def _fresh_table(url: str) -> str:
    """每次建一张独立空表，避免模块级 fixture 的数据把"清空向量"这类测试搞成假阳性。"""
    table = f"policy_chunks_test_{uuid4().hex[:8]}"
    with psycopg.connect(url) as conn:
        conn.execute(
            f"""
            CREATE TABLE {table} (
                chunk_id TEXT PRIMARY KEY,
                doc_key TEXT NOT NULL,
                version TEXT NOT NULL,
                title TEXT NOT NULL,
                section_path TEXT NOT NULL,
                section_key TEXT NOT NULL,
                text TEXT NOT NULL,
                char_start INT NOT NULL,
                char_end INT NOT NULL,
                effective_date DATE NOT NULL,
                superseded_date DATE,
                source TEXT NOT NULL,
                tokens TEXT NOT NULL DEFAULT '',
                tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', tokens)) STORED,
                embedding vector(1024)
            )
            """
        )
        conn.execute(f"CREATE INDEX ON {table} USING gin (tsv)")
    return table


def _drop(url: str, table: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


@pytest.fixture(scope="module")
def store():
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required for real PostgreSQL integration")
    table = _fresh_table(url)
    policy_store = PolicyStore(url, HashingEmbedder(), table=table)
    chunks, _ = load_corpus(POLICIES)
    policy_store.ingest(chunks)
    try:
        yield policy_store, chunks
    finally:
        _drop(url, table)


def test_ingest_is_idempotent(store):
    policy_store, chunks = store
    assert policy_store.ingest(chunks) == len(chunks)
    with psycopg.connect(policy_store.url) as conn:
        count = conn.execute(f"SELECT count(*) FROM {policy_store.table}").fetchone()[0]
    assert count == len(chunks)


def test_text_route_works_without_embeddings():
    """**独立建表、只写文本**，确保向量路真的没有候选。

    原先这个测试复用模块级 fixture 的表，`ingest(embed=False)` 又因 COALESCE 没清掉旧向量，
    于是它实际是走向量路通过的——全文路回 0 行它照样绿。现在两者都堵上了。
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required for real PostgreSQL integration")
    table = _fresh_table(url)
    try:
        policy_store = PolicyStore(url, HashingEmbedder(), table=table)
        chunks, _ = load_corpus(POLICIES)
        policy_store.ingest(chunks, embed=False)

        with psycopg.connect(url) as conn:
            nulls = conn.execute(
                f"SELECT count(*) FROM {table} WHERE embedding IS NULL"
            ).fetchone()[0]
        assert nulls == len(chunks), "ingest(embed=False) 必须真的把向量清空"

        ranked = policy_store.search("生产环境权限最长多少天", top_k=5, as_of=AS_OF)
        assert "route0" not in ranked.routes, "没有向量时不应出现向量路"
        assert "access-control@2026.1#006" in [hit.chunk_id for hit in ranked.hits]
    finally:
        _drop(url, table)


def test_text_route_returns_hits_for_every_eval_case():
    """回归：plainto_tsquery 的 AND 语义曾让 12 条评测查询全部 0 命中。"""
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required for real PostgreSQL integration")
    table = _fresh_table(url)
    try:
        policy_store = PolicyStore(url, None, table=table)
        chunks, _ = load_corpus(POLICIES)
        policy_store.ingest(chunks, embed=False)
        payload = json.loads(
            (Path(__file__).resolve().parents[1] / "eval" / "rag_cases.json").read_text(
                encoding="utf-8"
            )
        )
        as_of = date.fromisoformat(payload["as_of"])
        empty = [
            case["id"]
            for case in payload["cases"]
            if not policy_store._text_route(case["query"], 30, as_of)
        ]
        assert empty == [], f"全文路零命中的用例：{empty}"
    finally:
        _drop(url, table)


def test_vector_route_uses_pgvector_cosine(store):
    policy_store, _ = store
    ranked = policy_store.search("实习生账号有效期", top_k=5, as_of=AS_OF)
    assert ranked.routes.get("route0", 0) > 0, "向量路应当返回候选"


def test_version_resolution_holds_in_sql(store):
    policy_store, _ = store
    ranked = policy_store.search("上海出差住宿上限", top_k=5, as_of=AS_OF)
    ids = [hit.chunk_id for hit in ranked.hits]
    assert "travel-expense@2026.1#005" in ids
    assert "travel-expense@2025.2#005" not in ids
    assert "travel-expense@2025.2#005" in [hit.chunk_id for hit in ranked.superseded]


def test_as_of_switches_effective_version(store):
    policy_store, _ = store
    ranked = policy_store.search("上海出差住宿上限", top_k=5, as_of=EARLY)
    assert "travel-expense@2025.2#005" in [hit.chunk_id for hit in ranked.hits]


def test_context_block_is_numbered_consistently(store):
    policy_store, _ = store
    ranked = policy_store.search("报销时限", top_k=3, as_of=AS_OF)
    context = ranked.as_context()
    for index in range(1, len(ranked.hits) + 1):
        assert f"[{index}]" in context
