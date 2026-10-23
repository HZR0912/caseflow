"""OPA（Rego）策略客户端 —— 所有越权判断的唯一入口。

三条设计约束（也是面试会被追问的点）：

1. **fail-closed**：OPA 超时 / 5xx / 连不上 → 一律拒绝。策略引擎不可用时放行，
   等于把整个权限平面变成摆设。
2. **决策可解释**：调用 `/v1/data/caseflow/authz/decision` 拿 `{allow, reason}`，reason 进审计日志
   和前端提示，而不是只返回一个 false。
3. **不缓存决策**：角色可变、策略可热更新（OPA `--watch`），缓存会放大授权窗口。
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

# 策略包路径：policies/authz.rego -> package caseflow.authz，对外契约是 decision 对象。
# 只查 decision 而不是整个包：内部规则（角色映射表等）不进入调用方视野。
DEFAULT_DECISION_PATH = "/v1/data/caseflow/authz/decision"


class PolicySubject(BaseModel):
    """发起动作的主体。role 由身份系统给出，不接受客户端自报。"""

    id: str
    role: str
    tenant: str = "default"


class PolicyResource(BaseModel):
    """动作作用的对象。"""

    type: str
    id: str | None = None


class PolicyQuery(BaseModel):
    subject: PolicySubject
    action: str = Field(description="命名空间动作，如 code.invite_member / admin.grant")
    resource: PolicyResource

    def to_opa_input(self) -> dict[str, Any]:
        """转成 Rego 侧的 input 结构（保持与 policies/authz.rego 一致）。"""
        return {
            "user": {
                "id": self.subject.id,
                "role": self.subject.role,
                "tenant": self.subject.tenant,
            },
            "action": self.action,
            "resource": self.resource.model_dump(exclude_none=True),
        }


class PolicyDecision(BaseModel):
    allow: bool
    reason: str
    degraded: bool = Field(
        default=False, description="true 表示策略引擎不可用导致的兜底拒绝，需告警"
    )
    latency_ms: float = 0.0

    @classmethod
    def deny(cls, reason: str, *, degraded: bool, latency_ms: float = 0.0) -> PolicyDecision:
        return cls(allow=False, reason=reason, degraded=degraded, latency_ms=latency_ms)


class OPADecision(BaseModel):
    """OPA 的响应契约：禁止把字符串、数字等隐式转换为允许。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    allow: bool
    reason: str = Field(min_length=1)


class PolicyClient:
    """对 OPA HTTP API 的薄封装。故意不引入 OPA SDK：决策就是一次 POST。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 2.0,
        decision_path: str = DEFAULT_DECISION_PATH,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._decision_path = decision_path
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def authorize(self, query: PolicyQuery) -> PolicyDecision:
        started = time.perf_counter()
        try:
            response = await self._client.post(
                self._decision_path, json={"input": query.to_opa_input()}
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:  # 超时、连接失败、4xx/5xx 全部落到这里
            latency = (time.perf_counter() - started) * 1000
            return PolicyDecision.deny(
                f"policy_engine_unavailable:{type(exc).__name__}", degraded=True, latency_ms=latency
            )

        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("OPA response must be an object")
            # OPA 对 undefined 决策省略 result；显式 null 或错误类型是契约故障。
            if "result" not in body:
                return PolicyDecision.deny(
                    "policy_undefined",
                    degraded=True,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
            result = OPADecision.model_validate(body["result"])
        except ValueError:  # 包括 JSONDecodeError 和 Pydantic ValidationError
            return PolicyDecision.deny(
                "policy_engine_invalid_response",
                degraded=True,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        return PolicyDecision(
            allow=result.allow,
            reason=result.reason,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
