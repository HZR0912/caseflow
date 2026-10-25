"""Bounded structured memory inputs. No arbitrary instructions or authentication roles."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from core.graph.contracts import StrictModel
from core.graph.onboarding import OnboardingFacts, PlanningRequest
from core.guardrails.injection import RuleInjectionDetector
from core.guardrails.normalize import normalize
from core.guardrails.pii import mask

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
PreferenceKey = Literal["department_id", "email_domain", "repo_id", "project_id", "permission"]
LABELS = {
    "department_id": "常用部门 团队 入职",
    "email_domain": "邮箱域 邮件 入职",
    "repo_id": "代码仓库 项目 入职",
    "project_id": "工单项目 入职",
    "permission": "仓库权限 入职",
}


def safe_text(text):
    if RuleInjectionDetector().check(normalize(text)).blocked or mask(text).vault.entries:
        raise ValueError("unsafe_memory_content")
    return text


class PreferenceWrite(StrictModel):
    key: PreferenceKey
    value: str = Field(min_length=1, max_length=100)
    confirmed: bool = False
    expected_version: int = Field(default=0, ge=0)
    ttl_days: int = Field(default=180, ge=1, le=365)

    @model_validator(mode="after")
    def valid_value(self):
        safe_text(self.value)
        OnboardingFacts.model_validate({self.key: self.value})
        return self


class SessionTurn(StrictModel):
    turn_id: UUID
    expected_version: int = Field(ge=0)
    request: PlanningRequest
    clear_fields: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_clear(self):
        if any(k not in OnboardingFacts.model_fields for k in self.clear_fields):
            raise ValueError("unknown_slot")
        return self


class Department(StrictModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=80)
    parent_id: Identifier | None = None
    active: bool = True


class Member(StrictModel):
    id: Identifier
    department_id: Identifier
    manager_id: Identifier | None = None
    mentor_id: Identifier | None = None
    is_lead: bool = False
    active: bool = True


class OwnedSystem(StrictModel):
    department_id: Identifier
    kind: Literal["repo_id", "project_id", "email_domain"]
    resource_id: str = Field(min_length=1, max_length=100)
    owner_id: Identifier


class OrganizationWrite(StrictModel):
    expected_version: int = Field(ge=0)
    departments: list[Department] = Field(default_factory=list, max_length=100)
    members: list[Member] = Field(default_factory=list, max_length=1000)
    systems: list[OwnedSystem] = Field(default_factory=list, max_length=300)

    @model_validator(mode="after")
    def relationships(self):
        def unique(items):
            result = {i.id: i for i in items}
            if len(result) != len(items):
                raise ValueError("duplicate_org_id")
            return result

        departments, members = unique(self.departments), unique(self.members)
        for items, field in (
            (departments, "parent_id"),
            (members, "manager_id"),
            (members, "mentor_id"),
        ):
            for item in items.values():
                path = {item.id}
                current = getattr(item, field)
                while current is not None:
                    if current not in items:
                        raise ValueError("unknown_org_reference")
                    if current in path:
                        raise ValueError("cyclic_organization")
                    path.add(current)
                    current = getattr(items[current], field)
        leads, systems = set(), set()
        for dept in self.departments:
            safe_text(dept.name)
        for member in self.members:
            if member.department_id not in departments:
                raise ValueError("unknown_department")
            if member.active and not departments[member.department_id].active:
                raise ValueError("active_member_in_inactive_department")
            if member.active and member.is_lead:
                if member.department_id in leads:
                    raise ValueError("ambiguous_department_lead")
                leads.add(member.department_id)
        for system in self.systems:
            if system.department_id not in departments or system.owner_id not in members:
                raise ValueError("unknown_system_owner_or_department")
            if not members[system.owner_id].active or not departments[system.department_id].active:
                raise ValueError("inactive_system_owner_or_department")
            key = (system.department_id, system.kind)
            if key in systems:
                raise ValueError("ambiguous_system_default")
            systems.add(key)
            safe_text(system.resource_id)
            OnboardingFacts.model_validate({system.kind: system.resource_id})
        return self
