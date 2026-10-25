"""显式槽位、保守解析和入职 DAG 模板。未知值追问，不生成账号拼音或资源 ID。"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field

from core.graph.contracts import Plan, PlanStep, Ref, StrictModel
from core.guardrails.slots import extract


class Employee(StrictModel):
    name: str = Field(min_length=1, max_length=50)
    username: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class OnboardingFacts(StrictModel):
    employees: list[Employee] = Field(default_factory=list, max_length=10)
    quantity: int | None = Field(default=None, ge=1, le=10)
    role: str | None = Field(default=None, max_length=50)
    start_date: date | None = None
    end_date: date | None = None
    email_domain: str | None = Field(default=None, pattern=r"^[a-z0-9.-]+\.[a-z]{2,}$")
    repo_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")
    permission: Literal["read", "write", "admin"] | None = None
    department_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")
    paid_item: str | None = Field(default=None, min_length=1, max_length=100)
    unit_price_cents: int | None = Field(default=None, strict=True, gt=0, le=100_000_000)
    assignment_ticket_ids: list[str] = Field(default_factory=list, max_length=100)

    def questions(self) -> list[str]:
        missing = []
        if (self.paid_item is None) != (self.unit_price_cents is None):
            missing.append("购买席位需同时提供 paid_item 和 unit_price_cents（每人一席，单位分）。")
        count = len(self.employees) * (6 if self.paid_item else 4) + bool(
            self.assignment_ticket_ids
        )
        if count > 40:
            missing.append("当前计划最多 40 步，请减少本批员工人数或拆分申请。")
        if len(set(self.assignment_ticket_ids)) != len(self.assignment_ticket_ids):
            missing.append("批量分配的工单 ID 重复，请提供唯一工单列表。")
        if not self.employees:
            missing.append("请提供员工姓名和账号名，例如张三(zhangsan)，或填写 employees。")
        if self.quantity and len(self.employees) != self.quantity:
            missing.append(
                f"人数为 {self.quantity}，目前提供 {len(self.employees)} 人，请核对名单。"
            )
        if len({e.username for e in self.employees}) != len(self.employees):
            missing.append("名单内账号名重复，请确认每人的唯一账号名。")
        for key, question in {
            "role": "岗位或团队是什么？",
            "start_date": "哪天入职？",
            "end_date": "权限截止日期是哪天？",
            "email_domain": "邮箱域是什么？",
            "repo_id": "已有代码仓库的 ID 是什么？",
            "project_id": "已有工单项目的 ID 是什么？",
            "permission": "仓库权限使用 read、write 还是 admin？",
        }.items():
            if getattr(self, key) is None:
                missing.append(question)
        if self.start_date and self.end_date and self.end_date < self.start_date:
            missing.append("权限截止日期不能早于入职日期，请更正。")
        return missing


class MemorySelection(StrictModel):
    session_id: UUID | None = None
    session_version: int | None = Field(default=None, ge=0)
    use_preferences: bool = False
    use_organization: bool = False


class PlanningRequest(StrictModel):
    text: str = Field(min_length=1, max_length=4000)
    today: date = Field(default_factory=date.today)
    facts: OnboardingFacts = Field(default_factory=OnboardingFacts)
    memory: MemorySelection | None = None


class Interpreter(Protocol):
    name: str

    async def understand(self, request: PlanningRequest) -> OnboardingFacts: ...


class RuleInterpreter:
    name = "rules"

    async def understand(self, request: PlanningRequest) -> OnboardingFacts:
        text = request.text
        slots = extract(text, "provision_employee", today=request.today).values
        values = {key: slots[key] for key in ("role", "start_date", "end_date") if key in slots}
        employees = re.findall(r"([一-鿿]{2,4})[（(]([a-z][a-z0-9_-]{0,63})[)）]", text)
        if employees:
            values["employees"] = [
                dict(name=name, username=username) for name, username in employees
            ]
        for key, pattern in {
            "quantity": r"(\d+)\s*(?:个|位|名)\s*(?:实习生|员工|新人|同事)",
            "email_domain": r"邮箱域\s*[:：=]?\s*([a-z0-9.-]+\.[a-z]{2,})",
            "repo_id": r"仓库(?:ID)?\s*[:：= ]+([A-Za-z0-9_-]+)",
            "project_id": r"项目(?:ID)?\s*[:：= ]+([A-Za-z0-9_-]+)",
            "permission": r"权限\s*[:：= ]+\b(read|write|admin)\b",
        }.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                values[key] = int(match[1]) if key == "quantity" else match[1].lower()
        if "permission" not in values:
            permissions = {
                permission
                for phrase, permission in (("只读", "read"), ("读写", "write"), ("管理员", "admin"))
                if phrase in text
            }
            if len(permissions) == 1:
                values["permission"] = permissions.pop()
        # 用户补充的结构化槽位是显式修正；不接受身份、授权或任意工具字段。
        values.update(request.facts.model_dump(mode="json", exclude_unset=True, exclude_none=True))
        return OnboardingFacts.model_validate(values)


def make_plan(facts: OnboardingFacts, evidence_ids: list[str]) -> Plan:
    if facts.questions():
        raise ValueError("incomplete_facts")
    steps = []
    for i, employee in enumerate(facts.employees, 1):
        user, mail, invite, ticket = (
            f"p{i}_{kind}" for kind in ("user", "mail", "invite", "ticket")
        )
        common = {"evidence_ids": evidence_ids}
        steps.extend(
            [
                PlanStep(
                    id=user,
                    operation="identity.create_user",
                    arguments={"username": employee.username, "display_name": employee.name},
                    risk="medium",
                    reversibility="compensable",
                    rollback="同组失败时禁用本次创建的账号；保留历史记录，有外部依赖时转人工。",
                    saga_group=f"p{i}_identity",
                    **common,
                ),
                PlanStep(
                    id=mail,
                    operation="identity.create_mailbox",
                    arguments={
                        "user_id": Ref(step=user),
                        "email": f"{employee.username}@{facts.email_domain}",
                    },
                    depends_on=[user],
                    risk="medium",
                    reversibility="compensable",
                    rollback="同组失败时停用本次创建的邮箱；保留历史记录。",
                    saga_group=f"p{i}_identity",
                    **common,
                ),
                PlanStep(
                    id=invite,
                    operation="code.invite_member",
                    arguments={
                        "repo_id": facts.repo_id,
                        "user_id": Ref(step=user),
                        "permission": facts.permission,
                    },
                    depends_on=[user, mail],
                    risk="low" if facts.permission == "read" else "high",
                    reversibility="reversible",
                    rollback="仅撤销本次创建且未被修改的仓库成员；保留账号和邮箱。",
                    saga_group=f"p{i}_access",
                    **common,
                ),
                PlanStep(
                    id=ticket,
                    operation="ticket.create_ticket",
                    arguments={
                        "project_id": facts.project_id,
                        "assignee_id": Ref(step=user),
                        "title": f"入职验收：{employee.name}，权限截止 {facts.end_date}",
                    },
                    depends_on=[user, invite],
                    risk="low",
                    reversibility="irreversible",
                    rollback="当前工具没有删除工单接口，后续只能人工关闭。",
                    saga_group=f"p{i}_access",
                    **common,
                ),
            ]
        )
        if facts.paid_item is not None:
            order, seat = f"p{i}_order", f"p{i}_seat"
            steps[-1].depends_on.append(seat)
            steps.extend(
                [
                    PlanStep(
                        id=order,
                        operation="money.create_order",
                        arguments={
                            "item": facts.paid_item,
                            "quantity": 1,
                            "unit_price_cents": facts.unit_price_cents,
                        },
                        depends_on=[invite],
                        risk="high",
                        reversibility="compensable",
                        rollback="按原订单金额退款；保留订单和退款审计记录。",
                        saga_group=f"p{i}_access",
                        **common,
                    ),
                    PlanStep(
                        id=seat,
                        operation="money.bind_seat",
                        arguments={"order_id": Ref(step=order), "user_id": Ref(step=user)},
                        depends_on=[order, user],
                        risk="high",
                        reversibility="compensable",
                        rollback="停用本次创建且未被修改的席位。",
                        saga_group=f"p{i}_access",
                        **common,
                    ),
                ]
            )
    if facts.assignment_ticket_ids:
        steps.append(
            PlanStep(
                id="assign_tickets",
                operation="ticket.assign_tickets",
                arguments={
                    "ticket_ids": facts.assignment_ticket_ids,
                    "user_id": Ref(step="p1_user"),
                },
                depends_on=[
                    "p1_user",
                    *[f"p{i}_ticket" for i in range(1, len(facts.employees) + 1)],
                ],
                risk="medium",
                reversibility="reversible",
                rollback="仅恢复本批次实际修改且未再次变化的工单负责人；失败项不动。",
                saga_group="ticket_assignment",
                **common,
            )
        )
    return Plan(
        steps=steps,
        saga_version=1,
        assumptions=[f"使用已确认仓库 {facts.repo_id} 和项目 {facts.project_id}，不自动新建。"],
        warnings=[
            "补偿按步骤 saga_group 分组：身份与访问权限分开；组外成功保留。补偿不是删除审计历史。",
            "仅预演：尚未创建业务资源；审批通过后才能正式执行。",
            f"入职 {facts.start_date}、权限截止 {facts.end_date} 为计划约束；"
            "MockCorp 尚无定时开通/到期回收能力。",
            "预演 ID 不预留；正式执行必须重新校验状态、授权和有效期。",
        ],
    )
