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
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool, ToolResult
from pydantic.json_schema import SkipJsonSchema

from localcore_gateway.config import GatewayConfig
from localcore_gateway.targets.base import Target
from localcore_gateway.targets.lambda_target import LambdaTarget

log = logging.getLogger("lcgw")

NAME_SEP = "___"  # AgentCore Gateway tool-name convention


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

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        outcome = await self.dispatch(arguments)
        for line in outcome.logs:
            log.info("[%s] %s", self.name, line)
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


def build_targets(cfg: GatewayConfig) -> list[Target]:
    targets: list[Target] = []
    for tc in cfg.targets:
        if tc.type == "lambda":
            targets.append(LambdaTarget(tc, cfg))
        else:  # pragma: no cover - config validation prevents this
            raise ValueError(f"unsupported target type: {tc.type!r}")
    return targets


def build_gateway(cfg: GatewayConfig) -> tuple[FastMCP, list[Target]]:
    """Construct the FastMCP server and the live targets behind it."""
    # No inbound auth: this is a local dev tool. Front it externally if you
    # ever expose it (see SECURITY.md).
    mcp = FastMCP(name=cfg.server.name)
    targets = build_targets(cfg)

    tool_count = 0
    for target in targets:
        for td in target.list_tools():
            mcp.add_tool(
                GatewayTool(
                    name=f"{target.name}{NAME_SEP}{td.name}",
                    description=td.description,
                    parameters=td.input_schema or {"type": "object"},
                    dispatch=_make_dispatch(target, td.name),
                )
            )
            tool_count += 1

    log.info(
        "gateway %r ready: %d tool(s) across %d target(s)",
        cfg.server.name,
        tool_count,
        len(targets),
    )
    return mcp, targets
