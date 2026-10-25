"""Encrypted plans and durable per-step execution journal."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_execution"
down_revision = "0005_approval_requests"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("approval_requests", sa.Column("execution_payload", sa.Text))
    op.create_table(
        "execution_runs",
        sa.Column(
            "request_id",
            sa.String(36),
            sa.ForeignKey("approval_requests.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("journal", JSONB, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade():
    op.drop_table("execution_runs")
    op.drop_column("approval_requests", "execution_payload")
