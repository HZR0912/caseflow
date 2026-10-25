"""Explicit OTel spans with durable start/end frames and safe Langfuse OTLP export."""

import asyncio
import base64
import hashlib
import inspect
import json
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import uuid4

from langchain_core.runnables import RunnableLambda
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags

from core.obs.privacy import projection
from core.obs.store import TraceStore

active = ContextVar("caseflow_observation", default=None)


def identity(tenant, request_id):
    return hashlib.sha256(json.dumps([tenant, request_id]).encode()).hexdigest()[:32]


class Frame:
    def __init__(self, input=None, attributes=None):
        self.input = input or {}
        self.output = {}
        self.attributes = attributes or {}
        self.status = "ok"


class Runtime:
    def __init__(self, settings, store=None, exporter=None):
        self.enabled = settings.observation_enabled
        self.store = store or TraceStore(settings.database_url)
        self.provider = TracerProvider(resource=Resource.create({"service.name": "caseflow-api"}))
        if self.enabled:
            if exporter:
                self.provider.add_span_processor(
                    BatchSpanProcessor(exporter, schedule_delay_millis=200)
                )
            if settings.otel_exporter_otlp_traces_endpoint:
                self.provider.add_span_processor(
                    BatchSpanProcessor(
                        OTLPSpanExporter(
                            endpoint=settings.otel_exporter_otlp_traces_endpoint, timeout=2
                        ),
                        max_queue_size=2048,
                        schedule_delay_millis=500,
                    )
                )
            if (
                settings.langfuse_public_key.get_secret_value()
                and settings.langfuse_secret_key.get_secret_value()
            ):
                auth = base64.b64encode(
                    (
                        settings.langfuse_public_key.get_secret_value()
                        + ":"
                        + settings.langfuse_secret_key.get_secret_value()
                    ).encode()
                ).decode()
                self.provider.add_span_processor(
                    BatchSpanProcessor(
                        OTLPSpanExporter(
                            endpoint=settings.langfuse_base_url.rstrip("/")
                            + "/api/public/otel/v1/traces",
                            headers={"Authorization": "Basic " + auth},
                            timeout=2,
                        ),
                        max_queue_size=2048,
                        schedule_delay_millis=500,
                    )
                )
        self.tracer = self.provider.get_tracer("caseflow.explicit", "1")

    async def close(self):
        await asyncio.to_thread(self.provider.shutdown)

    @asynccontextmanager
    async def operation(self, subject, name, request_id=None):
        if not self.enabled:
            yield None
            return
        trace_id = identity(subject.tenant, request_id) if request_id else uuid4().hex
        # A stable synthetic lifecycle parent joins phases across restarts without a days-long span.
        root = hashlib.sha256((trace_id + "lifecycle").encode()).hexdigest()[:16]
        parent = SpanContext(int(trace_id, 16), int(root, 16), True, TraceFlags(1))
        ctx = trace.set_span_in_context(NonRecordingSpan(parent), Context())
        await self.store.ensure(trace_id, subject, request_id, root)
        token = active.set((self, trace_id))
        try:
            async with span(name, context=ctx) as frame:
                yield frame
        finally:
            active.reset(token)


@asynccontextmanager
async def span(name, input=None, attributes=None, context=None):
    frame = Frame(input, attributes)
    current = active.get()
    if current is None:
        yield frame
        return
    runtime, trace_id = current
    started = datetime.now(UTC)
    tick = time.perf_counter()
    with runtime.tracer.start_as_current_span(
        name,
        context=context,
        record_exception=False,
        set_status_on_exception=False,
    ) as otel:
        sid = format(otel.get_span_context().span_id, "016x")
        parent = format(otel.parent.span_id, "016x") if otel.parent else None
        record = {
            "span_id": sid,
            "parent_span_id": parent,
            "name": name,
            "started_at": started,
            "input": frame.input,
            "attributes": frame.attributes,
        }
        await runtime.store.begin(trace_id, record)
        try:
            yield frame
        except BaseException as exc:
            frame.status = "waiting" if type(exc).__name__ == "GraphInterrupt" else "error"
            frame.output = {"error_type": type(exc).__name__}
            raise
        finally:
            ended = datetime.now(UTC)
            record.update(
                ended_at=ended,
                duration_ms=round((time.perf_counter() - tick) * 1000, 3),
                status=frame.status,
                output=frame.output,
                attributes=frame.attributes,
            )
            await runtime.store.end(trace_id, record)
            otel.set_attribute(
                "langfuse.observation.type", "generation" if name == "model.call" else "span"
            )
            otel.set_attribute(
                "langfuse.observation.input", json.dumps(frame.input, ensure_ascii=False)
            )
            otel.set_attribute(
                "langfuse.observation.output", json.dumps(frame.output, ensure_ascii=False)
            )
            otel.set_attribute("caseflow.status", frame.status)
            for key, value in frame.attributes.items():
                if value is not None:
                    otel.set_attribute(key, value)
            if frame.status == "error":
                otel.set_status(Status(StatusCode.ERROR, "operation_failed"))


def node(name, function):
    async def traced(state):
        async with span("graph." + name, projection(state)) as frame:
            value = function(state)
            result = await value if inspect.isawaitable(value) else value
            frame.output = projection(result)
            if result.get("status") in {"denied", "blocked", "preview_unavailable"}:
                frame.status = "error"
            return result

    if not inspect.iscoroutinefunction(function):
        return RunnableLambda(function, afunc=traced)
    return traced


def headers():
    if active.get() is None:
        return {}
    ctx = trace.get_current_span().get_span_context()
    return {"traceparent": f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-01"}
