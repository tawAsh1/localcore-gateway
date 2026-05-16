"""Lambda gateway target: AgentCore-faithful MCP <-> Lambda translation.

AgentCore Gateway invokes the Lambda with the **tool input arguments as the
event payload**, and passes the tool identity via
``context.client_context.custom['bedrockAgentCoreToolName']``. One Lambda
target can back many tools; the handler branches on that value. The Lambda's
return value becomes the MCP tool result.
"""

from __future__ import annotations

from typing import Any

from localcore_gateway.config import GatewayConfig, LambdaTargetConfig
from localcore_gateway.lambda_emu import make_invoker
from localcore_gateway.targets.base import Target, ToolDef, ToolOutcome


class LambdaTarget(Target):
    def __init__(self, cfg: LambdaTargetConfig, gateway_cfg: GatewayConfig) -> None:
        self._cfg = cfg
        self._invoker = make_invoker(
            cfg.lambda_,
            code_roots=gateway_cfg.resolved_code_roots(cfg.lambda_),
            env_file=gateway_cfg.resolved_env_file(cfg.lambda_),
        )
        self._tools = {
            t.name: ToolDef(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
            )
            for t in gateway_cfg.effective_tools(cfg)
        }

    @property
    def name(self) -> str:
        return self._cfg.name

    def list_tools(self) -> list[ToolDef]:
        return list(self._tools.values())

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if tool_name not in self._tools:
            return ToolOutcome(
                payload={
                    "errorMessage": f"unknown tool {tool_name!r} on target {self._cfg.name!r}",
                    "errorType": "ToolNotFound",
                },
                is_error=True,
            )

        # AgentCore contract: event == tool arguments; tool identity rides on
        # client_context.custom.bedrockAgentCoreToolName.
        client_context = {
            "bedrockAgentCoreToolName": tool_name,
            "bedrockAgentCoreGatewayId": self._cfg.name,
            "bedrockAgentCoreTargetName": self._cfg.name,
        }
        result = await self._invoker.invoke(arguments, client_context=client_context)
        return ToolOutcome(
            payload=result.payload,
            is_error=result.errored,
            logs=result.logs,
        )

    async def aclose(self) -> None:
        await self._invoker.aclose()
