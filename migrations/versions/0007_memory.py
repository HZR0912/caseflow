"""Three memory types: scoped preferences, session ownership and relational organization."""

from alembic import op

revision = "0007_memory"
down_revision = "0006_execution"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE memory_preference_versions (
            tenant text NOT NULL, owner_id text NOT NULL, key text NOT NULL,
            version bigint NOT NULL, PRIMARY KEY (tenant, owner_id, key)
        );
        CREATE TABLE memory_preferences (
            tenant text NOT NULL, owner_id text NOT NULL, key text NOT NULL,
            value text NOT NULL, version bigint NOT NULL CHECK (version > 0),
            importance double precision NOT NULL CHECK (importance BETWEEN 0 AND 1),
            embedding vector(128) NOT NULL, embedding_model text NOT NULL,
            confirmed_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
            PRIMARY KEY (tenant, owner_id, key)
        );
        CREATE TABLE memory_sessions (
            id uuid PRIMARY KEY, tenant text NOT NULL, owner_id text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL
        );
        CREATE INDEX memory_sessions_owner ON memory_sessions(tenant, owner_id);
        CREATE TABLE org_versions (
            tenant text PRIMARY KEY, version integer NOT NULL CHECK (version >= 0)
        );
        CREATE TABLE org_departments (
            tenant text NOT NULL, id text NOT NULL, name text NOT NULL,
            parent_id text, active boolean NOT NULL,
            PRIMARY KEY (tenant, id),
            FOREIGN KEY (tenant, parent_id) REFERENCES org_departments(tenant, id)
                DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TABLE org_members (
            tenant text NOT NULL, id text NOT NULL, department_id text NOT NULL,
            manager_id text, mentor_id text, is_lead boolean NOT NULL, active boolean NOT NULL,
            PRIMARY KEY (tenant, id),
            FOREIGN KEY (tenant, department_id) REFERENCES org_departments(tenant, id),
            FOREIGN KEY (tenant, manager_id) REFERENCES org_members(tenant, id)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY (tenant, mentor_id) REFERENCES org_members(tenant, id)
                DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TABLE org_systems (
            tenant text NOT NULL, department_id text NOT NULL, kind text NOT NULL,
            resource_id text NOT NULL, owner_id text NOT NULL,
            PRIMARY KEY (tenant, department_id, kind),
            FOREIGN KEY (tenant, department_id) REFERENCES org_departments(tenant, id),
            FOREIGN KEY (tenant, owner_id) REFERENCES org_members(tenant, id)
        );
    """)


def downgrade():
    for table in (
        "org_systems",
        "org_members",
        "org_departments",
        "org_versions",
        "memory_sessions",
        "memory_preferences",
        "memory_preference_versions",
    ):
        op.execute(f"DROP TABLE {table}")
