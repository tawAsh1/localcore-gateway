"""Example Lambda handler backing several MCP tools.

One Lambda target can expose many tools; AgentCore Gateway tells the handler
which tool was called via ``context.client_context.custom``. Per the AWS docs
``bedrockAgentCoreToolName`` is the **prefixed** ``<target>___<tool>`` name,
so the handler strips the ``___`` prefix itself (the AWS-documented pattern).
This is plain AWS Lambda code -- it runs unchanged on real Lambda.
"""

from __future__ import annotations

SEP = "___"


def handler(event, context):
    custom = getattr(context.client_context, "custom", {}) or {}
    full = custom.get("bedrockAgentCoreToolName", "unknown")
    tool = full.split(SEP, 1)[1] if SEP in full else full  # strip target prefix

    # Lambda logs -> CloudWatch (the gateway surfaces these in its console).
    print(f"invoked tool={tool} (full={full}) event={event} remaining_ms={context.get_remaining_time_in_millis()}")

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
