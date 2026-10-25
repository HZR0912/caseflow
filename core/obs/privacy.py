"""Allowlisted projections: never send raw prompts, facts, arguments, credentials or errors."""

import re


def label(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,120}", value)
        else "redacted"
    )


def projection(state):
    if not isinstance(state, dict):
        return {}
    result = {}
    for key in ("status", "intent", "interpreter", "reason", "decision"):
        if isinstance(state.get(key), str):
            result[key] = label(state[key])
    if isinstance(state.get("facts"), dict):
        facts = state["facts"]
        result["confirmed_fields"] = sorted(
            label(k) for k, v in facts.items() if v is not None and v != []
        )
        result["employee_count"] = len(facts.get("employees", []))
    if isinstance(state.get("questions"), list):
        result["question_count"] = len(state["questions"])
    if isinstance(state.get("evidence"), list):
        result["evidence"] = [
            {
                "chunk_id": label(e.get("chunk_id")),
                "char_start": e.get("char_start"),
                "char_end": e.get("char_end"),
            }
            for e in state["evidence"][:30]
        ]
    if isinstance(state.get("plan"), dict):
        result["steps"] = [
            {
                "id": label(s.get("id")),
                "operation": label(s.get("operation")),
                "depends_on": [label(d) for d in s.get("depends_on", [])],
                "risk": label(s.get("risk")),
            }
            for s in state["plan"].get("steps", [])[:40]
        ]
    if isinstance(state.get("decisions"), list):
        result["decisions"] = [
            {
                "action": label(d.get("action")),
                "allow": d.get("allow") is True,
                "degraded": d.get("degraded") is True,
                "reason": label(d.get("reason")),
            }
            for d in state["decisions"][:80]
        ]
    if isinstance(state.get("preview"), dict):
        result["preview"] = [
            {"step_id": label(s.get("step_id")), "status": label(s.get("status"))}
            for s in state["preview"].get("steps", [])[:40]
        ]
    if isinstance(state.get("memory_context"), dict):
        result["memory_sources"] = [
            {"field": label(s.get("field")), "source": label(s.get("source"))}
            for s in state["memory_context"].get("sources", [])[:20]
        ]
    return result
