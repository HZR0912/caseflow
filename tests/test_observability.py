import copy
import json
from datetime import UTC, datetime

import httpx
import pytest
from apps.api.settings import PlanningProvider, Settings
from core.obs.metrics import metrics
from core.obs.model import completion
from core.obs.privacy import projection
from core.obs.runtime import Runtime, headers, identity, span
from core.obs.store import TraceStore
from core.policy.opa_client import PolicySubject
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class MemoryTraceStore:
    def __init__(self):
        self.frames = []
        self.traces = []

    async def ensure(self, *args):
        self.traces.append(args)

    async def begin(self, tid, frame):
        self.frames.append(copy.deepcopy({**frame, "trace_id": tid}))

    async def end(self, tid, frame):
        next(f for f in self.frames if f["span_id"] == frame["span_id"]).update(
            copy.deepcopy(frame)
        )


@pytest.mark.asyncio
async def test_nested_trace_context_privacy_error_and_restart_identity():
    store, exporter = MemoryTraceStore(), InMemorySpanExporter()
    settings = Settings(_env_file=None, observation_enabled=True)
    owner = PolicySubject(id="owner", role="admin", tenant="tenant")
    runtime = Runtime(settings, store, exporter)
    async with runtime.operation(owner, "submit", "request"):
        async with span("tool.call", {"operation": "identity.create_user"}):
            assert headers()["traceparent"].split("-")[1] == identity("tenant", "request")
            assert len(store.frames) == 2 and "ended_at" not in store.frames[-1]
        with pytest.raises(ValueError):
            async with span("failed"):
                raise ValueError("secret email alice@example.com")
    runtime.provider.force_flush()
    exported = exporter.get_finished_spans()
    assert len(exported) == 3
    assert exported[0].parent.span_id == exported[-1].context.span_id
    assert "alice@example.com" not in json.dumps([dict(s.attributes) for s in exported])
    assert not exported[1].events
    assert headers() == {}
    await runtime.close()
    new = Runtime(settings, store)
    async with new.operation(owner, "execute", "request"):
        pass
    await new.close()
    assert len({f["trace_id"] for f in store.frames}) == 1
    assert identity("other", "request") != identity("tenant", "request")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage,rates,known,cost",
    [
        ({"prompt_tokens": 100, "completion_tokens": 20}, (2, 4), True, 0.00028),
        ({"prompt_tokens": 100, "completion_tokens": 20}, (None, None), True, None),
        ({}, (2, 4), False, None),
        ({"prompt_tokens": -1, "completion_tokens": True}, (2, 4), False, None),
    ],
)
async def test_real_response_usage_and_unknown_cost(usage, rates, known, cost):
    store = MemoryTraceStore()
    runtime = Runtime(Settings(_env_file=None, observation_enabled=True), store)
    provider = PlanningProvider(
        name="fixture",
        model="fixture-model",
        base_url="http://model",
        api_key="secret",
        input_usd_per_million=rates[0],
        output_usd_per_million=rates[1],
    )

    def respond(req):
        return httpx.Response(
            200, json={"usage": usage, "choices": [{"message": {"content": "sensitive response"}}]}
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client,
        runtime.operation(PolicySubject(id="u", role="admin"), "plan"),
    ):
        assert await completion(client, provider, url="http://model") == "sensitive response"
    frame = next(f for f in store.frames if f["name"] == "model.call")
    assert frame["output"]["usage_known"] is known
    assert frame["output"]["cost_usd"] == cost
    assert "sensitive response" not in str(store.frames)
    await runtime.close()


def test_safe_projection_excludes_prompt_employee_arguments_and_quotes():
    result = projection(
        {
            "masked_text": "secret",
            "facts": {"employees": [{"name": "secret"}]},
            "evidence": [
                {"chunk_id": "chunk-1", "quote": "secret", "char_start": 1, "char_end": 5}
            ],
            "plan": {
                "steps": [
                    {"id": "p1", "operation": "identity.create_user", "args": {"name": "secret"}}
                ]
            },
        }
    )
    assert "secret" not in json.dumps(result)
    assert result["employee_count"] == 1 and result["evidence"][0]["char_start"] == 1


def test_metrics_no_double_count_unknown_and_incomplete():
    now = datetime.now(UTC)
    root = {
        "name": "execute",
        "status": "ok",
        "parent_span_id": "root",
        "duration_ms": 100,
        "started_at": now,
        "ended_at": now,
        "output": {},
    }
    child = {**root, "name": "model.call", "parent_span_id": "phase", "duration_ms": 80}
    result = metrics({"root_span_id": "root", "frames": [root, child]})
    assert result["active_ms"] == 100 and result["cost_usd"] is None
    assert result["input_tokens"] is None
    child["ended_at"] = None
    assert metrics({"root_span_id": "root", "frames": [root, child]})["partial"]
    assert metrics({"root_span_id": "root", "frames": [root]})["cost_basis"] == "no_model_calls"


@pytest.mark.asyncio
async def test_store_outage_circuit_breaker_and_explicit_read_failure():
    from core.graph.approval_service import ApprovalError

    store = TraceStore("postgresql://invalid:invalid@127.0.0.1:1/invalid")
    assert not await store.write("SELECT 1", ())
    assert not await store.write("SELECT 1", ())
    assert store.write_failures == 1
    with pytest.raises(ApprovalError) as error:
        await store.list(PolicySubject(id="owner", role="admin"))
    assert error.value.status == 503


@pytest.mark.asyncio
async def test_disabled_runtime_does_not_persist_or_propagate():
    store = MemoryTraceStore()
    runtime = Runtime(Settings(_env_file=None, observation_enabled=False), store)
    async with runtime.operation(PolicySubject(id="u", role="admin"), "plan"), span("child"):
        assert headers() == {}
    assert not store.frames and not store.traces
    await runtime.close()


@pytest.mark.asyncio
async def test_export_and_recording_outages_do_not_fail_business():
    store = TraceStore("postgresql://invalid:invalid@127.0.0.1:1/invalid")
    runtime = Runtime(
        Settings(
            _env_file=None,
            observation_enabled=True,
            otel_exporter_otlp_traces_endpoint="http://127.0.0.1:1/v1/traces",
        ),
        store,
    )
    completed = []
    async with (
        runtime.operation(PolicySubject(id="u", role="admin"), "execute"),
        span("tool.call"),
    ):
        completed.append(True)
    assert completed == [True] and store.write_failures == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_w3c_header_reaches_policy_http_transport():
    from core.policy.opa_client import PolicyClient, PolicyQuery, PolicyResource

    seen = []

    def respond(request):
        seen.append(request.headers.get("traceparent"))
        return httpx.Response(200, json={"result": {"allow": True, "reason": "allowed"}})

    runtime = Runtime(Settings(_env_file=None, observation_enabled=True), MemoryTraceStore())
    subject = PolicySubject(id="u", role="admin", tenant="t")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://opa"
    ) as client:
        policy = PolicyClient("http://opa", client=client)
        async with runtime.operation(subject, "phase", "r"):
            assert (
                await policy.authorize(
                    PolicyQuery(
                        subject=subject, action="trace.read", resource=PolicyResource(type="trace")
                    )
                )
            ).allow
    assert seen[0].split("-")[1] == identity("t", "r")
    await runtime.close()
