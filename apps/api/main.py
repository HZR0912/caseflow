"""CaseFlow API。

W1：`/healthz` + 一条**受策略门保护**的写动作入口 `/v1/actions/execute`。
W4：`/v1/guardrails/check` —— 输入护栏流水线（注入检测 → PII → 意图 → 槽位）。
    护栏的结论只用来说明"这句话能不能进规划"，**不在这里做授权**；
    越权与数据外泄只作为 risk 标签交给下游策略层。

刻意不做的事：不在这一层做权限判断。API 层只做鉴权（你是谁），
授权（你能不能做）永远交给 OPA，避免两处逻辑各自演化。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from core.db.pools import approval_pool
from core.graph.approval_service import ApprovalError, ApprovalService
from core.graph.onboarding import PlanningRequest
from core.graph.providers import FallbackInterpreter
from core.graph.workflow import HttpPreviewClient, build_planning_graph
from core.guardrails.pipeline import Guardrails
from core.memory.recall import RecallInterpreter
from core.obs.runtime import Runtime
from core.policy.opa_client import PolicyClient, PolicyQuery, PolicyResource, PolicySubject
from core.tools.payload import PlanCipher
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from apps.api.approvals import WEB_DIST
from apps.api.approvals import router as approval_router
from apps.api.auth import SubjectDep
from apps.api.memory import MemoryDep
from apps.api.memory import router as memory_router
from apps.api.settings import get_settings
from apps.api.traces import router as trace_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.observability = Runtime(settings)
    app.state.policy = PolicyClient(base_url=settings.opa_url, timeout=settings.opa_timeout_s)
    pool = approval_pool(settings.database_url)
    app.state.approvals = ApprovalService(
        settings.database_url,
        app.state.policy,
        settings.approval_ttl_hours,
        cipher=PlanCipher(settings.execution_encryption_key.get_secret_value())
        if settings.execution_encryption_key.get_secret_value()
        else None,
        pool=pool,
    )
    try:
        await pool.open()
        yield
    finally:
        await pool.close()
        await app.state.policy.aclose()
        await app.state.observability.close()


app = FastAPI(
    title="CaseFlow API",
    version="0.1.0",
    description="企业办事与审批 Agent —— 写操作平面",
    lifespan=lifespan,
)
app.include_router(approval_router)
app.include_router(memory_router)
app.include_router(trace_router)
app.mount(
    "/approval-assets",
    StaticFiles(directory=WEB_DIST, check_dir=False),
    name="approval-assets",
)


@app.exception_handler(ApprovalError)
async def approval_error(request: Request, exc: ApprovalError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


def get_policy_client(request: Request) -> PolicyClient:
    return request.app.state.policy


PolicyDep = Annotated[PolicyClient, Depends(get_policy_client)]


def get_preview_client(request: Request) -> HttpPreviewClient:
    # 转发已经验证过的调用者 Bearer；不接受请求体里的 tenant 或另一份工具凭据。
    return HttpPreviewClient(get_settings().mockcorp_url, request.headers["authorization"])


PreviewDep = Annotated[HttpPreviewClient, Depends(get_preview_client)]

# 护栏是无状态纯函数集合，进程级单例即可（不依赖 lifespan，测试不必构造上下文）。
guardrails = Guardrails()


class ActionRequest(BaseModel):
    """W1 策略探针契约；W7 正式执行使用独立的已批准计划入口。"""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(description="命名空间动作，如 code.invite_member")
    resource_type: str = Field(description="资源类型，如 repo / account / order")
    resource_id: str | None = None

    def to_query(self, subject: PolicySubject) -> PolicyQuery:
        return PolicyQuery(
            subject=subject,
            action=self.action,
            resource=PolicyResource(type=self.resource_type, id=self.resource_id),
        )


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    settings = get_settings()
    return {"status": "ok", "service": "caseflow-api", "env": settings.env}


@app.post("/v1/policy/check", tags=["policy"])
async def policy_check(req: ActionRequest, policy: PolicyDep, subject: SubjectDep) -> dict:
    """只问策略，不执行 —— 调试和前端预览用。"""
    decision = await policy.authorize(req.to_query(subject))
    return decision.model_dump()


class GuardrailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000, description="用户本轮的原始输入")
    today: date = Field(description="参照日期，由调用方给出（可复现的回放/评测需要它）")
    slots: dict[str, object] | None = Field(
        default=None, description="上一轮已确认的槽位，用于追问后合并"
    )


@app.post("/v1/guardrails/check", tags=["guardrails"])
async def guardrails_check(req: GuardrailRequest, subject: SubjectDep) -> dict:
    """输入护栏：返回处置结论与逐层证据。

    注意返回里 `masked_text` 是脱敏后的文本；PII 真实值只在进程内存的 vault 里，
    不会出现在响应中。
    """
    result = guardrails.check(req.text, today=req.today, existing_slots=req.slots)
    payload = result.model_dump()
    payload["subject"] = {"id": subject.id, "role": subject.role}
    return payload


@app.post("/v1/actions/execute", tags=["actions"])
async def execute_action(req: ActionRequest, policy: PolicyDep, subject: SubjectDep) -> dict:
    """受策略门保护的写操作入口：先问 OPA，再谈执行。"""
    decision = await policy.authorize(req.to_query(subject))
    if not decision.allow:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": decision.reason,
                "degraded": decision.degraded,
                "policy": "caseflow.authz",
            },
        )
    # 无审批单的旧探针不执行；W7 的正式入口绑定不可变审批快照。
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "reason": decision.reason,
            "message": "请通过 /v1/approval-requests/{id}/execute 执行已批准的计划",
        },
    )


@app.post("/v1/plans/preview", tags=["planning"])
async def plans_preview(
    req: PlanningRequest,
    request: Request,
    subject: SubjectDep,
    policy: PolicyDep,
    preview: PreviewDep,
    memory: MemoryDep,
) -> dict:
    """W5：追问或生成 DAG 并预演，成功也只返回 awaiting_approval，不执行写操作。"""
    graph = build_planning_graph(
        req,
        subject,
        policy,
        preview,
        interpreter=RecallInterpreter(
            FallbackInterpreter(get_settings().planning_providers), memory, subject
        ),
    )
    async with request.app.state.observability.operation(subject, "planning.preview"):
        return await graph.ainvoke({"trace": []})
