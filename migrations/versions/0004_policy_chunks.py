"""W3 知识库表：制度块 + 向量索引 + 全文索引。

三个设计判断：

1. **`tokens` 存成空格分隔串，`tsv` 是由它生成的列。**
   Postgres 自带的 `to_tsvector('simple', 中文)` 不做中文分词——它会把整段中文
   当成一个巨大的词元，检索几乎必然失配。所以中文切分放在应用层（`core/rag/bm25.py`），
   库里只负责倒排与打分。`simple` 配置不词干化、去停用词，正好适配已经切好的 token。
2. **`embedding` 用 pgvector 的 `vector(1024)`**，与 DashScope text-embedding-v3 维度一致。
   允许为 NULL：没有配置 API key 时只走全文检索，是**有意的降级**而不是故障。
3. **`effective_date` / `superseded_date` 存在块上**，检索层据此判断"问到 2025 年的
   报销时限时，旧版本还适用吗"。

索引：HNSW（cosine）用于向量召回，GIN 用于 `tsv` 全文召回。40 个块的规模用不上 HNSW，
但它不影响正确性，且把"规模变大时要做什么"这件事提前写进了结构里。
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0004_policy_chunks"
down_revision = "0003_mockcorp_oplog"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "policy_chunks",
        sa.Column("chunk_id", sa.String(200), primary_key=True),
        sa.Column("doc_key", sa.String(100), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column(
            "section_path",
            sa.String(500),
            nullable=False,
            comment="文档 > 章 > 条的路径，供引用展示",
        ),
        sa.Column(
            "section_key",
            sa.String(300),
            nullable=False,
            comment="去掉「第 N 条」后的条款标题，跨版本对齐用（条款号会漂移）",
        ),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column(
            "char_start",
            sa.Integer,
            nullable=False,
            comment="相对源文件正文的起始偏移，用于回原文逐字校验",
        ),
        sa.Column("char_end", sa.Integer, nullable=False),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("superseded_date", sa.Date, nullable=True),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "tokens",
            sa.Text,
            nullable=False,
            server_default="",
            comment="应用层切好的 token，空格分隔",
        ),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR,
            sa.Computed("to_tsvector('simple', tokens)", persisted=True),
        ),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_policy_chunks_doc_key", "policy_chunks", ["doc_key", "version"])
    op.create_index("ix_policy_chunks_tsv", "policy_chunks", ["tsv"], postgresql_using="gin")
    op.create_index(
        "ix_policy_chunks_embedding",
        "policy_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )


def downgrade() -> None:
    op.drop_table("policy_chunks")
