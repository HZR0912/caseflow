"""MockCorp 的租户状态和原子幂等收据。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_mockcorp"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mockcorp_state",
        sa.Column("tenant", sa.Text, primary_key=True),
        sa.Column("state", postgresql.JSONB, nullable=False),
    )
    op.create_table(
        "mockcorp_receipts",
        sa.Column(
            "tenant",
            sa.Text,
            sa.ForeignKey("mockcorp_state.tenant", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.String(200), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("response", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("mockcorp_receipts")
    op.drop_table("mockcorp_state")
