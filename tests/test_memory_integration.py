"""Actual PostgreSQL/pgvector and LangGraph checkpoints: recall, persistence and isolation."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from core.graph.approval_service import ApprovalError, ApprovalService
from core.graph.onboarding import PlanningRequest, RuleInterpreter
from core.graph.workflow import build_planning_graph
from core.memory.models import OrganizationWrite, PreferenceWrite, SessionTurn
from core.memory.organization import OrganizationService
from core.memory.recall import RecallInterpreter
from core.memory.service import MemoryService
from core.memory.sessions import SessionService
from core.policy.opa_client import PolicySubject
from core.tools.payload import PlanCipher, PreparedApproval
from cryptography.fernet import Fernet

from tests.test_approval_integration import ApprovalPolicy, choice
from tests.test_planning import LocalPreview, prepared_store

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def memory_env(approval_connections):
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required")
    service = ApprovalService(
        url,
        ApprovalPolicy(),
        cipher=PlanCipher(Fernet.generate_key().decode()),
        pool=approval_connections,
    )
    subject = PolicySubject(id="operator", role="admin", tenant="memory-" + uuid4().hex)
    memory = MemoryService(service)
    try:
        yield memory, subject
    finally:
        async with service.session() as (conn, _):
            rows = await (
                await conn.execute(
                    "SELECT id FROM memory_sessions WHERE tenant=%s",
                    (subject.tenant,),
                )
            ).fetchall()
        for row in rows:
            await SessionService(memory).forget(subject, str(row["id"]))
        with psycopg.connect(service.url) as conn:
            for table in (
                "org_systems",
                "org_members",
                "org_departments",
                "org_versions",
                "memory_preferences",
                "memory_preference_versions",
            ):
                conn.execute(f"DELETE FROM {table} WHERE tenant=%s", (subject.tenant,))
        from scripts.w6_approval_demo import cleanup

        cleanup(service.url, subject.tenant)


def organization(version=0, repo="r_frontend"):
    return OrganizationWrite(
        expected_version=version,
        departments=[
            {"id": "eng", "name": "Engineering"},
            {"id": "frontend", "name": "Frontend", "parent_id": "eng"},
        ],
        members=[
            {"id": "director", "department_id": "eng", "is_lead": True},
            {"id": "lead", "department_id": "frontend", "is_lead": True, "manager_id": "director"},
            {
                "id": "operator",
                "department_id": "frontend",
                "manager_id": "lead",
                "mentor_id": "director",
            },
        ],
        systems=[
            {
                "department_id": "eng",
                "kind": "email_domain",
                "resource_id": "example.com",
                "owner_id": "director",
            },
            {
                "department_id": "frontend",
                "kind": "repo_id",
                "resource_id": repo,
                "owner_id": "lead",
            },
            {
                "department_id": "frontend",
                "kind": "project_id",
                "resource_id": "p_onboard",
                "owner_id": "lead",
            },
        ],
    )


def turn(version, facts=None, **kwargs):
    return SessionTurn(
        turn_id=uuid4(),
        expected_version=version,
        request={"text": "请准备入职环境", "today": "2026-10-25", "facts": facts or {}},
        **kwargs,
    )


async def test_preference_confirmation_dedup_correction_tenant_and_owner(memory_env):
    memory, subject = memory_env
    assert (await memory.put_preference(subject, PreferenceWrite(key="permission", value="read")))[
        "status"
    ] == "not_saved"
    assert await memory.preferences(subject) == []

    body = PreferenceWrite(key="permission", value="read", confirmed=True)
    first = await memory.put_preference(subject, body)
    assert first["memory"]["version"] == 1
    assert (await memory.put_preference(subject, body))["status"] == "unchanged"
    with pytest.raises(ApprovalError, match="memory_version_conflict"):
        await memory.put_preference(subject, body.model_copy(update={"value": "write"}))
    updated = await memory.put_preference(
        subject, body.model_copy(update={"value": "write", "expected_version": 1})
    )
    assert updated["memory"]["version"] == 2
    for other in (
        subject.model_copy(update={"id": "other"}),
        subject.model_copy(update={"tenant": "other"}),
    ):
        assert await memory.preferences(other) == []
        with pytest.raises(ApprovalError):
            await memory.forget_preference(other, "permission", 2)
    await memory.forget_preference(subject, "permission", 2)
    assert await memory.preferences(subject) == []


async def test_deleted_preference_version_cannot_delete_recreated_value(memory_env):
    memory, subject = memory_env
    body = PreferenceWrite(key="permission", value="read", confirmed=True)
    first = await memory.put_preference(subject, body)
    await memory.forget_preference(subject, "permission", first["memory"]["version"])
    second = await memory.put_preference(subject, body)
    assert second["memory"]["version"] > first["memory"]["version"]
    with pytest.raises(ApprovalError, match="version_conflict"):
        await memory.forget_preference(subject, "permission", first["memory"]["version"])


async def test_concurrent_session_and_preference_updates_have_one_winner(memory_env):
    memory, subject = memory_env
    service = SessionService(memory)
    sid = (await service.create(subject))["id"]
    results = await asyncio.gather(
        service.turn(subject, sid, turn(0, {"permission": "read"})),
        service.turn(subject, sid, turn(0, {"permission": "write"})),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, ApprovalError) and r.status == 409 for r in results) == 1
    results = await asyncio.gather(
        memory.put_preference(
            subject, PreferenceWrite(key="permission", value="read", confirmed=True)
        ),
        memory.put_preference(
            subject, PreferenceWrite(key="permission", value="write", confirmed=True)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, ApprovalError) and r.status == 409 for r in results) == 1


async def test_session_checkpoint_before_confirm_resumes_and_cipher_binds_owner(memory_env):
    from core.memory.sessions import SessionState
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph

    memory, subject = memory_env
    service = SessionService(memory)
    sid = (await service.create(subject))["id"]
    encrypted = service.seal(subject, sid, {"permission": "read"})
    for other, other_sid in ((subject.model_copy(update={"id": "other"}), sid), (subject, "other")):
        with pytest.raises(ApprovalError, match="payload_invalid"):
            service.open(other, other_sid, encrypted)
    async with memory.approvals.session() as (conn, _):
        builder = StateGraph(SessionState)
        builder.add_node("confirm", lambda state: state)
        builder.add_edge(START, "confirm")
        builder.add_edge("confirm", END)
        graph = builder.compile(checkpointer=AsyncPostgresSaver(conn), interrupt_before=["confirm"])
        config = {"configurable": {"thread_id": service.thread_id(subject, sid)}}
        await graph.ainvoke({"version": 1, "encrypted_facts": encrypted, "turns": []}, config)
        assert (await graph.aget_state(config)).next == ("confirm",)
    assert (await SessionService(memory).get(subject, sid))["facts"]["permission"] == "read"
    old_cipher = memory.approvals.cipher
    memory.approvals.cipher = PlanCipher(Fernet.generate_key().decode())
    try:
        with pytest.raises(ApprovalError, match="payload_invalid"):
            await service.get(subject, sid)
    finally:
        memory.approvals.cipher = old_cipher


async def test_vector_retrieval_next_week_decay_expiry_no_read_refresh(memory_env):
    memory, subject = memory_env
    now = datetime(2026, 10, 25, tzinfo=UTC)
    memory.clock = lambda: now
    await memory.put_preference(
        subject,
        PreferenceWrite(key="email_domain", value="example.com", confirmed=True, ttl_days=10),
    )
    await memory.put_preference(
        subject, PreferenceWrite(key="repo_id", value="r_frontend", confirmed=True, ttl_days=180)
    )
    fresh = MemoryService(memory.approvals, clock=lambda: now + timedelta(days=7))
    rows = await fresh.preferences(subject, "邮箱域 邮件")
    assert rows[0]["key"] == "email_domain"
    assert rows[0]["score"] < 0.9 and datetime.fromisoformat(rows[0]["confirmed_at"]) == now
    fresh.clock = lambda: now + timedelta(days=11)
    assert [r["key"] for r in await fresh.preferences(subject)] == ["repo_id"]
    assert any(r["expired"] for r in await fresh.preferences(subject, include_expired=True))


async def test_session_restart_correction_replay_and_encrypted_checkpoint(memory_env):
    memory, subject = memory_env
    service = SessionService(memory)
    row = await service.create(subject)
    sid = row["id"]
    first = turn(0, {"employees": [{"name": "张三", "username": "zhangsan"}], "permission": "read"})
    saved = await service.turn(subject, sid, first)
    assert saved["version"] == 1
    assert "张三" not in json.dumps(saved, ensure_ascii=False)
    assert await service.turn(subject, sid, first) == saved
    fresh = SessionService(
        MemoryService(
            ApprovalService(
                memory.approvals.url, memory.approvals.policy, cipher=memory.approvals.cipher
            )
        )
    )
    assert await fresh.get(subject, sid) == saved
    with pytest.raises(ApprovalError, match="version_conflict"):
        await fresh.turn(subject, sid, turn(0, {"permission": "write"}))
    updated = await fresh.turn(subject, sid, turn(1, {"permission": None}))
    assert updated["version"] == 2 and updated["facts"]["permission"] is None
    raw = await fresh.facts(subject, sid, 2)
    assert raw["employees"][0]["username"] == "zhangsan"
    with psycopg.connect(memory.approvals.url) as conn:
        blobs = conn.execute(
            "SELECT blob FROM checkpoint_blobs WHERE thread_id=%s",
            (service.thread_id(subject, sid),),
        ).fetchall()
        assert all(
            b"zhangsan" not in bytes(b[0] or b"") and "张三".encode() not in bytes(b[0] or b"")
            for b in blobs
        )
    with pytest.raises(ApprovalError, match="version_conflict"):
        await fresh.facts(subject, sid, 1)
    await fresh.forget(subject, sid)
    with psycopg.connect(memory.approvals.url) as conn:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            assert (
                conn.execute(
                    f"SELECT count(*) FROM {table} WHERE thread_id=%s",
                    (service.thread_id(subject, sid),),
                ).fetchone()[0]
                == 0
            )


async def test_session_owner_tenant_expiry_and_turn_id_binding(memory_env):
    memory, subject = memory_env
    service = SessionService(memory)
    sid = (await service.create(subject))["id"]
    body = turn(0, {"role": "frontend"})
    await service.turn(subject, sid, body)
    with pytest.raises(ApprovalError, match="payload_mismatch"):
        await service.turn(subject, sid, body.model_copy(update={"clear_fields": ["role"]}))
    for other in (
        subject.model_copy(update={"id": "other"}),
        subject.model_copy(update={"tenant": "other"}),
    ):
        with pytest.raises(ApprovalError, match="not_found"):
            await service.get(other, sid)
        with pytest.raises(ApprovalError, match="not_found"):
            await service.forget(other, sid)
    later = SessionService(
        MemoryService(memory.approvals, clock=lambda: datetime.now(UTC) + timedelta(days=31))
    )
    with pytest.raises(ApprovalError, match="expired"):
        await later.get(subject, sid)
    await later.forget(subject, sid)


async def test_organization_recursive_inheritance_update_and_isolation(memory_env):
    memory, subject = memory_env
    service = OrganizationService(memory)
    await service.put(subject, organization())
    context = await service.context(subject)
    assert [d["id"] for d in context["ancestors"]] == ["frontend", "eng"]
    assert context["defaults"] == {
        "email_domain": "example.com",
        "repo_id": "r_frontend",
        "project_id": "p_onboard",
    }
    assert context["manager_chain"] == ["lead", "director"]
    assert context["mentor_id"] == "director"
    assert (await service.get(subject.model_copy(update={"tenant": "other"})))["departments"] == []
    with pytest.raises(ApprovalError, match="version_conflict"):
        await service.put(subject, organization())
    await service.put(subject, organization(1, "r_updated"))
    assert (await service.context(subject))["defaults"]["repo_id"] == "r_updated"
    with pytest.raises(ApprovalError, match="missing_or_inactive"):
        await service.context(subject, "missing")
    inactive = organization(2).model_dump()
    inactive["systems"] = []
    for dept in inactive["departments"]:
        dept["active"] = False
    for member in inactive["members"]:
        member["active"] = False
    await service.put(subject, OrganizationWrite.model_validate(inactive))
    with pytest.raises(ApprovalError, match="missing_or_inactive"):
        await service.context(subject, "frontend")


async def test_memory_recall_reaches_plan_and_freezes_in_approval(memory_env):
    memory, subject = memory_env
    await OrganizationService(memory).put(subject, organization())
    await memory.put_preference(
        subject, PreferenceWrite(key="permission", value="read", confirmed=True)
    )
    sessions = SessionService(memory)
    sid = (await sessions.create(subject))["id"]
    await sessions.turn(
        subject,
        sid,
        turn(
            0,
            {
                "employees": [{"name": "张三", "username": "zhangsan"}],
                "role": "frontend",
                "start_date": "2026-10-26",
                "end_date": "2026-12-31",
            },
        ),
    )
    request = PlanningRequest(
        text="请准备入职环境",
        today="2026-10-25",
        memory={
            "session_id": sid,
            "session_version": 1,
            "use_preferences": True,
            "use_organization": True,
        },
    )
    captured = []
    store = prepared_store()

    async def prepare():
        graph = build_planning_graph(
            request,
            subject,
            memory.approvals.policy,
            LocalPreview(store),
            interpreter=RecallInterpreter(RuleInterpreter(), memory, subject),
            execution_sink=captured.append,
        )
        result = await graph.ainvoke({"trace": []})
        assert result["status"] == "awaiting_approval", result
        assert result["memory_context"]["organization"]["version"] == 1
        assert "张三" not in json.dumps(result, ensure_ascii=False)
        return PreparedApproval(result, captured[0])

    row = await memory.approvals.create(request, subject, str(uuid4()), prepare)
    await memory.approvals.decide(
        row["id"], choice(row), subject.model_copy(update={"id": "reviewer"})
    )
    await memory.put_preference(
        subject,
        PreferenceWrite(key="permission", value="admin", confirmed=True, expected_version=1),
    )
    await OrganizationService(memory).put(subject, organization(1, "r_new"))
    view = await memory.approvals.get(row["id"], subject)
    assert view["plan_hash"] == row["plan_hash"]
    assert view["snapshot"]["memory_context"]["organization"]["version"] == 1
    invite = next(
        s for s in view["snapshot"]["plan"]["steps"] if s["operation"] == "code.invite_member"
    )
    assert invite["arguments"]["permission"] == "read"
    assert invite["arguments"]["repo_id"] == "r_frontend"
    assert (await memory.history(subject))[0]["decision"] == "approve"
    assert await memory.history(subject.model_copy(update={"id": "other"})) == []
    assert (await memory.history(subject, "frontend"))[0]["request_id"] == row["id"]
    recalled = RecallInterpreter(RuleInterpreter(), memory, subject)
    await recalled.understand(request)
    assert recalled.context["organization_history"][0]["request_id"] == row["id"]


async def test_reading_memory_never_bypasses_policy(memory_env):
    memory, subject = memory_env
    await memory.put_preference(
        subject, PreferenceWrite(key="permission", value="admin", confirmed=True)
    )
    memory.approvals.policy.denied = True
    try:
        with pytest.raises(ApprovalError, match="policy_denied"):
            await memory.preferences(subject)
        with pytest.raises(ApprovalError, match="policy_denied"):
            await OrganizationService(memory).get(subject)
    finally:
        memory.approvals.policy.denied = False
