"""Target interface. A target contributes tools to the aggregated gateway."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

# AgentCore Gateway tool-name convention: <target>___<tool>. Kept here (the
# leaf module) so both the gateway and targets can use it without a cycle.
NAME_SEP = "___"


@dataclass
class ToolDef:
    """A tool a target exposes (becomes one MCP tool)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


@dataclass
class ToolOutcome:
    """Backend-neutral result of a tool call; the gateway adapts it to MCP."""

    payload: Any
    is_error: bool = False
    logs: list[str] = field(default_factory=list)


class Target(abc.ABC):
    # Whether the gateway prefixes this target's tools with `<name>___`.
    # True for every AgentCore-faithful target type; the aws-gateway
    # passthrough sets False because a real gateway's tools already carry
    # the `remoteTarget___tool` form (re-prefixing would double it).
    prefix_tools: bool = True

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Target name; tools are exposed as ``<name>___<tool>``."""

    @abc.abstractmethod
    def list_tools(self) -> list[ToolDef]: ...

    @abc.abstractmethod
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Invoke ``tool_name`` (the un-prefixed name) with ``arguments``."""

    async def resync(self) -> list[ToolDef] | None:
        """Re-discover tools from the backend; the fresh list, or None.

        None means "static target, nothing to do" (Lambda/OpenAPI: their tool
        sets come from config/spec, re-read only on gateway restart). MCP
        targets override this to re-query the upstream server (the local
        SynchronizeGatewayTargets analog -- see ``gateway.sync_targets``).
        """
        return None

    async def aclose(self) -> None:  # noqa: B027  # optional no-op hook
        """Release target resources (override if needed)."""
