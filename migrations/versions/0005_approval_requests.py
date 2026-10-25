"""W6 durable approval snapshots and single-winner decisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_approval_requests"
down_revision = "0004_policy_chunks"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant", sa.Text, nullable=False),
        sa.Column("requester", sa.Text, nullable=False),
        sa.Column("creation_key", sa.String(36), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("decision", JSONB),
        sa.Column("status", sa.Text, nullable=False, server_default="submitted"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant", "requester", "creation_key", name="uq_approval_creation"),
        sa.CheckConstraint(
            "status IN ('submitted','awaiting_approval','approved_ready','rejected')",
            name="ck_approval_status",
        ),
    )
    op.create_index("ix_approval_tenant_created", "approval_requests", ["tenant", "created_at"])


def downgrade():
    op.drop_table("approval_requests")
