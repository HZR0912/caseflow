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

from core.guardrails.pipeline import Guardrails
from core.policy.opa_client import PolicyClient, PolicyQuery, PolicyResource, PolicySubject
from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.auth import SubjectDep
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

# 护栏是无状态纯函数集合，进程级单例即可（不依赖 lifespan，测试不必构造上下文）。
guardrails = Guardrails()


class ActionRequest(BaseModel):
    """一次写动作请求。W1 的字段刻意最小，W7 会扩成完整的 tool call 结构。"""

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
    # 放行也只到这一步：执行器（幂等键 / 重试 / dry-run / saga）在 W7-W8。
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "reason": decision.reason,
            "message": "策略已放行；执行器在 W7 实现（幂等键 / 重试 / 补偿）",
        },
    )
