"""Builds the aggregated MCP gateway on top of FastMCP 3.x.

We do NOT hand-roll JSON-RPC / streamable HTTP -- FastMCP provides the wire
surface AgentCore Gateway also exposes (streamable HTTP at ``/mcp``). Our thin
AgentCore-compat layer is just:

* register every ``(target, tool)`` as one MCP tool named
  ``<target>___<tool>`` (AgentCore's triple-underscore convention) with the
  tool's explicit JSON Schema and a closure that dispatches into the target.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool, ToolResult
from pydantic.json_schema import SkipJsonSchema

from localcore_gateway.config import GatewayConfig
from localcore_gateway.history import InvocationLog
from localcore_gateway.targets.base import NAME_SEP, Target, ToolDef
from localcore_gateway.targets.lambda_target import LambdaTarget

log = logging.getLogger("lcgw")

__all__ = ["NAME_SEP", "build_gateway", "build_targets", "sync_targets"]


def _to_tool_result(payload: Any) -> ToolResult:
    if isinstance(payload, str):
        return ToolResult(content=payload)
    if isinstance(payload, dict):
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False, default=str),
            structured_content=payload,
        )
    if isinstance(payload, (list, int, float, bool)) or payload is None:
        return ToolResult(content=json.dumps(payload, default=str))
    return ToolResult(content=str(payload))


class GatewayTool(Tool):
    """A FastMCP tool with an explicit JSON Schema and a custom dispatcher."""

    dispatch: SkipJsonSchema[Callable[[dict[str, Any]], Any]]
    history: SkipJsonSchema[Any]  # InvocationLog (Any: pydantic can't schema it)

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        start = time.monotonic()
        outcome = await self.dispatch(arguments)
        duration_ms = (time.monotonic() - start) * 1000
        for line in outcome.logs:
            log.info("[%s] %s", self.name, line)
        # Record uniformly across target types (backs `lcgw tail`), and log
        # a one-line summary (the target logs above are already logged --
        # this is just the invoke itself).
        self.history.record(
            tool=self.name,
            arguments=arguments,
            payload=outcome.payload,
            is_error=outcome.is_error,
            duration_ms=duration_ms,
            logs=outcome.logs,
        )
        log.info("%s %s in %.0f ms", self.name, "ERROR" if outcome.is_error else "OK", duration_ms)
        if outcome.is_error:
            raise ToolError(
                json.dumps(outcome.payload, ensure_ascii=False, default=str)
                if not isinstance(outcome.payload, str)
                else outcome.payload
            )
        return _to_tool_result(outcome.payload)


def _make_dispatch(target: Target, tool_name: str):
    async def dispatch(arguments: dict[str, Any]):
        return await target.call_tool(tool_name, arguments)

    return dispatch


def _full_name(target: Target, tool_name: str) -> str:
    """The registered MCP tool name; verbatim for prefix_tools=False targets."""
    return f"{target.name}{NAME_SEP}{tool_name}" if target.prefix_tools else tool_name


def _make_gateway_tool(target: Target, td: ToolDef, history: InvocationLog) -> GatewayTool:
    kw: dict[str, Any] = {}
    if td.output_schema is not None:
        kw["output_schema"] = td.output_schema
    return GatewayTool(
        name=_full_name(target, td.name),
        description=td.description,
        parameters=td.input_schema or {"type": "object"},
        dispatch=_make_dispatch(target, td.name),
        history=history,
        **kw,
    )


def build_targets(cfg: GatewayConfig) -> list[Target]:
    targets: list[Target] = []
    for tc in cfg.targets:
        if tc.type == "lambda":
            targets.append(LambdaTarget(tc, cfg))
        elif tc.type == "openapi":
            from localcore_gateway.targets.openapi_target import OpenAPITarget

            targets.append(OpenAPITarget(tc, cfg))
        elif tc.type == "mcp":
            from localcore_gateway.targets.mcp_target import MCPTarget

            targets.append(MCPTarget(tc, cfg))
        elif tc.type == "aws-gateway":
            from localcore_gateway.targets.aws_gateway_target import AWSGatewayTarget

            targets.append(AWSGatewayTarget(tc, cfg))
        else:  # pragma: no cover - config validation prevents this
            raise ValueError(f"unsupported target type: {tc.type!r}")
    return targets


def build_gateway(cfg: GatewayConfig, history: InvocationLog | None = None) -> tuple[FastMCP, list[Target]]:
    """Construct the FastMCP server and the live targets behind it.

    ``history`` is the invocation ring buffer every tool records into; pass
    your own to read it back (the app layer does, for ``/-/invocations``),
    else a private one is created.
    """
    # No inbound auth: this is a local dev tool. Front it externally if you
    # ever expose it (see SECURITY.md).
    mcp = FastMCP(name=cfg.server.name)
    targets = build_targets(cfg)
    if history is None:
        history = InvocationLog(cfg.server.history)

    # Registered name -> owning target. Prefixed names can only collide when
    # two targets share a name; verbatim (aws-gateway) names can collide with
    # anything -- both are config errors, caught here rather than silently
    # shadowed in FastMCP's registry.
    owners: dict[str, str] = {}
    tool_count = 0
    for target in targets:
        for td in target.list_tools():
            full = _full_name(target, td.name)
            if full in owners:
                raise ValueError(
                    f"tool name collision: {full!r} from target {target.name!r} "
                    f"is already registered by target {owners[full]!r}"
                )
            owners[full] = target.name
            mcp.add_tool(_make_gateway_tool(target, td, history))
            tool_count += 1

    log.info(
        "gateway %r ready: %d tool(s) across %d target(s)",
        cfg.server.name,
        tool_count,
        len(targets),
    )
    return mcp, targets


async def _resync_target(
    mcp: FastMCP,
    target: Target,
    history: InvocationLog,
    taken: dict[str, str],
) -> dict[str, list[str]] | None:
    """Run ``target.resync()`` and reconcile the live tool registry; None = static.

    ``taken`` maps every OTHER target's registered tool name to its owner, so
    a newly appearing name that collides (possible for verbatim aws-gateway
    names, or when two targets share a name) errors instead of shadowing.
    """
    old = {td.name: td for td in target.list_tools()}
    fresh = await target.resync()
    if fresh is None:
        return None
    new = {td.name: td for td in fresh}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    updated = sorted(
        n
        for n in set(new) & set(old)
        if (old[n].description, old[n].input_schema, old[n].output_schema)
        != (new[n].description, new[n].input_schema, new[n].output_schema)
    )
    collisions = [n for n in added if _full_name(target, n) in taken]
    if collisions:
        # Registry left untouched. The target's own discovered set is now
        # ahead of it, so after resolving the collision (a config change on
        # the owning target) restart the gateway -- say so in the error.
        detail = ", ".join(
            f"{_full_name(target, n)!r} (owned by target {taken[_full_name(target, n)]!r})" for n in collisions
        )
        raise ValueError(f"tool name collision: {detail}; resolve it and restart the gateway")
    for n in removed + updated:
        mcp.local_provider.remove_tool(_full_name(target, n))
    for n in updated + added:
        mcp.add_tool(_make_gateway_tool(target, new[n], history))
    return {"added": added, "removed": removed, "updated": updated}


async def sync_targets(
    mcp: FastMCP,
    targets: list[Target],
    history: InvocationLog,
    only: str | None = None,
) -> dict[str, Any]:
    """Local SynchronizeGatewayTargets analog (POST /-/sync, `lcgw sync`).

    Divergence from AWS: the real API (PUT /gateways/{id}/synchronize) is
    asynchronous (202 + poll); this is synchronous and returns the result
    directly. Per-target value: ``{"added", "removed", "updated"}`` name
    lists, the string ``"static"`` (nothing to re-discover), or
    ``{"error": ...}`` -- one failing target does not fail the others.
    """
    results: dict[str, Any] = {}
    for target in targets:
        if only is not None and target.name != only:
            continue
        taken = {_full_name(t, td.name): t.name for t in targets if t is not target for td in t.list_tools()}
        try:
            diff = await _resync_target(mcp, target, history, taken)
        except Exception as exc:  # noqa: BLE001  # per-target error, keep going
            results[target.name] = {"error": str(exc)}
            continue
        results[target.name] = "static" if diff is None else diff
    if only is not None and only not in results:
        results[only] = {"error": f"unknown target {only!r}"}
    return results
