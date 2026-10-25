"""Durable scoped traces and sanitized replay frames, independent of business state."""

from alembic import op

revision = "0008_observability"
down_revision = "0007_memory"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE observation_traces (
            trace_id text PRIMARY KEY, tenant text NOT NULL, owner_id text NOT NULL,
            request_id text, root_span_id text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX observation_traces_scope
            ON observation_traces(tenant,owner_id,created_at DESC);
        CREATE TABLE observation_spans (
            trace_id text NOT NULL REFERENCES observation_traces(trace_id) ON DELETE CASCADE,
            span_id text NOT NULL, parent_span_id text, name text NOT NULL,
            started_at timestamptz NOT NULL, ended_at timestamptz,
            duration_ms double precision, status text NOT NULL DEFAULT 'running',
            input jsonb NOT NULL DEFAULT '{}', output jsonb NOT NULL DEFAULT '{}',
            attributes jsonb NOT NULL DEFAULT '{}',
            PRIMARY KEY(trace_id,span_id)
        );
        CREATE INDEX observation_spans_order ON observation_spans(trace_id,started_at,span_id);
    """)


def downgrade():
    op.execute("DROP TABLE observation_spans; DROP TABLE observation_traces")
