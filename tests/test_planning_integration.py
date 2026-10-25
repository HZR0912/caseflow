"""真实 Postgres 上证明 12 步预演不会留下用户、邮箱、席位或幂等收据。"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from apps.mockcorp.models import MockSettings
from apps.mockcorp.preview import preview_plan
from apps.mockcorp.service import MockCorp
from apps.mockcorp.store import PostgresStore
from core.graph.onboarding import PlanningRequest, RuleInterpreter, make_plan

from tests.test_planning import TEXT, TODAY

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("seat_limit,ok", [(3, True), (2, False)])
async def test_plan_preview_rolls_back_business_and_only_logs_attempt(
    seat_limit, ok, supplier_connections
):
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required for PostgreSQL integration")
    settings = MockSettings(_env_file=None, code_seat_limit=seat_limit)
    store = PostgresStore(url, settings, pool=supplier_connections)
    tenant = "w5-test-" + uuid4().hex
    service = MockCorp(store, settings)
    try:
        repo = service.execute(
            tenant, "code.create_repo", {"name": "frontend"}, key="seed-repo"
        ).body["result"]
        project = service.execute(
            tenant, "ticket.create_project", {"key": "ONBOARD", "name": "入职"}, key="seed-project"
        ).body["result"]
        text = TEXT.replace("r_frontend", repo["id"]).replace("p_onboard", project["id"])
        facts = await RuleInterpreter().understand(PlanningRequest(text=text, today=TODAY))
        before = store.snapshot(tenant)
        reply = preview_plan(store, tenant, "actor", make_plan(facts, []))
        assert reply.status == 200 and reply.body["ok"] is ok
        assert len(reply.body["steps"]) == 12
        assert store.snapshot(tenant) == before
        with psycopg.connect(store.url) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM mockcorp_receipts WHERE tenant=%s", (tenant,)
                ).fetchone()[0]
                == 2
            )
            row = conn.execute(
                "SELECT dry_run, outcome, idempotency_key FROM mockcorp_oplog "
                "WHERE tenant=%s AND operation='plan.preview'",
                (tenant,),
            ).fetchone()
            assert row == (True, "dry_run", None)
    finally:
        with psycopg.connect(store.url) as conn:
            for table in ("mockcorp_receipts", "mockcorp_oplog", "mockcorp_state"):
                conn.execute(f"DELETE FROM {table} WHERE tenant=%s", (tenant,))
