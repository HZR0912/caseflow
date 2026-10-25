from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]
Name = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^\S(?:.*\S)?$")]
Permission = Literal["read", "write", "admin"]
Cents = Annotated[int, Field(strict=True, gt=0, le=100_000_000)]


class MockSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCKCORP_", env_file=".env", extra="ignore")
    faults_enabled: bool = False
    fault_delay_s: float = Field(default=1.0, ge=0, le=10)
    initial_balance_cents: int = Field(default=100_000, ge=0)
    code_seat_limit: int = Field(default=3, ge=1)
    allowed_email_domains: list[str] = Field(
        default_factory=lambda: ["example.com", "mockcorp.test"]
    )


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateUser(Input):
    username: Identifier
    display_name: Name


class CreateMailbox(Input):
    user_id: Identifier
    email: str = Field(min_length=3, max_length=254, pattern=r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$")


class ResetPassword(Input):
    new_password: SecretStr = Field(min_length=12, max_length=128)


class CreateRepo(Input):
    name: Identifier


class InviteMember(Input):
    user_id: Identifier
    permission: Permission = "read"


class SetPermission(Input):
    permission: Permission


class CreateProject(Input):
    key: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,15}$")
    name: Name


class CreateTicket(Input):
    project_id: Identifier
    title: Name
    assignee_id: Identifier | None = None


class AssignTickets(Input):
    ticket_ids: list[Identifier] = Field(min_length=1, max_length=100)
    user_id: Identifier


class CreateOrder(Input):
    item: Name
    quantity: int = Field(strict=True, ge=1, le=1000)
    unit_price_cents: Cents


class BindSeat(Input):
    order_id: Identifier
    user_id: Identifier


class Refund(Input):
    order_id: Identifier
    amount_cents: Cents


class CompensationRequest(Input):
    source_key: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,200}$")
    operation: str = Field(pattern=r"^[a-z]+\.[a-z_]+$")
