"""A *second* example Lambda, backing the ``math`` target.

Demonstrates that a gateway aggregates multiple Lambda targets: this is a
different Lambda function from ``handlers.py``. Its ``add`` is independent of
``demo``'s ``add`` -- they surface as ``math___add`` vs ``demo___add``.
"""

from __future__ import annotations


def handler(event, context):
    tool = (getattr(context.client_context, "custom", {}) or {}).get("bedrockAgentCoreToolName", "unknown")
    print(f"[math] tool={tool} event={event}")

    if tool == "add":
        return {"result": float(event["a"]) + float(event["b"])}

    if tool == "mul":
        return {"result": float(event["a"]) * float(event["b"])}

    raise ValueError(f"no branch for tool {tool!r}")
