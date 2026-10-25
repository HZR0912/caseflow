"""Authenticated approval API and same-origin Vue console."""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

from core.graph.approval import ApprovalDecision
from core.graph.approval_service import ApprovalError, ApprovalService
from core.graph.onboarding import PlanningRequest
from core.graph.providers import FallbackInterpreter
from core.graph.workflow import HttpPreviewClient, build_planning_graph
from core.memory.recall import RecallInterpreter
from core.memory.service import MemoryService
from core.tools.execution import ExecutionService
from core.tools.gateway import HttpToolGateway
from core.tools.payload import PreparedApproval
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import FileResponse

from apps.api.auth import SubjectDep
from apps.api.settings import get_settings

router = APIRouter()
WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"


def get_approval_service(request: Request):
    return request.app.state.approvals


ApprovalDep = Annotated[ApprovalService, Depends(get_approval_service)]


@router.get("/traces", include_in_schema=False)
@router.get("/approvals", include_in_schema=False)
async def console():
    if not (WEB_DIST / "index.html").is_file():
        raise ApprovalError(503, "approval_ui_not_built_run_npm_ci_and_build_in_apps_web")
    return FileResponse(WEB_DIST / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/v1/approval-requests/identity", tags=["approvals"])
async def identity(subject: SubjectDep, service: ApprovalDep):
    return {
        **subject.model_dump(),
        "can_review": await service.permitted(subject, "approval.decide"),
    }


@router.post("/v1/approval-requests", tags=["approvals"])
async def submit(
    body: PlanningRequest,
    subject: SubjectDep,
    service: ApprovalDep,
    request: Request,
    creation_key: Annotated[UUID, Header(alias="Idempotency-Key")],
):
    async def prepare():
        settings = get_settings()
        captured = []
        graph = build_planning_graph(
            body,
            subject,
            service.policy,
            HttpPreviewClient(settings.mockcorp_url, request.headers["authorization"]),
            interpreter=RecallInterpreter(
                FallbackInterpreter(settings.planning_providers), MemoryService(service), subject
            ),
            execution_sink=captured.append if service.cipher is not None else None,
        )
        snapshot = await graph.ainvoke({"trace": []})
        return PreparedApproval(snapshot, captured[0]) if captured else snapshot

    rid = str(uuid5(NAMESPACE_URL, json.dumps([subject.tenant, subject.id, str(creation_key)])))
    await service.require(subject, "approval.create")
    async with request.app.state.observability.operation(subject, "approval.submit", rid):
        return await service.create(body, subject, str(creation_key), prepare)


@router.get("/v1/approval-requests", tags=["approvals"])
async def listing(subject: SubjectDep, service: ApprovalDep):
    return {"items": await service.list(subject)}


@router.get("/v1/approval-requests/{request_id}", tags=["approvals"])
async def detail(request_id: UUID, subject: SubjectDep, service: ApprovalDep):
    return await service.get(str(request_id), subject)


@router.post("/v1/approval-requests/{request_id}/decision", tags=["approvals"])
async def decide(
    request_id: UUID,
    body: ApprovalDecision,
    subject: SubjectDep,
    service: ApprovalDep,
    request: Request,
):
    async with observe_existing(request, service, subject, request_id, "approval.decide"):
        return await service.decide(str(request_id), body, subject)


@router.post("/v1/approval-requests/{request_id}/recover", tags=["approvals"])
async def recover(request_id: UUID, subject: SubjectDep, service: ApprovalDep, request: Request):
    async with observe_existing(request, service, subject, request_id, "approval.recover"):
        return await service.recover(str(request_id), subject)


@router.post("/v1/approval-requests/{request_id}/execute", tags=["execution"])
async def execute(request_id: UUID, subject: SubjectDep, service: ApprovalDep, request: Request):
    gateway = HttpToolGateway(get_settings().mockcorp_url, request.headers["authorization"])
    async with observe_existing(request, service, subject, request_id, "execution.run") as frame:
        result = await ExecutionService(service, gateway).execute(str(request_id), subject)
        if frame is not None:
            frame.output = {"status": result["status"]}
            frame.status = "ok" if result["status"] == "succeeded" else "error"
        return result


@asynccontextmanager
async def observe_existing(request, service, subject, request_id, name):
    async with service.session() as (conn, _):
        row = await service.row(conn, str(request_id), subject)
        await service.readable(row, subject)
    owner = subject.model_copy(update={"id": row["requester"]})
    async with request.app.state.observability.operation(owner, name, str(request_id)) as frame:
        yield frame
