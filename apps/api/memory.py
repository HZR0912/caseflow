"""Authenticated memory and organization controls, separate from business execution."""

from typing import Annotated
from uuid import UUID

from core.memory.models import OrganizationWrite, PreferenceKey, PreferenceWrite, SessionTurn
from core.memory.organization import OrganizationService
from core.memory.service import MemoryService
from core.memory.sessions import SessionService
from fastapi import APIRouter, Depends, Query, Request

from apps.api.auth import SubjectDep

router = APIRouter(prefix="/v1/memory", tags=["memory"])


def get_memory(request: Request):
    return MemoryService(request.app.state.approvals)


MemoryDep = Annotated[MemoryService, Depends(get_memory)]


@router.get("/preferences")
async def preferences(subject: SubjectDep, service: MemoryDep):
    return {"items": await service.preferences(subject, include_expired=True)}


@router.put("/preferences")
async def remember(body: PreferenceWrite, subject: SubjectDep, service: MemoryDep):
    return await service.put_preference(subject, body)


@router.delete("/preferences/{key}")
async def forget(
    key: PreferenceKey,
    subject: SubjectDep,
    service: MemoryDep,
    expected_version: Annotated[int, Query(ge=1)],
):
    return await service.forget_preference(subject, key, expected_version)


@router.get("/search")
async def search(
    subject: SubjectDep, service: MemoryDep, q: Annotated[str, Query(min_length=1, max_length=300)]
):
    return {"items": await service.preferences(subject, q), "embedding_model": "hashing-v1-128"}


@router.get("/history")
async def history(
    subject: SubjectDep,
    service: MemoryDep,
    department_id: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9_-]{1,100}$")] = None,
):
    return {"items": await service.history(subject, department_id)}


@router.post("/sessions")
async def create_session(subject: SubjectDep, service: MemoryDep):
    return await SessionService(service).create(subject)


@router.get("/sessions/{session_id}")
async def session(session_id: UUID, subject: SubjectDep, service: MemoryDep):
    return await SessionService(service).get(subject, str(session_id))


@router.post("/sessions/{session_id}/turns")
async def turn(session_id: UUID, body: SessionTurn, subject: SubjectDep, service: MemoryDep):
    return await SessionService(service).turn(subject, str(session_id), body)


@router.delete("/sessions/{session_id}")
async def forget_session(session_id: UUID, subject: SubjectDep, service: MemoryDep):
    return await SessionService(service).forget(subject, str(session_id))


@router.get("/organization")
async def organization(subject: SubjectDep, service: MemoryDep):
    return await OrganizationService(service).get(subject)


@router.put("/organization")
async def save_organization(body: OrganizationWrite, subject: SubjectDep, service: MemoryDep):
    return await OrganizationService(service).put(subject, body)


@router.get("/organization/context")
async def context(
    subject: SubjectDep,
    service: MemoryDep,
    department_id: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9_-]{1,100}$")] = None,
):
    return await OrganizationService(service).context(subject, department_id)


@router.post("/purge-expired")
async def purge(subject: SubjectDep, service: MemoryDep):
    await service.require(subject, "memory.write")
    async with service.approvals.session() as (conn, _):
        preferences = await conn.execute(
            "DELETE FROM memory_preferences WHERE tenant=%s AND owner_id=%s AND expires_at<=%s",
            (subject.tenant, subject.id, service.clock()),
        )
        ids = await (
            await conn.execute(
                "SELECT id FROM memory_sessions WHERE tenant=%s AND owner_id=%s AND expires_at<=%s",
                (subject.tenant, subject.id, service.clock()),
            )
        ).fetchall()
    for row in ids:
        await SessionService(service).forget(subject, str(row["id"]))
    return {"preferences": preferences.rowcount, "sessions": len(ids)}
