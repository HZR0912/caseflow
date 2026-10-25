"""Receipt-backed manual resolution, separate from the immutable execution journal."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import httpx
from apps.mockcorp.preview import SCHEMAS
from apps.mockcorp.reconciliation import EvidenceTarget, ReceiptEvidence
from psycopg.types.json import Jsonb
from pydantic import Field, ValidationError

from core.graph.approval_service import ApprovalError, digest
from core.graph.contracts import Ref, StrictModel
from core.graph.workflow import Redactor
from core.tools.execution import idempotency_key

ACTIVE = {"running", "retry_waiting", "compensating", "compensation_waiting"}
EXCEPTIONS = {
    "human_required",
    "retry_exhausted",
    "compensation_required",
    "compensation_failed",
    "partial_success",
    "failed",
}


class ResolutionDecision(StrictModel):
    evidence_id: UUID
    journal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["confirm_compensated", "accept_observed_state", "keep_open"]
    reason: str = Field(min_length=1, max_length=1000)


class HttpEvidenceGateway:
    def __init__(self, base_url, bearer, client=None):
        self.base_url, self.bearer, self.client = base_url.rstrip("/"), bearer, client

    async def inspect(self, tenant, targets):
        async def send(client):
            try:
                response = await client.post(
                    self.base_url + "/v1/reconciliation/evidence",
                    json={"targets": [t.model_dump() for t in targets]},
                    headers={"Authorization": self.bearer},
                    timeout=10,
                    follow_redirects=False,
                )
                response.raise_for_status()
                body = response.json()
                items = [ReceiptEvidence.model_validate(i).model_dump() for i in body["items"]]
                if body["tenant"] != tenant or len(items) != len(targets):
                    raise ValueError("scope_or_count_mismatch")
                for target, item in zip(targets, items, strict=True):
                    if any(item[k] != v for k, v in target.model_dump().items()):
                        raise ValueError("target_mismatch")
                return items
            except httpx.HTTPError as exc:
                raise ApprovalError(503, "reconciliation_supplier_unavailable") from exc
            except (ValueError, KeyError, TypeError, ValidationError) as exc:
                raise ApprovalError(503, "reconciliation_invalid_supplier_evidence") from exc

        if not targets:
            return []
        if self.client is not None:
            return await send(self.client)
        async with httpx.AsyncClient() as client:
            return await send(client)


class ReconciliationService:
    def __init__(self, approvals, gateway=None):
        self.approvals, self.gateway = approvals, gateway

    async def context(self, conn, request_id, subject, action=None):
        row = await self.approvals.row(conn, request_id, subject)
        await self.approvals.readable(row, subject)
        if action:
            await self.approvals.require(subject, action, request_id)
        cursor = await conn.execute(
            "SELECT journal FROM execution_runs WHERE request_id=%s", (request_id,)
        )
        saved = await cursor.fetchone()
        return saved["journal"] if saved else None

    @staticmethod
    def require_exception(journal):
        if journal and journal["status"] in ACTIVE:
            raise ApprovalError(409, "reconciliation_execution_active")
        if not journal or journal["status"] not in EXCEPTIONS:
            raise ApprovalError(409, "reconciliation_not_required")

    async def view(self, conn, request_id, journal):
        cursor = await conn.execute(
            "SELECT * FROM reconciliation_evidence WHERE request_id=%s "
            "ORDER BY observed_at DESC,id DESC LIMIT 1",
            (request_id,),
        )
        evidence = await cursor.fetchone()
        if evidence:
            evidence = {k: v for k, v in evidence.items() if k != "request_id"}
            evidence.update(evidence.pop("payload"))
        cursor = await conn.execute(
            "SELECT * FROM reconciliation_resolutions WHERE request_id=%s", (request_id,)
        )
        resolution = await cursor.fetchone()
        cursor = await conn.execute(
            "SELECT id,actor,event,created_at,payload,previous_hash,event_hash "
            "FROM reconciliation_events WHERE request_id=%s ORDER BY id DESC LIMIT 100",
            (request_id,),
        )
        return {
            "status": "closed"
            if resolution
            else "open"
            if journal and journal["status"] in EXCEPTIONS
            else "not_required",
            "latest_evidence": evidence,
            "resolution": resolution,
            "events": await cursor.fetchall(),
        }

    async def get(self, request_id, subject):
        async with self.approvals.session() as (conn, _):
            journal = await self.context(conn, request_id, subject)
            return await self.view(conn, request_id, journal)

    async def targets(self, conn, request_id, subject, journal):
        row = await self.approvals.row(conn, request_id, subject)
        try:
            if digest(row["snapshot"]) != row["plan_hash"]:
                raise ValueError("snapshot_changed")
            plan = self.approvals.cipher.open(row["execution_payload"], subject.tenant, request_id)
            if (
                digest(plan.model_dump(mode="json", exclude_unset=True))
                != row["snapshot"]["execution_plan_hash"]
            ):
                raise ValueError("plan_changed")
            saved = {step["step_id"]: step for step in journal["steps"]}
            result = []
            for step in plan.ordered_steps():
                entry = saved[step.id]
                if not entry.get("key"):
                    continue
                args = {
                    k: saved[v.step]["result"][v.field] if isinstance(v, Ref) else v
                    for k, v in step.arguments.items()
                }
                args = SCHEMAS[step.operation].model_validate(args).model_dump(mode="json")
                if (
                    entry["key"]
                    != idempotency_key(subject.tenant, request_id, row["plan_hash"], step, args)
                    or entry["operation"] != step.operation
                ):
                    raise ValueError("arguments_changed")
                result.append(
                    EvidenceTarget(
                        step_id=step.id,
                        operation=step.operation,
                        key=entry["key"],
                        fingerprint=digest({"operation": step.operation, "payload": args}),
                    )
                )
            return result
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ApprovalError(409, "reconciliation_execution_plan_changed") from exc

    async def collect(self, journal, tenant, targets):
        observed = {item["step_id"]: item for item in await self.gateway.inspect(tenant, targets)}
        undo = {s["step_id"]: s for s in journal.get("saga", {}).get("steps", [])}
        items = []
        for step in journal["steps"]:
            sid = step["step_id"]
            item = dict(
                observed.get(
                    sid,
                    {
                        "step_id": sid,
                        "operation": step["operation"],
                        "forward": "missing",
                        "compensation": "missing",
                        "state": "unconfirmed",
                        "receipt_hash": digest(None),
                        "state_hash": digest(None),
                    },
                )
            )
            # A missing receipt NEVER resolves an unknown write: an HTTP request
            # can still arrive after this observation, even with supplier row locks.
            if (
                item["forward"] == "missing"
                and item["compensation"] == "missing"
                and not step.get("outcome_unknown")
                and not undo.get(sid, {}).get("outcome_unknown")
                and step["status"] not in {"succeeded", "partial_success"}
            ):
                item["state"] = (
                    "not_started" if step["status"] == "not_started" else "not_committed"
                )
            if undo.get(sid, {}).get("outcome_unknown") and item["compensation"] != "committed":
                item["state"] = "unconfirmed"
            if step.get("outcome_unknown") and item["forward"] != "committed":
                item["state"] = "unconfirmed"
            item["needs_attention"] = item["state"] in {"changed", "unconfirmed"}
            # Keys are supplier lookup secrets, not useful display data.
            item.pop("key", None)
            item.pop("fingerprint", None)
            items.append(item)
        # `changed` is a real, supplier-observed state and may be explicitly
        # accepted by an authorized operator. `unconfirmed` remains impossible
        # to close because a missing receipt can still arrive after observation.
        clear = not any(i["state"] == "unconfirmed" for i in items)
        return {
            "items": items,
            "can_confirm_compensated": bool(undo)
            and clear
            and all(
                next(i for i in items if i["step_id"] == sid)["state"] == "compensated"
                for sid in undo
            ),
            "can_accept_observed_state": clear,
        }

    async def audit(self, conn, request_id, subject, event, payload, when):
        cursor = await conn.execute(
            "SELECT event_hash FROM reconciliation_events WHERE request_id=%s "
            "ORDER BY id DESC LIMIT 1",
            (request_id,),
        )
        previous = await cursor.fetchone()
        previous_hash = previous["event_hash"] if previous else None
        event_hash = digest(
            [request_id, subject.id, event, when.isoformat(), payload, previous_hash]
        )
        await conn.execute(
            "INSERT INTO reconciliation_events"
            "(request_id,actor,event,created_at,payload,previous_hash,event_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (request_id, subject.id, event, when, Jsonb(payload), previous_hash, event_hash),
        )

    async def inspect(self, request_id, subject):
        async with self.approvals.session(request_id) as (conn, _):
            journal = await self.context(conn, request_id, subject, "approval.reconcile")
            self.require_exception(journal)
            view = await self.view(conn, request_id, journal)
            if view["resolution"]:
                raise ApprovalError(409, "reconciliation_already_closed")
            targets = await self.targets(conn, request_id, subject, journal)
            payload = await self.collect(journal, subject.tenant, targets)
            when, eid = datetime.now(UTC), str(uuid4())
            evidence_hash, journal_hash = digest(payload), digest(journal)
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO reconciliation_evidence"
                    "(id,request_id,actor,journal_hash,evidence_hash,"
                    "observed_at,expires_at,payload) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        eid,
                        request_id,
                        subject.id,
                        journal_hash,
                        evidence_hash,
                        when,
                        when + timedelta(minutes=10),
                        Jsonb(payload),
                    ),
                )
                await self.audit(
                    conn,
                    request_id,
                    subject,
                    "inspected",
                    {
                        "evidence_id": eid,
                        "evidence_hash": evidence_hash,
                        "journal_hash": journal_hash,
                    },
                    when,
                )
            return await self.view(conn, request_id, journal)

    async def resolve(self, request_id, choice, subject):
        async with self.approvals.session(request_id) as (conn, _):
            journal = await self.context(conn, request_id, subject, "approval.resolve")
            self.require_exception(journal)
            view = await self.view(conn, request_id, journal)
            reason = Redactor(choice.reason).clean(choice.reason).strip()
            if not reason:
                raise ApprovalError(422, "reconciliation_reason_required")
            if view["resolution"]:
                old = view["resolution"]
                if (
                    old["evidence_id"] == str(choice.evidence_id)
                    and old["decision"] == choice.decision
                    and old["reason"] == reason
                    and old["actor"] == subject.id
                ):
                    return view
                raise ApprovalError(409, "reconciliation_already_closed")
            evidence = view["latest_evidence"]
            if (
                not evidence
                or evidence["id"] != str(choice.evidence_id)
                or evidence["journal_hash"] != choice.journal_hash
                or digest(journal) != choice.journal_hash
            ):
                raise ApprovalError(409, "reconciliation_stale_evidence")
            if datetime.now(UTC) >= evidence["expires_at"]:
                raise ApprovalError(409, "reconciliation_evidence_expired")
            targets = await self.targets(conn, request_id, subject, journal)
            fresh = await self.collect(journal, subject.tenant, targets)
            if digest(fresh) != evidence["evidence_hash"]:
                raise ApprovalError(409, "reconciliation_evidence_changed")
            if choice.decision == "confirm_compensated" and not fresh["can_confirm_compensated"]:
                raise ApprovalError(409, "reconciliation_compensation_not_confirmed")
            if (
                choice.decision == "accept_observed_state"
                and not fresh["can_accept_observed_state"]
            ):
                raise ApprovalError(409, "reconciliation_unconfirmed_outcome")
            when = datetime.now(UTC)
            payload = {
                "evidence_id": evidence["id"],
                "decision": choice.decision,
                "reason": reason,
                "journal_hash": choice.journal_hash,
                "evidence_hash": evidence["evidence_hash"],
            }
            async with conn.transaction():
                if choice.decision != "keep_open":
                    await conn.execute(
                        "INSERT INTO reconciliation_resolutions"
                        "(request_id,evidence_id,decision,reason,actor,resolved_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (request_id, evidence["id"], choice.decision, reason, subject.id, when),
                    )
                    await conn.execute(
                        "UPDATE background_jobs SET status='completed',credential=NULL,"
                        "last_error='manual_case_closed',updated_at=now() WHERE request_id=%s",
                        (request_id,),
                    )
                await self.audit(
                    conn,
                    request_id,
                    subject,
                    "kept_open" if choice.decision == "keep_open" else "closed",
                    payload,
                    when,
                )
            return await self.view(conn, request_id, journal)
