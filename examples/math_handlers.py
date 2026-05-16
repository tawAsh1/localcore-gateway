"""A *second* example Lambda, backing the ``math`` target.

Demonstrates that a gateway aggregates multiple Lambda targets: this is a
different Lambda function from ``handlers.py``. Its ``add`` is independent of
``demo``'s ``add`` -- they surface as ``math___add`` vs ``demo___add``.
"""

from __future__ import annotations

SEP = "___"


def handler(event, context):
    custom = getattr(context.client_context, "custom", {}) or {}
    full = custom.get("bedrockAgentCoreToolName", "unknown")
    tool = full.split(SEP, 1)[1] if SEP in full else full
    print(f"[math] tool={tool} (full={full}) event={event}")

    if tool == "add":
        return {"result": float(event["a"]) + float(event["b"])}

    if tool == "mul":
        return {"result": float(event["a"]) * float(event["b"])}

    raise ValueError(f"no branch for tool {tool!r}")
