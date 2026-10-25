"""护栏流水线：把四层串成一次调用，产出唯一的处置结论。

执行顺序与理由（顺序错了会出真问题）：
1. **注入检测在最前**。检测要看攻击原文的变形；先做 PII 脱敏会把
   "忽略以上指令"之类的信号部分替换掉（万一攻击文本里带邮箱/电话）。
2. **PII 脱敏第二**。此后的意图分类与槽位抽取只在脱敏文本上做——
   模型上下文、日志、数据库里永远不出现原文。
3. **意图分类**。拿到置信度三档路由。
4. **槽位抽取**。只抽说出来的，缺的进追问清单。

处置结论的优先级（前者覆盖后者）：
    blocked        注入检测判定拦截 → 直接拒绝，不产生意图与槽位
    human_review  路由到人工（置信度不足/意图不明）
    clarify        意图够清楚但必需槽位缺失 → 只问缺的，一次问完
    route          进入规划

设计上刻意不让护栏自己"顺手"做权限判断：越权和数据外泄在这里只表现为
`risk` 标签和意图；当前不调用 OPA，W5 编排接入后由策略层决定是否放行。理由见 ADR-0002：
授权只有一个入口，两处判断必然漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from core.guardrails.injection import InjectionDetector, InjectionReport, RuleInjectionDetector
from core.guardrails.intent import IntentClassifier, IntentResult, RuleIntentClassifier, risk_labels
from core.guardrails.normalize import NormalizedInput, normalize
from core.guardrails.pii import MaskedText, PiiVault, mask
from core.guardrails.slots import SlotState, extract

Disposition = Literal["blocked", "human_review", "clarify", "route"]

# 意图 → 是否需要在进入规划前过一道 OPA。data_export 天然越权风险高，
# 即使分类置信度拉满也要先问策略；其余意图在规划出具体动作后再逐条问。
RISK_ACTIONS = {
    "data_export": "data.export",
}


@dataclass
class GuardrailResult:
    disposition: Disposition
    reasons: list[str] = field(default_factory=list)
    injection: InjectionReport | None = None
    masked_text: str = ""
    pii: MaskedText | None = None
    intent: IntentResult | None = None
    slots: SlotState | None = None
    risk: list[str] = field(default_factory=list)
    policy_action: str | None = None
    vault: PiiVault = field(default_factory=PiiVault, repr=False)

    def model_dump(self) -> dict:
        """审计与 API 输出。**不含 vault 内容**——占位符背后的真实值不出这个对象。"""
        return {
            "disposition": self.disposition,
            "reasons": self.reasons,
            "injection": self.injection.model_dump() if self.injection else None,
            "masked_text": self.masked_text,
            "pii": self.pii.model_dump() if self.pii else None,
            "intent": self.intent.model_dump() if self.intent else None,
            "slots": self.slots.model_dump() if self.slots else None,
            "risk": self.risk,
            "policy_action": self.policy_action,
        }


@dataclass
class Guardrails:
    detector: InjectionDetector = field(default_factory=RuleInjectionDetector)
    classifier: IntentClassifier = field(default_factory=RuleIntentClassifier)

    def check(
        self,
        text: str,
        *,
        today: date,
        existing_slots: dict[str, object] | None = None,
        vault: PiiVault | None = None,
    ) -> GuardrailResult:
        normalized: NormalizedInput = normalize(text)

        injection = self.detector.check(normalized)
        if injection.blocked:
            return GuardrailResult(
                disposition="blocked",
                reasons=[
                    f"注入检测拦截：{[f.rule_id for f in injection.findings]}",
                    *([f"变形痕迹：{injection.obfuscation}"] if injection.obfuscation else []),
                ],
                injection=injection,
                masked_text="",  # 被拦的原文不进下游，也不回显
            )

        masked = mask(normalized.text, vault)
        intent = self.classifier.classify(masked.text)
        slots = extract(masked.text, intent.intent, today=today, existing=existing_slots or {})
        risks = list(dict.fromkeys([*intent.risk, *risk_labels(masked.text)]))
        policy_action = RISK_ACTIONS.get(intent.intent)

        reasons: list[str] = []
        if injection.verdict == "flag":
            reasons.append(f"注入检测标记（未拦截）：{[f.rule_id for f in injection.findings]}")
        if risks:
            reasons.append(f"风险标签：{risks}（交策略层判定，护栏不拦）")
        if slots.questions:
            reasons.append(f"缺槽追问：{slots.questions}")

        if intent.route == "human":
            disposition: Disposition = "human_review"
            reasons.append(f"意图置信度不足（{intent.confidence:.2f}）")
        elif intent.route == "clarify":
            disposition = "clarify"
            reasons.append(
                f"意图接近但不够确定（{intent.confidence:.2f} / 第二名差距 {intent.margin:.2f}）"
            )
        elif not slots.complete:
            # 有缺槽但没有追问文案（比如只读问答的必填项），仍然算可以继续，
            # 缺的字段由 W5 的补全节点负责——这里不制造一次无意义的追问。
            disposition = "clarify" if slots.questions else "route"
        else:
            disposition = "route"

        return GuardrailResult(
            disposition=disposition,
            reasons=reasons,
            injection=injection,
            masked_text=masked.text,
            pii=masked,
            intent=intent,
            slots=slots,
            risk=risks,
            policy_action=policy_action,
            vault=masked.vault,
        )
