"""Immutable evidence and separately recorded human closure of execution exceptions."""

from alembic import op

revision = "0010_reconciliation"
down_revision = "0009_background_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE reconciliation_evidence (
            id text PRIMARY KEY,
            request_id text NOT NULL REFERENCES approval_requests(id) ON DELETE CASCADE,
            actor text NOT NULL, journal_hash text NOT NULL, evidence_hash text NOT NULL,
            observed_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
            payload jsonb NOT NULL
        );
        CREATE INDEX reconciliation_evidence_request
            ON reconciliation_evidence(request_id,observed_at DESC);
        CREATE TABLE reconciliation_resolutions (
            request_id text PRIMARY KEY REFERENCES approval_requests(id) ON DELETE CASCADE,
            evidence_id text NOT NULL REFERENCES reconciliation_evidence(id),
            decision text NOT NULL
                CHECK(decision IN ('confirm_compensated','accept_observed_state')),
            reason text NOT NULL, actor text NOT NULL, resolved_at timestamptz NOT NULL
        );
        CREATE TABLE reconciliation_events (
            id bigserial PRIMARY KEY,
            request_id text NOT NULL REFERENCES approval_requests(id) ON DELETE CASCADE,
            actor text NOT NULL, event text NOT NULL, created_at timestamptz NOT NULL,
            payload jsonb NOT NULL, previous_hash text, event_hash text NOT NULL
        );
        CREATE INDEX reconciliation_events_request ON reconciliation_events(request_id,id);
    """)


def downgrade():
    op.execute(
        "DROP TABLE reconciliation_events; DROP TABLE reconciliation_resolutions; "
        "DROP TABLE reconciliation_evidence"
    )
