"""Best-effort trace recording. Replay queries fail explicitly; business state is independent."""

import logging
import time
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)


class TraceStore:
    def __init__(self, url):
        self.url = url.replace("postgresql+psycopg://", "postgresql://")
        self.write_failures = 0
        self.retry_at = 0.0

    @asynccontextmanager
    async def connection(self):
        async with await psycopg.AsyncConnection.connect(
            self.url,
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=2,
            options="-c statement_timeout=2000 -c lock_timeout=1000",
        ) as conn:
            yield conn

    async def write(self, query, params):
        if time.monotonic() < self.retry_at:
            return False
        try:
            async with self.connection() as conn:
                await conn.execute(query, params)
            return True
        except psycopg.Error:
            self.write_failures += 1
            self.retry_at = time.monotonic() + 30
            log.warning("observation_write_failed")
            return False

    async def ensure(self, trace_id, subject, request_id, root):
        return await self.write(
            "INSERT INTO observation_traces(trace_id,tenant,owner_id,request_id,root_span_id) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(trace_id) DO NOTHING",
            (trace_id, subject.tenant, subject.id, request_id, root),
        )

    async def begin(self, trace_id, frame):
        return await self.write(
            "INSERT INTO observation_spans"
            "(trace_id,span_id,parent_span_id,name,started_at,input,attributes) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                trace_id,
                frame["span_id"],
                frame["parent_span_id"],
                frame["name"],
                frame["started_at"],
                Jsonb(frame["input"]),
                Jsonb(frame["attributes"]),
            ),
        )

    async def end(self, trace_id, frame):
        return await self.write(
            "UPDATE observation_spans SET ended_at=%s,duration_ms=%s,status=%s,"
            "output=%s,attributes=%s "
            "WHERE trace_id=%s AND span_id=%s",
            (
                frame["ended_at"],
                frame["duration_ms"],
                frame["status"],
                Jsonb(frame["output"]),
                Jsonb(frame["attributes"]),
                trace_id,
                frame["span_id"],
            ),
        )

    async def list(self, subject, all_in_tenant=False, request_id=None):
        from core.graph.approval_service import ApprovalError

        try:
            async with self.connection() as conn:
                return await (
                    await conn.execute(
                        "SELECT t.*,count(s.span_id) AS span_count,"
                        "max(s.ended_at) AS last_finished_at "
                        "FROM observation_traces t LEFT JOIN observation_spans s USING(trace_id) "
                        "WHERE t.tenant=%s AND (%s OR t.owner_id=%s) "
                        "AND (%s::text IS NULL OR t.request_id=%s) GROUP BY t.trace_id "
                        "ORDER BY t.created_at DESC,t.trace_id LIMIT 100",
                        (subject.tenant, all_in_tenant, subject.id, request_id, request_id),
                    )
                ).fetchall()
        except psycopg.Error as exc:
            raise ApprovalError(503, "trace_storage_unavailable") from exc

    async def get(self, subject, trace_id, all_in_tenant=False):
        from core.graph.approval_service import ApprovalError

        try:
            async with self.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT * FROM observation_traces WHERE trace_id=%s AND tenant=%s "
                        "AND (%s OR owner_id=%s)",
                        (trace_id, subject.tenant, all_in_tenant, subject.id),
                    )
                ).fetchone()
                if not row:
                    raise ApprovalError(404, "trace_not_found")
                frames = await (
                    await conn.execute(
                        "SELECT * FROM observation_spans WHERE trace_id=%s "
                        "ORDER BY started_at,span_id LIMIT 2001",
                        (trace_id,),
                    )
                ).fetchall()
                return {**row, "frames": frames[:2000], "truncated": len(frames) > 2000}
        except psycopg.Error as exc:
            raise ApprovalError(503, "trace_storage_unavailable") from exc
