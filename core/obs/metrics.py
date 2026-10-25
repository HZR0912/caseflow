"""Metrics do not double count nested spans or mistake missing billing for zero."""


def metrics(record):
    frames = record["frames"]
    calls = [f for f in frames if f["name"] == "model.call"]
    usage_known = all(f["output"].get("usage_known") for f in calls)
    cost_known = all(f["output"].get("cost_usd") is not None for f in calls)
    starts = [f["started_at"] for f in frames]
    ends = [f["ended_at"] for f in frames if f["ended_at"]]
    return {
        "span_count": len(frames),
        "error_count": sum(f["status"] == "error" for f in frames),
        "unfinished_count": sum(f["ended_at"] is None for f in frames),
        "active_ms": round(
            sum(
                f["duration_ms"] or 0
                for f in frames
                if f["parent_span_id"] == record["root_span_id"]
            ),
            3,
        ),
        "wall_ms": round((max(ends) - min(starts)).total_seconds() * 1000, 3)
        if starts and ends
        else None,
        "model_calls": len(calls),
        "input_tokens": sum(f["output"].get("input_tokens", 0) for f in calls)
        if usage_known
        else None,
        "output_tokens": sum(f["output"].get("output_tokens", 0) for f in calls)
        if usage_known
        else None,
        "cost_usd": sum(f["output"]["cost_usd"] for f in calls) if cost_known else None,
        "cost_basis": "configured_rates"
        if calls and cost_known
        else "unknown"
        if calls
        else "no_model_calls",
        "partial": record.get("truncated", False) or any(f["ended_at"] is None for f in frames),
    }
