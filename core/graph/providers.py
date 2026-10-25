"""可选 JSON 槽位解析：Qwen/DeepSeek 的 chat/completions 兼容接口，失败退回规则。

模型只提议事实，不提供身份、授权、工具名或 DAG。每个新增值必须能在输入找到依据。
"""

from __future__ import annotations

import json

import httpx
from apps.api.settings import PlanningProvider

from core.graph.onboarding import OnboardingFacts, PlanningRequest, RuleInterpreter
from core.guardrails.pii import mask
from core.obs.model import completion


class FallbackInterpreter:
    def __init__(self, providers: list[PlanningProvider], client: httpx.AsyncClient | None = None):
        self.providers = providers
        self.client = client
        self.name = "rules"

    async def understand(self, request: PlanningRequest) -> OnboardingFacts:
        baseline = await RuleInterpreter().understand(request)
        masked = mask(request.text)
        text = masked.text
        for employee in baseline.employees:
            for kind, raw in (("person", employee.name), ("account", employee.username)):
                token = masked.vault.tokenize(kind, raw)
                text = text.replace(raw, token)
        for provider in self.providers:
            try:

                async def call(client, provider=provider):
                    return await completion(
                        client,
                        provider,
                        url=provider.base_url.rstrip("/") + "/chat/completions",
                        headers={"Authorization": f"Bearer {provider.api_key.get_secret_value()}"},
                        json={
                            "model": provider.model,
                            "temperature": 0,
                            "response_format": {"type": "json_object"},
                            "messages": [
                                {
                                    "role": "system",
                                    "content": "只提取用户明确提供的入职事实，输出 JSON。缺值省略。"
                                    "不得猜姓名拼音、资源 ID、权限、日期或数量。保留脱敏占位符。"
                                    "字段 schema："
                                    + json.dumps(OnboardingFacts.model_json_schema()),
                                },
                                {"role": "user", "content": text},
                            ],
                        },
                        timeout=provider.timeout_s,
                    )

                if self.client is None:
                    async with httpx.AsyncClient() as client:
                        content = await call(client)
                else:
                    content = await call(self.client)
                candidate = OnboardingFacts.model_validate_json(masked.vault.restore(content))
                values = baseline.model_dump(mode="json", exclude_none=True)
                original = request.text.lower()
                for key, value in candidate.model_dump(mode="json", exclude_none=True).items():
                    if value == values.get(key):
                        continue
                    # 不支持模型新造员工实体；账号与姓名由显式名单/确认槽位提供。
                    if key == "employees":
                        if value:
                            raise ValueError("unconfirmed_employees")
                        continue
                    aliases = {"read": "只读", "write": "读写", "admin": "管理员"}
                    grounded = (
                        str(value).lower() in original or aliases.get(str(value), "\0") in original
                    )
                    if not grounded or values.get(key) is not None:
                        continue
                    values[key] = value
                self.name = provider.name
                return OnboardingFacts.model_validate(values)
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                continue
        self.name = "rules_fallback" if self.providers else "rules"
        return baseline
