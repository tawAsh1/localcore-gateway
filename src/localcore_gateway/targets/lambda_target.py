"""Lambda gateway target: AgentCore-faithful MCP <-> Lambda translation.

AgentCore Gateway invokes the Lambda with the **tool input arguments as the
event payload**, and passes a faithful ``context.client_context.custom`` map.
Per the AWS docs the custom map contains, notably,
``bedrockAgentCoreToolName`` = the **prefixed** ``<target>___<tool>`` name
(the handler is expected to strip the ``___`` prefix itself), plus message /
request / mcp-message / gateway / target identifiers. One Lambda target can
back many tools; the handler branches on the (stripped) tool name. The
Lambda's return value becomes the MCP tool result.
"""

from __future__ import annotations

import uuid
from typing import Any

from localcore_gateway.config import GatewayConfig, LambdaTargetConfig
from localcore_gateway.lambda_emu import make_invoker
from localcore_gateway.targets.base import NAME_SEP, Target, ToolDef, ToolOutcome


class LambdaTarget(Target):
    def __init__(self, cfg: LambdaTargetConfig, gateway_cfg: GatewayConfig) -> None:
        self._cfg = cfg
        self._gateway_id = gateway_cfg.server.name
        self._invoker = make_invoker(
            cfg.lambda_,
            code_roots=gateway_cfg.resolved_code_roots(cfg.lambda_),
            env_file=gateway_cfg.resolved_env_file(cfg.lambda_),
            python=gateway_cfg.resolved_python(cfg.lambda_),
        )
        self._tools = {
            t.name: ToolDef(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema,
                output_schema=t.output_schema,
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

        # AgentCore contract (AWS docs): event == tool arguments; the custom
        # map carries the **prefixed** tool name (handler strips `___`) plus
        # the documented identifiers, so handlers written to the AWS
        # boilerplate work unchanged here.
        client_context = {
            "bedrockAgentCoreMessageVersion": "1.0",
            "bedrockAgentCoreAwsRequestId": str(uuid.uuid4()),
            "bedrockAgentCoreMcpMessageId": str(uuid.uuid4()),
            "bedrockAgentCoreGatewayId": self._gateway_id,
            "bedrockAgentCoreTargetId": self._cfg.name,
            "bedrockAgentCoreToolName": f"{self._cfg.name}{NAME_SEP}{tool_name}",
        }
        result = await self._invoker.invoke(arguments, client_context=client_context)
        return ToolOutcome(
            payload=result.payload,
            is_error=result.errored,
            logs=result.logs,
        )

    async def aclose(self) -> None:
        await self._invoker.aclose()
