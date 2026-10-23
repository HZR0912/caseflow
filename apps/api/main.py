"""CaseFlow API。

W1 只做两件事：
1. `/healthz` 探活。
2. 一条**受策略门保护**的写动作入口 `/v1/actions/execute` —— 被 OPA 拒绝即 403。
   执行器本体在 W7 才实现，这里先证明"策略能拒绝一次请求"（W1 验收项）。

刻意不做的事：不在这一层做权限判断。API 层只做鉴权（你是谁），
授权（你能不能做）永远交给 OPA，避免两处逻辑各自演化。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from core.policy.opa_client import PolicyClient, PolicyQuery, PolicyResource, PolicySubject
from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from apps.api.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.policy = PolicyClient(base_url=settings.opa_url, timeout=settings.opa_timeout_s)
    try:
        yield
    finally:
        await app.state.policy.aclose()


app = FastAPI(
    title="CaseFlow API",
    version="0.1.0",
    description="企业办事与审批 Agent —— 写操作平面",
    lifespan=lifespan,
)


def get_policy_client(request: Request) -> PolicyClient:
    return request.app.state.policy


PolicyDep = Annotated[PolicyClient, Depends(get_policy_client)]


class ActionRequest(BaseModel):
    """一次写动作请求。W1 的字段刻意最小，W7 会扩成完整的 tool call 结构。"""

    user_id: str = Field(description="发起人 ID")
    role: str = Field(description="发起人角色，取自身份系统，不由客户端自报")
    action: str = Field(description="命名空间动作，如 code.invite_member")
    resource_type: str = Field(description="资源类型，如 repo / account / order")
    resource_id: str | None = None
    tenant: str = "default"

    def to_query(self) -> PolicyQuery:
        return PolicyQuery(
            subject=PolicySubject(id=self.user_id, role=self.role, tenant=self.tenant),
            action=self.action,
            resource=PolicyResource(type=self.resource_type, id=self.resource_id),
        )


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    settings = get_settings()
    return {"status": "ok", "service": "caseflow-api", "env": settings.env}


@app.post("/v1/policy/check", tags=["policy"])
async def policy_check(req: ActionRequest, policy: PolicyDep) -> dict:
    """只问策略，不执行 —— 调试和前端预览用。"""
    decision = await policy.authorize(req.to_query())
    return decision.model_dump()


@app.post("/v1/actions/execute", tags=["actions"])
async def execute_action(req: ActionRequest, policy: PolicyDep) -> dict:
    """受策略门保护的写操作入口：先问 OPA，再谈执行。"""
    decision = await policy.authorize(req.to_query())
    if not decision.allow:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": decision.reason,
                "degraded": decision.degraded,
                "policy": "caseflow.authz",
            },
        )
    # 放行也只到这一步：执行器（幂等键 / 重试 / dry-run / saga）在 W7-W8。
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "reason": decision.reason,
            "message": "策略已放行；执行器在 W7 实现（幂等键 / 重试 / 补偿）",
        },
    )
