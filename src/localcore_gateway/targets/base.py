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
    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Target name; tools are exposed as ``<name>___<tool>``."""

    @abc.abstractmethod
    def list_tools(self) -> list[ToolDef]: ...

    @abc.abstractmethod
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Invoke ``tool_name`` (the un-prefixed name) with ``arguments``."""

    async def aclose(self) -> None:  # noqa: B027  # optional no-op hook
        """Release target resources (override if needed)."""
