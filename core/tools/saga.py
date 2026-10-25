"""Persisted group-scoped Saga. Never infer a rollback from an unknown outcome."""

import asyncio
from datetime import UTC, datetime, timedelta

from core.obs.privacy import label
from core.obs.runtime import span
from core.policy.opa_client import PolicyQuery, PolicyResource
from core.tools.compensation import UNDO_ACTIONS, compensation_key


def summarize(journal):
    undo = {i["step_id"]: i for i in journal.get("saga", {}).get("steps", [])}
    summary = {
        key: []
        for key in (
            "completed",
            "retained",
            "compensated",
            "failed",
            "not_started",
            "unknown",
            "compensation_unknown",
            "manual",
        )
    }
    for step in journal["steps"]:
        sid = step["step_id"]
        if step["status"] in {"succeeded", "partial_success"}:
            summary["completed"].append(sid)
            summary[
                "compensated" if undo.get(sid, {}).get("status") == "succeeded" else "retained"
            ].append(sid)
        if step["status"] == "not_started":
            summary["not_started"].append(sid)
        elif step["status"] != "succeeded":
            summary["failed"].append(sid)
        if step.get("outcome_unknown"):
            summary["unknown"].append(sid)
        if undo.get(sid, {}).get("outcome_unknown"):
            summary["compensation_unknown"].append(sid)
        if step.get("outcome_unknown") or (
            sid in undo
            and (
                undo[sid]["status"] in {"blocked", "failed"}
                or (
                    journal.get("saga", {}).get("status") == "compensation_failed"
                    and undo[sid]["status"] != "succeeded"
                )
            )
        ):
            summary["manual"].append(sid)
    journal["summary"] = summary
    journal["compensation_executed"] = any(i["attempts"] for i in undo.values())
    return journal


class Saga:
    def __init__(self, executor):
        self.executor = executor

    async def run(self, conn, plan, journal, subject):
        executor = self.executor
        by_id = {s.id: s for s in plan.steps}
        if "saga" not in journal:
            journal["forward_status"] = journal["status"]
            # Unknown commits must be reconciled manually, not replayed with new keys or undone.
            if any(s.get("outcome_unknown") for s in journal["steps"]):
                journal["saga"] = {"status": "blocked_unknown_outcome", "steps": []}
                summarize(journal)
                await executor.save(conn, journal)
                return journal
            failed = by_id[journal["failed_step"]]
            completed = [
                e for e in journal["steps"] if e["status"] in {"succeeded", "partial_success"}
            ]
            candidates = {
                e["step_id"]
                for e in completed
                if failed.saga_group is not None
                and by_id[e["step_id"]].saga_group == failed.saga_group
            }
            # An irreversible or out-of-scope retained consumer protects its ancestors.
            retained = {
                e["step_id"]
                for e in completed
                if e["step_id"] not in candidates
                or by_id[e["step_id"]].operation not in UNDO_ACTIONS
                or by_id[e["step_id"]].reversibility == "irreversible"
            }
            protected = set()

            def protect(sid):
                for dep in by_id[sid].depends_on:
                    if dep not in protected:
                        protected.add(dep)
                        protect(dep)

            for sid in retained:
                protect(sid)
            items = []
            for entry in reversed(completed):
                sid = entry["step_id"]
                if sid not in candidates:
                    continue
                reason = (
                    "retained_dependency"
                    if sid in protected
                    else "irreversible_operation"
                    if sid in retained
                    else None
                )
                items.append(
                    {
                        "step_id": sid,
                        "operation": entry["operation"],
                        "source_key": entry["key"],
                        "key": compensation_key(entry["key"]),
                        "status": "blocked" if reason else "pending",
                        "code": reason,
                        "attempts": 0,
                        "events": [],
                    }
                )
            journal["saga"] = {
                "version": 1,
                "group": failed.saga_group,
                "status": "compensating",
                "steps": items,
            }
            journal["status"] = "compensating"
            await executor.save(conn, summarize(journal))
        saga = journal["saga"]
        if saga["status"] not in {"compensating", "compensation_waiting"}:
            return journal
        for item in saga["steps"]:
            if item["status"] in {"succeeded", "blocked"}:
                continue
            while item["attempts"] < executor.retry.max_attempts:
                if item.get("next_attempt_at"):
                    remaining = (
                        datetime.fromisoformat(item["next_attempt_at"]) - datetime.now(UTC)
                    ).total_seconds()
                    if remaining > executor.retry.max_inline_delay:
                        journal["status"] = saga["status"] = "compensation_waiting"
                        await executor.save(conn, summarize(journal))
                        return journal
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                step = by_id[item["step_id"]]
                decision = await executor.approvals.policy.authorize(
                    PolicyQuery(
                        subject=subject,
                        action=UNDO_ACTIONS[item["operation"]],
                        resource=PolicyResource(
                            type=step.operation.split(".")[0], id=step.arguments.get("repo_id")
                        ),
                    )
                )
                if not decision.allow:
                    return await self.fail(conn, journal, item, "compensation_policy_denied")
                prior_unknown = item.get("outcome_unknown", False)
                item.update(status="running", attempts=item["attempts"] + 1, outcome_unknown=True)
                item.pop("next_attempt_at", None)
                journal["status"] = saga["status"] = "compensating"
                event = {
                    "attempt": item["attempts"],
                    "actor": subject.id,
                    "started_at": datetime.now(UTC).isoformat(),
                    "status": "started",
                }
                item["events"].append(event)
                await executor.save(conn, summarize(journal))
                async with span(
                    "saga.compensate",
                    {
                        "step_id": item["step_id"],
                        "operation": item["operation"],
                        "attempt": item["attempts"],
                    },
                ) as frame:
                    result = await executor.gateway.compensate(
                        item["operation"], item["source_key"], item["key"]
                    )
                    frame.output = {
                        "status": result.status,
                        "code": label(result.code),
                        "replayed": result.replayed,
                        "outcome_unknown": result.outcome_unknown,
                    }
                    frame.status = "ok" if result.status == "succeeded" else "error"
                item["outcome_unknown"] = prior_unknown or result.outcome_unknown
                event.update(status=result.status, code=result.code, replayed=result.replayed)
                if result.status == "succeeded":
                    item.update(status="succeeded", outcome_unknown=False, code=None)
                    await executor.save(conn, summarize(journal))
                    break
                if result.status != "retryable":
                    return await self.fail(conn, journal, item, result.code)
                if item["attempts"] < executor.retry.max_attempts:
                    delay = executor.retry.delay(item["attempts"], result.retry_after)
                    event["delay_seconds"] = delay
                    item.update(
                        status="retry_waiting",
                        next_attempt_at=(datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
                    )
                    await executor.save(conn, summarize(journal))
            if item["status"] != "succeeded":
                return await self.fail(conn, journal, item, "compensation_retry_exhausted")
        summarize(journal)
        saga["status"] = "completed" if not journal["summary"]["manual"] else "manual_required"
        journal["status"] = (
            "compensation_failed"
            if journal["summary"]["manual"]
            else "partial_success"
            if journal["summary"]["retained"]
            else "compensated"
            if journal["summary"]["compensated"]
            else "failed"
        )
        await executor.save(conn, journal)
        return journal

    async def fail(self, conn, journal, item, code):
        item.update(status="failed", code=code)
        journal["status"] = journal["saga"]["status"] = "compensation_failed"
        await self.executor.save(conn, summarize(journal))
        return journal
