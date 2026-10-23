"""MockCorp 操作日志：供给方视角的独立流水。

设计取舍见 ADR-0004：
- 不设外键：失败/预演路径的业务状态会回滚，但日志必须留下，否则"记录每一次尝试"无从谈起。
- 与业务同一事务：成功路径上日志和数据同生共死，不存在"改了数据没留痕"。
- 只记录进入事务的请求：认证失败、缺幂等键这类在事务前就被拒绝的请求不入表。
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_mockcorp_oplog"
down_revision = "0002_mockcorp"
branch_labels = None
depends_on = None

OUTCOMES = ("committed", "dry_run", "rejected", "error")


def upgrade() -> None:
    op.create_table(
        "mockcorp_oplog",
        sa.Column("id", sa.BigInteger, autoincrement=True, primary_key=True),
        sa.Column("tenant", sa.Text, nullable=False),
        sa.Column("operation", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=True, comment="发起凭据映射出的 user_id"),
        sa.Column("dry_run", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("replayed", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("result_id", sa.Text, nullable=True, comment="成功时创建/变更资源的 ID"),
        sa.Column("fingerprint", sa.Text, nullable=True, comment="与 mockcorp_receipts 同源"),
        sa.Column("idempotency_key", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "outcome IN (" + ", ".join(f"'{o}'" for o in OUTCOMES) + ")",
            name="ck_mockcorp_oplog_outcome",
        ),
    )
    # 按租户增量读取：id 单调递增，是天然的游标。
    op.create_index("ix_mockcorp_oplog_tenant_id", "mockcorp_oplog", ["tenant", "id"])


def downgrade() -> None:
    op.drop_table("mockcorp_oplog")
