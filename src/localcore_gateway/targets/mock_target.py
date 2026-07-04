"""Mock gateway target: tools declared entirely in config, canned outcomes.

Local-only, NO AWS analog -- this exists so you can develop and test the
agent before the real tools do: each tool is declared with its normal
schemas (advertised via tools/list like any other target) plus a canned
``response`` (returned verbatim as the payload) or ``error`` (returned as
the standard error envelope). Arguments are accepted and ignored. Static for
``lcgw sync`` purposes (nothing to re-discover).
"""

from __future__ import annotations

from typing import Any

from localcore_gateway.config import MockTargetConfig
from localcore_gateway.targets.base import Target, ToolDef, ToolOutcome


class MockTarget(Target):
    def __init__(self, cfg: MockTargetConfig) -> None:
        self._cfg = cfg
        self._tools = {t.name: t for t in cfg.tools}

    @property
    def name(self) -> str:
        return self._cfg.name

    def list_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
                output_schema=t.output_schema,
            )
            for t in self._cfg.tools
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolOutcome:  # noqa: ARG002  # canned outcome; arguments ignored
        spec = self._tools.get(tool_name)
        if spec is None:
            return ToolOutcome(
                payload={
                    "errorMessage": f"unknown tool {tool_name!r} on target {self._cfg.name!r}",
                    "errorType": "ToolNotFound",
                },
                is_error=True,
            )
        if spec.error is not None:
            return ToolOutcome(
                payload={"errorMessage": spec.error.message, "errorType": spec.error.type},
                is_error=True,
            )
        return ToolOutcome(payload=spec.response, is_error=False)
