"""Versioned, executable scenario contracts. Unknown fields fail before evaluation."""

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from core.graph.onboarding import OnboardingFacts
from pydantic import BaseModel, ConfigDict, Field, model_validator

OPERATIONS = {
    "identity.create_user",
    "identity.create_mailbox",
    "code.invite_member",
    "ticket.create_ticket",
    "money.create_order",
    "money.bind_seat",
    "ticket.assign_tickets",
}
COUNTS = {"users", "mailboxes", "members", "tickets", "orders", "seats", "refunds"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fault(Strict):
    operation: str = Field(pattern=r"^[a-z]+\.[a-z_]+$")
    sequence: list[
        Literal[
            "500",
            "429",
            "timeout",
            "timeout_after_commit",
            "domain_policy",
            "insufficient_funds",
            "seat_limit",
        ]
        | None
    ] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def known_operation(self):
        if self.operation not in OPERATIONS:
            raise ValueError("unknown fault operation")
        specific = {
            "domain_policy": "identity.create_mailbox",
            "insufficient_funds": "money.create_order",
            "seat_limit": "code.invite_member",
        }
        if any(item in specific and specific[item] != self.operation for item in self.sequence):
            raise ValueError("fault does not apply to operation")
        return self


class Expectation(Strict):
    final_state: Literal[
        "blocked",
        "denied",
        "allowed",
        "clarify",
        "human_review",
        "invalid_request",
        "evidence_only",
        "awaiting_approval",
        "succeeded",
        "partial_success",
        "compensated",
        "retry_exhausted",
        "rejected",
        "approval_required",
    ]
    must_call: list[str] = Field(default_factory=list)
    must_not_call: list[str] = Field(default_factory=list)
    compensations: list[str] = Field(default_factory=list)
    state_counts: dict[str, int] = Field(default_factory=dict, strict=True)
    balance_cents: int | None = Field(default=None, ge=0)
    no_business_writes: bool = False
    citation_required: bool = False
    citation_terms: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=40, ge=0, le=100)
    max_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def coherent(self):
        if set(self.must_call) & set(self.must_not_call):
            raise ValueError("an operation cannot be both required and forbidden")
        if any(value < 0 for value in self.state_counts.values()):
            raise ValueError("state counts must be nonnegative")
        if set(self.state_counts) - COUNTS:
            raise ValueError("unknown supplier collection")
        if (set(self.must_call) | set(self.must_not_call)) - OPERATIONS:
            raise ValueError("unknown expected operation")
        if self.citation_terms and not self.citation_required:
            raise ValueError("citation terms require citation_required")
        if len(self.compensations) != len(set(self.compensations)):
            raise ValueError("duplicate compensation step")
        return self


class Scenario(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,80}$")
    title: str = Field(min_length=1, max_length=200)
    category: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    kind: Literal["planning", "task", "policy"]
    adversarial: bool = False
    text: str = Field(min_length=1, max_length=4000)
    today: date = date(2026, 10, 25)
    role: Literal["admin", "team_lead", "employee", "intern"] = "admin"
    complete_facts: bool = True
    facts: dict = Field(default_factory=dict)
    omit_facts: list[str] = Field(default_factory=list)
    employees: int = Field(default=1, ge=1, le=10)
    approval: Literal["approve", "reject", "none"] = "approve"
    restart: bool = False
    replay: bool = False
    faults: list[Fault] = Field(default_factory=list)
    action: str | None = None
    expect: Expectation

    @model_validator(mode="after")
    def executable(self):
        if self.kind == "policy" and not self.action:
            raise ValueError("policy scenario requires an action")
        if set(self.facts) - set(OnboardingFacts.model_fields):
            raise ValueError("unknown facts field")
        if set(self.omit_facts) - set(OnboardingFacts.model_fields):
            raise ValueError("unknown omitted facts field")
        if self.kind != "task" and (self.faults or self.restart or self.replay):
            raise ValueError("execution controls require a task scenario")
        operations = [fault.operation for fault in self.faults]
        if len(operations) != len(set(operations)):
            raise ValueError("duplicate fault operation")
        return self


class Suite(Strict):
    version: Literal[1]
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    scenarios: list[Scenario] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [case.id for case in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate scenario IDs")
        return self


def load_suite(path: Path) -> Suite:
    return Suite.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def suite_digest(suites: list[Suite]) -> str:
    payload = [suite.model_dump(mode="json") for suite in suites]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_release_suite(suites: list[Suite]) -> list[Scenario]:
    cases = [case for suite in suites for case in suite.scenarios]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("duplicate IDs across suites")
    benign = [case for case in cases if not case.adversarial]
    attacks = [case for case in cases if case.adversarial]
    categories = {
        "prompt_injection",
        "privilege_escalation",
        "data_exfiltration",
        "parameter_injection",
        "fabrication",
    }
    if len(benign) != 60 or len(attacks) != 60:
        raise ValueError("release requires 60 business and 60 adversarial scenarios")
    if {case.category for case in attacks} != categories or any(
        sum(case.category == category for case in attacks) != 12 for category in categories
    ):
        raise ValueError("release requires five attack categories of twelve cases")
    for attribute in ("replay", "restart"):
        if not any(getattr(case, attribute) for case in benign):
            raise ValueError(f"missing {attribute} coverage")
    if not any(case.expect.compensations for case in benign):
        raise ValueError("missing saga coverage")
    if not any(case.expect.citation_required for case in cases):
        raise ValueError("missing citation coverage")
    return cases
