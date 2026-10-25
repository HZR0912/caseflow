"""Durable execution consent and resumable background scheduling."""

from alembic import op

revision = "0009_background_jobs"
down_revision = "0008_observability"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE background_jobs (
            request_id text PRIMARY KEY REFERENCES approval_requests(id) ON DELETE CASCADE,
            tenant text NOT NULL, actor text NOT NULL, role text NOT NULL,
            plan_hash text NOT NULL, credential text,
            authorized_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
            status text NOT NULL CHECK(status IN
                ('queued','running','waiting','completed','blocked')),
            due_at timestamptz NOT NULL DEFAULT now(),
            failures integer NOT NULL DEFAULT 0, runs integer NOT NULL DEFAULT 0,
            last_error text, updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX background_jobs_due ON background_jobs(due_at)
            WHERE status IN ('queued','running','waiting');
    """)


def downgrade():
    op.execute("DROP TABLE background_jobs")
