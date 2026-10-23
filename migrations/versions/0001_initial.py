"""W1 第一批表：tenants / users / requests / audit_log

设计取舍（W1 就写清楚，避免后面语义漂移）：

1. 主键用 UUID 而不是自增整数：写操作要跨系统引用（MCP 工具、审计日志、
   幂等键 hash 都含资源 ID），自增 ID 会泄露规模且不便分布式生成。
2. `requests.thread_id` 是业务请求与 LangGraph checkpoint 的锚点：
   一个办事请求 = 一个 thread，审批中断后从 checkpoint 恢复靠它对上号。
3. `audit_log` 从第一张表就带 `prev_hash` / `entry_hash`：
   链式哈希事后补不了——旧记录没有 prev，补写就会断链。所以 W1 就把列建好，
   W7 写入器只需按 hash 链追加。
4. 状态用 CHECK 约束而不是 PG enum：enum 加值要 ALTER TYPE，迁移成本高；
   约束写在表上，改起来是一条 ALTER。

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

REQUEST_STATUSES = (
    "drafting",
    "awaiting_clarification",
    "awaiting_approval",
    "executing",
    "completed",
    "completed_with_compensation",
    "failed",
    "cancelled",
)


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(128), nullable=False, comment="AD / SSO 侧的用户 ID"),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("display_name", sa.String(200), nullable=True, comment="PII：入库前脱敏"),
        sa.Column("role", sa.String(32), nullable=False, server_default="employee"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "external_id", name="uq_users_tenant_external"),
    )
    op.create_index("ix_users_tenant_role", "users", ["tenant_id", "role"])

    op.create_table(
        "requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requester_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "intent", sa.String(64), nullable=True, comment="provision_employee / purchase ..."
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="drafting"),
        sa.Column(
            "thread_id",
            sa.String(128),
            nullable=False,
            unique=True,
            comment="LangGraph checkpoint 的 thread_id：业务请求与图状态的锚点",
        ),
        sa.Column("input_text", sa.Text, nullable=True, comment="用户原始输入（已脱敏）"),
        sa.Column(
            "plan",
            postgresql.JSONB,
            nullable=True,
            comment="规划产出的 DAG：每步含可逆性 / 风险级 / 依赖",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in REQUEST_STATUSES) + ")",
            name="ck_requests_status",
        ),
    )
    op.create_index("ix_requests_tenant_status", "requests", ["tenant_id", "status"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, autoincrement=True, primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor", sa.String(128), nullable=False, comment="user:<id> / agent / system"),
        sa.Column("action", sa.String(128), nullable=False, comment="如 policy.deny / tool.call"),
        sa.Column(
            "payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "prev_hash", sa.String(64), nullable=True, comment="上一条 entry_hash；首条为 NULL"
        ),
        sa.Column(
            "entry_hash",
            sa.String(64),
            nullable=False,
            comment="sha256(prev_hash || 规范化 payload)",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_audit_log_tenant_created", "audit_log", ["tenant_id", "created_at"])
    op.create_index("ix_audit_log_request", "audit_log", ["request_id"])

    # 只有一条链：tenant 内单调追加。唯一索引让"两条记录指向同一个 prev"这种
    # 分叉/篡改在数据库层就无法写入。
    op.create_index(
        "uq_audit_log_entry_hash",
        "audit_log",
        ["entry_hash"],
        unique=True,
    )
    op.create_index(
        "uq_audit_log_prev_hash",
        "audit_log",
        ["tenant_id", "prev_hash"],
        unique=True,
        postgresql_where=sa.text("prev_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("requests")
    op.drop_table("users")
    op.drop_table("tenants")
