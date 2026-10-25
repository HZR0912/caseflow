"""Record provider-reported usage; configured rates are estimates, never billing claims."""

import json

from core.obs.privacy import label
from core.obs.runtime import span


async def completion(client, provider, **kwargs):
    async with span(
        "model.call", {"provider": label(provider.name), "model": label(provider.model)}
    ) as frame:
        frame.attributes["gen_ai.request.model"] = label(provider.model)
        frame.attributes["langfuse.observation.model.name"] = label(provider.model)
        response = await client.post(**kwargs)
        response.raise_for_status()
        body = response.json()
        usage = body.get("usage", {}) if isinstance(body, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
        known = all(type(v) is int and v >= 0 for v in (incoming, outgoing))
        frame.output = {"usage_known": known, "cost_usd": None}
        if known:
            frame.output.update(input_tokens=incoming, output_tokens=outgoing)
            frame.attributes.update(
                {
                    "gen_ai.usage.input_tokens": incoming,
                    "gen_ai.usage.output_tokens": outgoing,
                    "langfuse.observation.usage_details": json.dumps(
                        {"input": incoming, "output": outgoing}
                    ),
                }
            )
            if (
                provider.input_usd_per_million is not None
                and provider.output_usd_per_million is not None
            ):
                cost = (
                    incoming * provider.input_usd_per_million
                    + outgoing * provider.output_usd_per_million
                ) / 1_000_000
                frame.output["cost_usd"] = cost
                frame.attributes["langfuse.observation.cost_details"] = json.dumps({"total": cost})
        return body["choices"][0]["message"]["content"]
