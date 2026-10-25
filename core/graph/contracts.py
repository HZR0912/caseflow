"""W5 的计划契约。步骤只能引用已声明的依赖，不允许任意工具或表达式。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Operation = Literal[
    "identity.create_user",
    "identity.create_mailbox",
    "code.invite_member",
    "ticket.create_ticket",
    "money.create_order",
    "money.bind_seat",
    "ticket.assign_tickets",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ref(StrictModel):
    step: str
    field: Literal["id"] = "id"


class PlanStep(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")
    operation: Operation
    arguments: dict[str, str | int | list[str] | Ref]
    depends_on: list[str] = Field(default_factory=list, max_length=40)
    risk: Literal["low", "medium", "high"]
    reversibility: Literal["reversible", "compensable", "irreversible"]
    rollback: str
    evidence_ids: list[str] = Field(default_factory=list)
    saga_group: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,49}$")


class Plan(StrictModel):
    steps: list[PlanStep] = Field(min_length=1, max_length=40)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    saga_version: Literal[1] | None = None

    @model_validator(mode="after")
    def validate_dag(self):
        by_id = {step.id: step for step in self.steps}
        if len(by_id) != len(self.steps):
            raise ValueError("duplicate_step_id")
        for step in self.steps:
            if len(set(step.depends_on)) != len(step.depends_on):
                raise ValueError("duplicate_dependency")
            if any(dep not in by_id or dep == step.id for dep in step.depends_on):
                raise ValueError("unknown_or_self_dependency")
            for value in step.arguments.values():
                if isinstance(value, Ref) and value.step not in step.depends_on:
                    raise ValueError("reference_must_be_declared_dependency")
        self.ordered_steps()
        return self

    def ordered_steps(self) -> list[PlanStep]:
        remaining = list(self.steps)
        ordered: list[PlanStep] = []
        done: set[str] = set()
        while remaining:
            ready = [step for step in remaining if set(step.depends_on) <= done]
            if not ready:
                raise ValueError("cyclic_plan")
            for step in ready:
                ordered.append(step)
                done.add(step.id)
                remaining.remove(step)
        return ordered

    def mermaid(self) -> str:
        # 仅插入已校验的 ID/操作名，不把用户文本插入 Mermaid 语法。
        lines = ["flowchart TD"]
        for step in self.steps:
            lines.append(f'  {step.id}["{step.id}: {step.operation}"]')
            lines.extend(f"  {dep} --> {step.id}" for dep in step.depends_on)
        return "\n".join(lines)
