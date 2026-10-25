"""Postgres owns consent and progress; Redis contains disposable wake-up IDs only."""

import hashlib
import json
import secrets
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

from cryptography.fernet import InvalidToken

from core.graph.approval_service import ApprovalError
from core.policy.opa_client import PolicySubject
from core.tools.execution import ExecutionService
from core.tools.gateway import HttpToolGateway

ACTIVE = {"queued", "running", "waiting"}
CONTINUABLE = {"running", "retry_waiting", "compensating", "compensation_waiting"}
PUBLIC_FIELDS = (
    "request_id",
    "status",
    "due_at",
    "actor",
    "authorized_at",
    "expires_at",
    "failures",
    "runs",
    "last_error",
    "updated_at",
)


class BackgroundJobs:
    def __init__(
        self, approvals, settings_provider, *, gateway_factory=None, retry=None, runtime=None
    ):
        self.approvals = approvals
        self.settings_provider = settings_provider
        self.gateway_factory = gateway_factory or (
            lambda settings, authorization: HttpToolGateway(settings.mockcorp_url, authorization)
        )
        self.retry = retry
        self.runtime = runtime

    def subject_for(self, authorization):
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ApprovalError(401, "background_credentials_revoked")
        digest = hashlib.sha256(token.encode()).hexdigest()
        for identity in self.settings_provider().auth_credentials:
            if secrets.compare_digest(digest, identity.token_sha256):
                return PolicySubject(
                    id=identity.user_id, role=identity.role, tenant=identity.tenant
                )
        raise ApprovalError(401, "background_credentials_revoked")

    def guard(self, authorization, expected, expires_at):
        if datetime.now(UTC) >= expires_at:
            raise ApprovalError(403, "background_authorization_expired")
        if self.subject_for(authorization) != expected:
            raise ApprovalError(403, "background_identity_changed")

    def seal(self, authorization, subject, row, expires_at):
        # Use a purpose tag and all immutable bindings to prevent ciphertext substitution.
        payload = {
            "purpose": "background_execution_v1",
            "authorization": authorization,
            "subject": subject.model_dump(),
            "request_id": row["id"],
            "plan_hash": row["plan_hash"],
            "expires_at": expires_at.isoformat(),
        }
        return self.approvals.cipher.fernet.encrypt(json.dumps(payload).encode()).decode()

    def unseal(self, job):
        try:
            if self.approvals.cipher is None or not job["credential"]:
                raise ValueError("missing_ciphertext")
            data = json.loads(self.approvals.cipher.fernet.decrypt(job["credential"].encode()))
            subject = PolicySubject.model_validate(data["subject"])
            if (
                data["purpose"] != "background_execution_v1"
                or data["request_id"] != job["request_id"]
                or data["plan_hash"] != job["plan_hash"]
                or subject.id != job["actor"]
                or subject.tenant != job["tenant"]
                or subject.role != job["role"]
                or datetime.fromisoformat(data["expires_at"]) != job["expires_at"]
            ):
                raise ValueError("binding_mismatch")
            return data["authorization"], subject
        except (InvalidToken, ValueError, KeyError, TypeError) as exc:
            raise ApprovalError(403, "background_credential_invalid") from exc

    async def execute(self, request_id, subject, authorization):
        """Record fresh consent under the same lock before the first external effect."""
        async with self.approvals.session(request_id) as (conn, graph):
            settings = self.settings_provider()
            expires_at = datetime.now(UTC) + timedelta(
                hours=settings.background_authorization_hours
            )
            self.guard(authorization, subject, expires_at)
            gateway = self.gateway_factory(settings, authorization)
            executor = ExecutionService(
                self.approvals,
                gateway,
                self.retry,
                identity_guard=lambda: self.guard(authorization, subject, expires_at),
            )
            prepared = await executor.prepare(conn, graph, request_id, subject)
            row, plan, journal = prepared
            # Existing terminal/manual results never gain a new runnable background job.
            if not executor.can_continue(plan, journal):
                await self.finish(conn, request_id, journal)
                return journal
            credential = self.seal(authorization, subject, row, expires_at)
            await conn.execute(
                "INSERT INTO background_jobs(request_id,tenant,actor,role,plan_hash,credential,"
                "authorized_at,expires_at,status) VALUES (%s,%s,%s,%s,%s,%s,now(),%s,'queued') "
                "ON CONFLICT(request_id) DO UPDATE SET actor=EXCLUDED.actor,role=EXCLUDED.role,"
                "credential=EXCLUDED.credential,authorized_at=now(),expires_at=EXCLUDED.expires_at,"
                "status='queued',due_at=now(),failures=0,last_error=NULL,updated_at=now()",
                (
                    request_id,
                    subject.tenant,
                    subject.id,
                    subject.role,
                    row["plan_hash"],
                    credential,
                    expires_at,
                ),
            )
            # Autocommit intent above survives process death before any execution journal exists.
            try:
                result = await executor.run_prepared(conn, *prepared, subject)
                await self.finish(conn, request_id, result)
                return result
            except ApprovalError as exc:
                await self.failed(conn, request_id, exc)
                raise

    async def get(self, request_id, subject):
        async with self.approvals.session() as (conn, _):
            row = await self.approvals.row(conn, request_id, subject)
            await self.approvals.readable(row, subject)
            cursor = await conn.execute(
                "SELECT * FROM background_jobs WHERE request_id=%s AND tenant=%s",
                (request_id, subject.tenant),
            )
            job = await cursor.fetchone()
            return {"job": {key: job[key] for key in PUBLIC_FIELDS} if job else None}

    async def due(self, limit=100):
        async with self.approvals.connection() as conn:
            cursor = await conn.execute(
                "SELECT request_id FROM background_jobs WHERE status IN "
                "('queued','running','waiting') AND due_at<=now() ORDER BY due_at LIMIT %s",
                (limit,),
            )
            return [row["request_id"] for row in await cursor.fetchall()]

    async def run(self, request_id):
        """Duplicate wake-ups compete for the existing nonblocking approval advisory lock."""
        try:
            async with self.approvals.session(request_id) as (conn, graph):
                cursor = await conn.execute(
                    "SELECT * FROM background_jobs WHERE request_id=%s",
                    (request_id,),
                )
                job = await cursor.fetchone()
                if not job or job["status"] not in ACTIVE or job["due_at"] > datetime.now(UTC):
                    return
                try:
                    authorization, subject = self.unseal(job)
                    self.guard(authorization, subject, job["expires_at"])
                    executor = ExecutionService(
                        self.approvals,
                        self.gateway_factory(self.settings_provider(), authorization),
                        self.retry,
                        identity_guard=lambda: self.guard(
                            authorization, subject, job["expires_at"]
                        ),
                    )
                    prepared = await executor.prepare(conn, graph, request_id, subject)
                    if prepared[0]["plan_hash"] != job["plan_hash"]:
                        raise ApprovalError(409, "background_plan_changed")
                    # due_at is a bounded rescan delay, not an execution lease. The request
                    # advisory lock remains the sole exclusion mechanism, even past this time.
                    await conn.execute(
                        "UPDATE background_jobs SET status='running',runs=runs+1,"
                        "due_at=now()+interval '30 seconds',updated_at=now() WHERE request_id=%s",
                        (request_id,),
                    )
                    owner = subject.model_copy(update={"id": prepared[0]["requester"]})
                    observation = (
                        self.runtime.operation(owner, "execution.background", request_id)
                        if self.runtime is not None
                        else nullcontext(None)
                    )
                    async with observation as frame:
                        result = await executor.run_prepared(conn, *prepared, subject)
                        if frame is not None:
                            frame.output = {"status": result["status"]}
                            frame.status = "ok" if result["status"] == "succeeded" else "error"
                    await self.finish(conn, request_id, result)
                except ApprovalError as exc:
                    await self.failed(conn, request_id, exc)
                except Exception:
                    # No exception text: it may contain DSNs, headers or tool arguments.
                    await self.failed(conn, request_id, ApprovalError(503, "background_run_failed"))
        except ApprovalError as exc:
            if exc.detail not in {"approval_busy_retry", "approval_storage_unavailable"}:
                raise

    async def failed(self, conn, request_id, exc):
        cursor = await conn.execute(
            "SELECT failures FROM background_jobs WHERE request_id=%s",
            (request_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        failures = row["failures"] + 1
        transient = (
            exc.status == 503 and failures < self.settings_provider().background_max_failures
        )
        delay = min(300, 2 ** min(failures, 8))
        await conn.execute(
            "UPDATE background_jobs SET status=%s,failures=%s,last_error=%s,"
            "due_at=%s,credential=CASE WHEN %s THEN credential ELSE NULL END,updated_at=now() "
            "WHERE request_id=%s",
            (
                "waiting" if transient else "blocked",
                failures,
                exc.detail,
                datetime.now(UTC) + timedelta(seconds=delay),
                transient,
                request_id,
            ),
        )

    async def finish(self, conn, request_id, journal):
        continuing = journal["status"] in CONTINUABLE
        entries = (
            journal.get("saga", {}).get("steps", [])
            if journal["status"] in {"compensating", "compensation_waiting"}
            else journal["steps"]
        )
        deadlines = [
            datetime.fromisoformat(item["next_attempt_at"])
            for item in entries
            if item.get("next_attempt_at") and item["status"] != "succeeded"
        ]
        due = min(deadlines) if deadlines else datetime.now(UTC)
        manual = bool(journal.get("summary", {}).get("manual")) or journal["status"] not in {
            "succeeded",
            "compensated",
            "partial_success",
            *CONTINUABLE,
        }
        await conn.execute(
            "UPDATE background_jobs SET status=%s,due_at=%s,last_error=%s,"
            "credential=CASE WHEN %s THEN credential ELSE NULL END,updated_at=now() "
            "WHERE request_id=%s",
            (
                "waiting" if continuing else "blocked" if manual else "completed",
                due,
                None if continuing or not manual else "execution_requires_manual_resolution",
                continuing,
                request_id,
            ),
        )
