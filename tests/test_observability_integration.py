import os
from uuid import uuid4

import pytest
from apps.api.settings import Settings
from core.graph.approval_service import ApprovalError
from core.obs.runtime import Runtime, identity, span
from core.obs.store import TraceStore
from core.policy.opa_client import PolicySubject

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_persisted_frames_scope_restart_and_incomplete():
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required")
    subject = PolicySubject(id="owner", role="employee", tenant=uuid4().hex)
    rid = str(uuid4())
    tid = identity(subject.tenant, rid)
    settings = Settings(_env_file=None, observation_enabled=True, database_url=url)
    store = TraceStore(url)
    runtime = Runtime(settings, store)
    try:
        async with runtime.operation(subject, "submit", rid), span("node"):
            live = await store.get(subject, tid)
            assert all(f["status"] == "running" and f["ended_at"] is None for f in live["frames"])
        await runtime.close()
        runtime = Runtime(settings, store)
        async with runtime.operation(subject, "execute", rid):
            pass
        record = await store.get(subject, tid)
        assert len(record["frames"]) == 3
        assert all(f["ended_at"] and f["status"] == "ok" for f in record["frames"])
        assert len(await store.list(subject, request_id=rid)) == 1
        colleague = subject.model_copy(update={"id": "other"})
        assert await store.list(colleague) == []
        with pytest.raises(ApprovalError) as e:
            await store.get(colleague, tid)
        assert e.value.status == 404
        assert (await store.get(colleague, tid, all_in_tenant=True))["owner_id"] == "owner"
        outsider = subject.model_copy(update={"tenant": "other"})
        assert await store.list(outsider, all_in_tenant=True) == []
        with pytest.raises(ApprovalError):
            await store.get(outsider, tid, all_in_tenant=True)
    finally:
        await runtime.close()
        await store.write("DELETE FROM observation_traces WHERE trace_id=%s", (tid,))
