"""Target interface. A target contributes tools (and, for MCP-server
targets, prompts and resources) to the aggregated gateway."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

# AgentCore Gateway tool-name convention: <target>___<tool>. Kept here (the
# leaf module) so both the gateway and targets can use it without a cycle.
# AWS documents the same convention for prompts (see
# gateway-using-mcp-prompts-get in the devguide).
NAME_SEP = "___"


@dataclass
class ToolDef:
    """A tool a target exposes (becomes one MCP tool)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


@dataclass
class PromptDef:
    """A prompt a target exposes (becomes one MCP prompt) -- MCP targets only."""

    name: str
    description: str
    arguments: list[dict[str, Any]] = field(default_factory=list)  # {name, description, required}


@dataclass
class ResourceDef:
    """A resource a target exposes -- MCP targets only.

    ``uri`` is the concrete URI, or the URI template when ``template`` is
    True. Exposed verbatim (URIs must not be prefixed).
    """

    uri: str
    name: str
    description: str
    mime_type: str
    template: bool = False


@dataclass
class ToolOutcome:
    """Backend-neutral result of a tool call; the gateway adapts it to MCP."""

    payload: Any
    is_error: bool = False
    logs: list[str] = field(default_factory=list)


class Target(abc.ABC):
    # Whether the gateway prefixes this target's tools AND prompts with
    # `<name>___`. True for every AgentCore-faithful target type; the
    # aws-gateway passthrough sets False because a real gateway's names
    # already carry the `remoteTarget___name` form (re-prefixing would
    # double it). Resource URIs are always verbatim, never prefixed.
    prefix_tools: bool = True

    # AgentCore's `resourcePriority` analog: when several targets expose the
    # same resource URI, the lowest value wins. Only meaningful for targets
    # that expose resources (MCP targets set it from config).
    resource_priority: int = 100

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Target name; tools are exposed as ``<name>___<tool>``."""

    @abc.abstractmethod
    def list_tools(self) -> list[ToolDef]: ...

    def list_prompts(self) -> list[PromptDef]:
        """Prompts this target exposes (MCP targets; every other type has none)."""
        return []

    def list_resources(self) -> list[ResourceDef]:
        """Resources (+ templates) this target exposes (MCP targets only)."""
        return []

    @abc.abstractmethod
    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Invoke ``tool_name`` (the un-prefixed name) with ``arguments``."""

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Render prompt ``name`` (un-prefixed); an ``mcp.types.GetPromptResult``.

        Only called for names in :meth:`list_prompts` (MCP targets override).
        """
        raise NotImplementedError

    async def read_resource(self, uri: str) -> Any:
        """Read resource ``uri``; a list of MCP resource-contents objects.

        Only called for URIs owned by this target (MCP targets override).
        """
        raise NotImplementedError

    async def resync(self) -> list[ToolDef] | None:
        """Re-discover from the backend; the fresh tool list, or None.

        None means "static target, nothing to do" (Lambda/OpenAPI: their tool
        sets come from config/spec, re-read only on gateway restart). MCP
        targets override this to re-query the upstream server (the local
        SynchronizeGatewayTargets analog -- see ``gateway.sync_targets``);
        their prompt/resource state refreshes as a side effect and the
        gateway re-reads it via list_prompts()/list_resources().
        """
        return None

    async def aclose(self) -> None:  # noqa: B027  # optional no-op hook
        """Release target resources (override if needed)."""
