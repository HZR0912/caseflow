"""Allowlisted MockCorp HTTP adapter and conservative failure classification."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx
from apps.mockcorp.models import Identifier
from apps.mockcorp.preview import SCHEMAS
from pydantic import TypeAdapter

from core.obs.runtime import headers


@dataclass
class ToolResult:
    status: str
    code: str = ""
    result_id: str | None = None
    retry_after: float = 0
    outcome_unknown: bool = False
    replayed: bool = False
    items: list[dict] | None = None


BUSINESS_ERRORS = {
    "domain_policy",
    "email_domain_denied",
    "duplicate_name",
    "seat_limit",
    "insufficient_funds",
}


def retry_after(value: str | None) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0


def classify(response: httpx.Response, operation: str, arguments=None) -> ToolResult:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    error = body.get("error")
    code = error.get("code", "") if isinstance(error, dict) else ""
    if not isinstance(code, str):
        code = ""
    # Never persist arbitrary upstream text (it may contain PII or credentials).
    known = BUSINESS_ERRORS | {
        "idempotency_key_conflict",
        "invalid_tool_arguments",
        "compensation_state_changed",
        "compensation_dependency_in_use",
        "compensation_source_mismatch",
        "compensation_unsupported",
    }
    safe_code = code if isinstance(code, str) and code in known else f"http_{response.status_code}"
    if operation == "ticket.assign_tickets" and response.status_code in {200, 207}:
        try:
            result = body["result"]
            items = result["items"]
            expected = arguments["ticket_ids"]
            ids = [item["id"] for item in items]
            succeeded = sum(item["status"] == "assigned" for item in items)
            if (
                body.get("operation") != operation
                or body.get("dry_run") is not False
                or len(ids) != len(set(ids))
                or set(ids) != set(expected)
                or any(item["status"] not in {"assigned", "failed"} for item in items)
                or type(result["succeeded"]) is not int
                or type(result["failed"]) is not int
                or result["succeeded"] != succeeded
                or result["failed"] != len(items) - succeeded
                or (response.status_code == 200 and result["failed"] != 0)
            ):
                raise ValueError("invalid_batch_receipt")
            return ToolResult(
                ("partial_success" if succeeded else "compensation_required")
                if result["failed"]
                else "succeeded",
                code="batch_all_failed" if not succeeded else "",
                items=[{"id": item["id"], "status": item["status"]} for item in items],
                replayed=response.headers.get("Idempotency-Replayed") == "true",
            )
        except (ValueError, KeyError, TypeError):
            return ToolResult("human_required", "invalid_batch_receipt", outcome_unknown=True)
    if code in BUSINESS_ERRORS:
        return ToolResult("compensation_required", safe_code)
    if response.status_code in {408, 429} or response.status_code >= 500:
        delay = retry_after(response.headers.get("Retry-After"))
        if delay > 86400:
            return ToolResult("human_required", "retry_after_too_large", outcome_unknown=True)
        return ToolResult(
            "retryable",
            safe_code,
            retry_after=delay,
            outcome_unknown=response.status_code != 429,
        )
    if response.status_code == 409 and code != "idempotency_key_conflict":
        return ToolResult("compensation_required", safe_code)
    if response.status_code in {200, 201}:
        result = body.get("result")
        try:
            identifier = TypeAdapter(Identifier).validate_python(result["id"])
            if body.get("operation") != operation or body.get("dry_run") is not False:
                raise ValueError("invalid_receipt")
        except (ValueError, KeyError, TypeError):
            return ToolResult("human_required", "invalid_success_receipt", outcome_unknown=True)
        return ToolResult(
            "succeeded",
            result_id=identifier,
            replayed=response.headers.get("Idempotency-Replayed") == "true",
        )
    return ToolResult(
        "human_required", safe_code, outcome_unknown=200 <= response.status_code < 300
    )


class HttpToolGateway:
    def __init__(self, base_url: str, bearer: str, client=None, timeout=10):
        self.base_url, self.bearer = base_url.rstrip("/"), bearer
        self.client, self.timeout = client, timeout

    async def call(self, step, arguments, key):
        payload = SCHEMAS[step.operation].model_validate(arguments).model_dump(mode="json")
        paths = {
            "identity.create_user": "/v1/identity/users",
            "identity.create_mailbox": "/v1/identity/mailboxes",
            "ticket.create_ticket": "/v1/ticket/tickets",
            "ticket.assign_tickets": "/v1/ticket/tickets/assign",
            "money.create_order": "/v1/money/orders",
            "money.bind_seat": "/v1/money/seats",
        }
        if step.operation == "code.invite_member":
            path = "/v1/code/repos/" + quote(payload.pop("repo_id"), safe="") + "/members"
        else:
            path = paths[step.operation]

        async def send(client):
            try:
                response = await client.post(
                    self.base_url + path,
                    json=payload,
                    headers={"Authorization": self.bearer, "Idempotency-Key": key, **headers()},
                    timeout=self.timeout,
                    follow_redirects=False,
                )
            except httpx.TransportError:
                return ToolResult("retryable", "transport_error", outcome_unknown=True)
            return classify(response, step.operation, arguments)

        if self.client is not None:
            return await send(self.client)
        async with httpx.AsyncClient() as client:
            return await send(client)

    async def compensate(self, operation, source_key, key):
        async def send(client):
            try:
                response = await client.post(
                    self.base_url + "/v1/compensations",
                    json={"operation": operation, "source_key": source_key},
                    headers={"Authorization": self.bearer, "Idempotency-Key": key, **headers()},
                    timeout=self.timeout,
                    follow_redirects=False,
                )
            except httpx.TransportError:
                return ToolResult("retryable", "transport_error", outcome_unknown=True)
            return classify(response, "saga.compensate")

        if self.client is not None:
            return await send(self.client)
        async with httpx.AsyncClient() as client:
            return await send(client)
