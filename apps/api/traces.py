"""Read-only, tenant-scoped trace replay. No execution routes live here."""

from typing import Annotated
from uuid import UUID

from core.obs.metrics import metrics
from fastapi import APIRouter, Path, Request, Response

from apps.api.approvals import ApprovalDep
from apps.api.auth import SubjectDep

router = APIRouter(prefix="/v1/traces", tags=["observability"])


async def scope(subject, service):
    await service.require(subject, "trace.read")
    return await service.permitted(subject, "trace.read_all")


@router.get("")
async def listing(
    request: Request,
    response: Response,
    subject: SubjectDep,
    service: ApprovalDep,
    request_id: UUID | None = None,
):
    all_in_tenant = await scope(subject, service)
    response.headers["Cache-Control"] = "no-store"
    runtime = request.app.state.observability
    return {
        "enabled": runtime.enabled,
        "items": await runtime.store.list(
            subject, all_in_tenant, str(request_id) if request_id else None
        ),
    }


@router.get("/{trace_id}")
async def detail(
    trace_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    request: Request,
    response: Response,
    subject: SubjectDep,
    service: ApprovalDep,
):
    all_in_tenant = await scope(subject, service)
    response.headers["Cache-Control"] = "no-store"
    result = await request.app.state.observability.store.get(subject, trace_id, all_in_tenant)
    return {**result, "metrics": metrics(result)}
