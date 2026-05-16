"""Example Lambda handler backing several MCP tools.

One Lambda target can expose many tools; AgentCore Gateway tells the handler
which tool was called via ``context.client_context.custom``. This handler is
plain AWS Lambda code -- it would run unchanged on real Lambda.
"""

from __future__ import annotations


def handler(event, context):
    custom = getattr(context.client_context, "custom", {}) or {}
    tool = custom.get("bedrockAgentCoreToolName", "unknown")

    # Lambda logs -> CloudWatch (the gateway surfaces these in its console).
    print(
        f"invoked tool={tool} event={event} "
        f"remaining_ms={context.get_remaining_time_in_millis()}"
    )

    if tool == "echo":
        return {"echo": event.get("message", "")}

    if tool == "add":
        return {"sum": float(event["a"]) + float(event["b"])}

    if tool == "weather":
        city = event.get("city", "Tokyo")
        return {"city": city, "forecast": "sunny", "tempC": 21}

    if tool == "boom":  # demonstrates the Lambda error envelope
        raise RuntimeError("intentional failure for testing")

    raise ValueError(f"no branch for tool {tool!r}")
