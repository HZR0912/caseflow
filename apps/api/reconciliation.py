"""Scoped evidence inspection and explicit human resolutions."""

from uuid import UUID

from core.tools.reconciliation import HttpEvidenceGateway, ReconciliationService, ResolutionDecision
from fastapi import APIRouter, Request

from apps.api.approvals import ApprovalDep, observe_existing
from apps.api.auth import SubjectDep
from apps.api.settings import get_settings

router = APIRouter()


def service(approvals, request):
    return ReconciliationService(
        approvals,
        HttpEvidenceGateway(get_settings().mockcorp_url, request.headers["authorization"]),
    )


@router.get("/v1/approval-requests/{request_id}/reconciliation", tags=["reconciliation"])
async def detail(request_id: UUID, subject: SubjectDep, approvals: ApprovalDep):
    return await ReconciliationService(approvals).get(str(request_id), subject)


@router.post("/v1/approval-requests/{request_id}/reconcile", tags=["reconciliation"])
async def inspect(request_id: UUID, subject: SubjectDep, approvals: ApprovalDep, request: Request):
    async with observe_existing(request, approvals, subject, request_id, "reconciliation.inspect"):
        return await service(approvals, request).inspect(str(request_id), subject)


@router.post("/v1/approval-requests/{request_id}/resolve", tags=["reconciliation"])
async def resolve(
    request_id: UUID,
    body: ResolutionDecision,
    subject: SubjectDep,
    approvals: ApprovalDep,
    request: Request,
):
    async with observe_existing(request, approvals, subject, request_id, "reconciliation.resolve"):
        return await service(approvals, request).resolve(str(request_id), body, subject)
