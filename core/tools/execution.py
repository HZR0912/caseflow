"""Durable sequential executor. Persist intent before IO; replay with the same key."""

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from apps.mockcorp.preview import SCHEMAS
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from core.graph.approval_service import ApprovalError, digest
from core.graph.contracts import Ref
from core.obs.privacy import label
from core.obs.runtime import span
from core.policy.opa_client import PolicyQuery, PolicyResource
from core.tools.saga import Saga, summarize


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4  # Initial attempt + at most three retries, across restarts.
    base_delay: float = 0.5
    max_delay: float = 8
    max_inline_delay: float = 5

    def delay(self, attempt, retry_after=0):
        ceiling = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return max(retry_after, ceiling * random.uniform(0.75, 1.0))


def idempotency_key(tenant, request_id, plan_hash, step, arguments):
    # Include the resolved target/payload, operation and immutable plan version.
    return "cf7:" + digest([tenant, request_id, plan_hash, step.id, step.operation, arguments])


class ExecutionService:
    def __init__(self, approvals, gateway, retry=None):
        self.approvals, self.gateway = approvals, gateway
        self.retry = retry or RetryPolicy()

    async def execute(self, request_id, subject):
        service = self.approvals
        # Same lock namespace as approval: decisions/recovery/execution cannot race.
        async with service.session(request_id) as (conn, graph):
            row = await service.row(conn, request_id, subject)
            await service.require(subject, "approval.execute", request_id)
            if digest(row["snapshot"]) != row["plan_hash"]:
                raise ApprovalError(409, "approval_plan_changed")
            decision = row["decision"] or {}
            if (
                decision.get("decision") != "approve"
                or decision.get("plan_hash") != row["plan_hash"]
            ):
                raise ApprovalError(409, "execution_requires_approval")
            state = await graph.aget_state(service.config(request_id))
            if state.next or state.values.get("status") != "approved_ready":
                raise ApprovalError(409, "approval_recovery_required")
            if service.cipher is None:
                raise ApprovalError(503, "execution_key_not_configured")
            if not row.get("execution_payload"):
                raise ApprovalError(409, "legacy_approval_resubmit_required")
            try:
                plan = service.cipher.open(row["execution_payload"], subject.tenant, request_id)
            except ValueError as exc:
                raise ApprovalError(409, "execution_payload_invalid") from exc
            if digest(plan.model_dump(mode="json", exclude_unset=True)) != row["snapshot"].get(
                "execution_plan_hash"
            ):
                raise ApprovalError(409, "execution_plan_changed")
            cursor = await conn.execute(
                "SELECT journal FROM execution_runs WHERE request_id=%s", (request_id,)
            )
            saved = await cursor.fetchone()
            journal = saved["journal"] if saved else None
            if journal and journal["status"] in {"compensating", "compensation_waiting"}:
                return await Saga(self).run(conn, plan, journal, subject)
            if journal and journal["status"] not in {"running", "retry_waiting"}:
                if (
                    plan.saga_version == 1
                    and "saga" not in journal
                    and journal["status"]
                    in {
                        "compensation_required",
                        "human_required",
                        "retry_exhausted",
                        "partial_success",
                    }
                ):
                    return await Saga(self).run(conn, plan, journal, subject)
                return journal
            if journal is None:
                if datetime.now(UTC) >= row["expires_at"]:
                    raise ApprovalError(409, "execution_approval_expired")
                journal = {
                    "request_id": request_id,
                    "plan_hash": row["plan_hash"],
                    "status": "running",
                    "steps": [
                        {
                            "step_id": step.id,
                            "operation": step.operation,
                            "status": "not_started",
                            "attempts": 0,
                            "events": [],
                        }
                        for step in plan.ordered_steps()
                    ],
                }
            result = await self.drive(conn, row, plan, journal, subject)
            if plan.saga_version == 1 and result["status"] not in {
                "succeeded",
                "running",
                "retry_waiting",
            }:
                return await Saga(self).run(conn, plan, result, subject)
            return result

    async def save(self, conn, journal):
        await conn.execute(
            "INSERT INTO execution_runs(request_id,journal) VALUES (%s,%s) "
            "ON CONFLICT(request_id) DO UPDATE SET journal=EXCLUDED.journal,updated_at=now()",
            (journal["request_id"], Jsonb(journal)),
        )

    async def drive(self, conn, row, plan, journal, subject):
        results = {
            s["step_id"]: s["result"] for s in journal["steps"] if s["status"] == "succeeded"
        }
        for step, entry in zip(plan.ordered_steps(), journal["steps"], strict=True):
            if entry["status"] == "succeeded":
                continue
            try:
                args = {
                    k: results[v.step][v.field] if isinstance(v, Ref) else v
                    for k, v in step.arguments.items()
                }
                args = SCHEMAS[step.operation].model_validate(args).model_dump(mode="json")
            except (ValidationError, KeyError, TypeError):
                return await self.stop(
                    conn, journal, entry, "human_required", "invalid_tool_arguments"
                )
            key = idempotency_key(subject.tenant, row["id"], row["plan_hash"], step, args)
            if entry.get("key", key) != key:
                raise ApprovalError(409, "execution_arguments_changed")
            entry["key"] = key
            while entry["attempts"] < self.retry.max_attempts:
                if entry.get("next_attempt_at"):
                    remaining = (
                        datetime.fromisoformat(entry["next_attempt_at"]) - datetime.now(UTC)
                    ).total_seconds()
                    if remaining > self.retry.max_inline_delay:
                        journal["status"] = "retry_waiting"
                        await self.save(conn, journal)
                        return journal
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                actions = [step.operation]
                if args.get("permission") == "admin":
                    actions.append("admin.grant")
                for action in actions:
                    allowed = await self.approvals.policy.authorize(
                        PolicyQuery(
                            subject=subject,
                            action=action,
                            resource=PolicyResource(
                                type=step.operation.split(".")[0], id=args.get("repo_id")
                            ),
                        )
                    )
                    if not allowed.allow:
                        return await self.stop(
                            conn, journal, entry, "human_required", "execution_policy_denied"
                        )
                # Persist unknown outcome before sending: a crash can happen after remote commit.
                prior_unknown = entry.get("outcome_unknown", False)
                entry.update(status="running", attempts=entry["attempts"] + 1, outcome_unknown=True)
                entry.pop("next_attempt_at", None)
                journal["status"] = "running"
                event = {
                    "attempt": entry["attempts"],
                    "started_at": datetime.now(UTC).isoformat(),
                    "actor": subject.id,
                    "status": "started",
                }
                entry["events"].append(event)
                await self.save(conn, journal)
                async with span(
                    "tool.call",
                    {"step_id": step.id, "operation": step.operation, "attempt": entry["attempts"]},
                ) as frame:
                    result = await self.gateway.call(step, args, key)
                    frame.output = {
                        "status": result.status,
                        "code": label(result.code),
                        "replayed": result.replayed,
                        "outcome_unknown": result.outcome_unknown,
                        "item_count": len(result.items or []),
                    }
                    frame.status = "ok" if result.status == "succeeded" else "error"
                entry["outcome_unknown"] = prior_unknown or result.outcome_unknown
                event.update(status=result.status, code=result.code, replayed=result.replayed)
                if result.items is not None:
                    entry["items"] = result.items
                if result.status == "partial_success":
                    entry["outcome_unknown"] = False
                    return await self.stop(
                        conn, journal, entry, "partial_success", "batch_partial_failure"
                    )
                if result.status == "succeeded":
                    entry.update(
                        status="succeeded", result={"id": result.result_id}, outcome_unknown=False
                    )
                    entry.pop("code", None)
                    results[step.id] = entry["result"]
                    await self.save(conn, journal)
                    break
                if result.status != "retryable":
                    return await self.stop(conn, journal, entry, result.status, result.code)
                entry["code"] = result.code
                if entry["attempts"] < self.retry.max_attempts:
                    delay = self.retry.delay(entry["attempts"], result.retry_after)
                    event["delay_seconds"] = delay
                    entry.update(
                        status="retry_waiting",
                        next_attempt_at=(datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
                    )
                    await self.save(conn, journal)
            if entry["status"] != "succeeded":
                return await self.stop(
                    conn,
                    journal,
                    entry,
                    "retry_exhausted",
                    entry.get("code", "interrupted_attempt_budget_exhausted"),
                )
        journal["status"] = "succeeded"
        await self.save(conn, summarize(journal))
        return journal

    async def stop(self, conn, journal, entry, status, code):
        entry.update(status=status, code=code)
        journal.update(status=status, failed_step=entry["step_id"])
        journal["completed_steps"] = [
            s["step_id"] for s in journal["steps"] if s["status"] == "succeeded"
        ]
        journal["compensation_executed"] = False
        await self.save(conn, summarize(journal))
        return journal
