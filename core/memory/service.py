"""Personal preferences: explicit writes, deduplication, decay and scoped vector recall."""

import json
import math
from datetime import UTC, datetime, timedelta

from core.graph.approval_service import ApprovalError
from core.memory.models import LABELS, PreferenceWrite
from core.rag.embedder import HashingEmbedder

EMBEDDING_MODEL = "hashing-v1-128"


def importance(confirmed):
    return 0.9 if confirmed else 0.2


def decayed_score(weight, confirmed_at, now):
    age = max(0, (now - confirmed_at).total_seconds() / 86400)
    return weight * math.pow(0.5, age / 90)


class MemoryService:
    def __init__(self, approvals, clock=None):
        self.approvals = approvals
        self.clock = clock or (lambda: datetime.now(UTC))
        self.embedder = HashingEmbedder(dim=128)

    async def require(self, subject, action):
        await self.approvals.require(subject, action)

    async def put_preference(self, subject, body: PreferenceWrite):
        await self.require(subject, "memory.write")
        weight = importance(body.confirmed)
        if weight < 0.6:
            return {
                "status": "not_saved",
                "reason": "explicit_confirmation_required",
                "importance": weight,
            }
        async with self.approvals.session(
            f"preference:{subject.tenant}:{subject.id}:{body.key}"
        ) as (conn, _):
            row = await (
                await conn.execute(
                    "SELECT * FROM memory_preferences WHERE tenant=%s AND owner_id=%s AND key=%s",
                    (subject.tenant, subject.id, body.key),
                )
            ).fetchone()
            now = self.clock()
            if row and row["value"] == body.value and row["expires_at"] > now:
                return {"status": "unchanged", "memory": self.public(row, now)}
            version = row["version"] if row else 0
            if body.expected_version != version:
                raise ApprovalError(409, "memory_version_conflict")
            revision = await (
                await conn.execute(
                    "INSERT INTO memory_preference_versions VALUES (%s,%s,%s,1) "
                    "ON CONFLICT (tenant,owner_id,key) DO UPDATE "
                    "SET version=memory_preference_versions.version+1 RETURNING version",
                    (subject.tenant, subject.id, body.key),
                )
            ).fetchone()
            vector = self.embedder.embed([LABELS[body.key] + " " + body.value])[0]
            row = await (
                await conn.execute(
                    "INSERT INTO memory_preferences "
                    "(tenant,owner_id,key,value,version,importance,embedding,embedding_model,"
                    "confirmed_at,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s) "
                    "ON CONFLICT (tenant,owner_id,key) DO UPDATE SET value=EXCLUDED.value,"
                    "version=EXCLUDED.version,importance=EXCLUDED.importance,embedding=EXCLUDED.embedding,"
                    "embedding_model=EXCLUDED.embedding_model,confirmed_at=EXCLUDED.confirmed_at,"
                    "expires_at=EXCLUDED.expires_at RETURNING *",
                    (
                        subject.tenant,
                        subject.id,
                        body.key,
                        body.value,
                        revision["version"],
                        weight,
                        json.dumps(vector),
                        EMBEDDING_MODEL,
                        now,
                        now + timedelta(days=body.ttl_days),
                    ),
                )
            ).fetchone()
            return {"status": "saved", "memory": self.public(row, now)}

    @staticmethod
    def public(row, now):
        return {
            **{k: row[k] for k in ("key", "value", "version", "importance", "embedding_model")},
            "confirmed_at": row["confirmed_at"].isoformat(),
            "expires_at": row["expires_at"].isoformat(),
            "score": round(decayed_score(row["importance"], row["confirmed_at"], now), 6),
            "expired": row["expires_at"] <= now,
        }

    async def preferences(self, subject, query=None, include_expired=False):
        await self.require(subject, "memory.read")
        now = self.clock()
        vector = self.embedder.embed([query or "入职"])[0]
        async with self.approvals.session() as (conn, _):
            rows = await (
                await conn.execute(
                    "SELECT *, 1-(embedding <=> %s::vector) AS similarity "
                    "FROM memory_preferences WHERE tenant=%s AND owner_id=%s "
                    "AND embedding_model=%s ORDER BY embedding <=> %s::vector",
                    (
                        json.dumps(vector),
                        subject.tenant,
                        subject.id,
                        EMBEDDING_MODEL,
                        json.dumps(vector),
                    ),
                )
            ).fetchall()
        results = []
        for row in rows:
            public = self.public(row, now)
            if include_expired or (not public["expired"] and public["score"] >= 0.2):
                similarity = row["similarity"]
                similarity = similarity if math.isfinite(similarity) else 0
                public["relevance"] = round(max(0, similarity) * public["score"], 6)
                results.append(public)
        return sorted(results, key=lambda r: (-r["relevance"], r["key"]))

    async def forget_preference(self, subject, key, expected_version):
        await self.require(subject, "memory.write")
        async with self.approvals.session(f"preference:{subject.tenant}:{subject.id}:{key}") as (
            conn,
            _,
        ):
            deleted = await (
                await conn.execute(
                    "DELETE FROM memory_preferences WHERE tenant=%s AND owner_id=%s "
                    "AND key=%s AND version=%s RETURNING key",
                    (subject.tenant, subject.id, key, expected_version),
                )
            ).fetchone()
            if not deleted:
                raise ApprovalError(409, "memory_missing_or_version_conflict")
        return {"status": "forgotten", "key": key}

    async def history(self, subject, department_id=None):
        await self.require(subject, "organization.read" if department_id else "memory.read")
        async with self.approvals.session() as (conn, _):
            rows = await (
                await conn.execute(
                    "SELECT a.id,a.requester,a.snapshot,a.decision,e.journal "
                    "FROM approval_requests a "
                    "LEFT JOIN execution_runs e ON a.id=e.request_id "
                    "WHERE a.tenant=%s AND "
                    "(CASE WHEN %s::text IS NULL THEN a.requester=%s "
                    "ELSE a.snapshot->'facts'->>'department_id'=%s END) "
                    "AND a.decision IS NOT NULL ORDER BY a.created_at DESC LIMIT 20",
                    (subject.tenant, department_id, subject.id, department_id),
                )
            ).fetchall()
        # History is evidence, never an instruction or a new source of default permissions.
        return [
            {
                "request_id": row["id"],
                "decision": row["decision"]["decision"],
                "execution_status": (row["journal"] or {}).get("status", "not_started"),
                "counts": {
                    k: len((row["journal"] or {}).get("summary", {}).get(k, []))
                    for k in ("retained", "compensated", "failed", "manual")
                },
            }
            for row in rows
        ]
