"""OpenAPI gateway target: a REST API exposed as MCP tools.

AgentCore-faithful: the MCP tool name is the operation's ``operationId``
**verbatim** (operationId is required on every operation), and the spec's own
security schemes are ignored -- outbound auth is configured here (an API key
in a header/query, or a bearer token), the AgentCore credential-provider
analog.

FastMCP's ``OpenAPIProvider`` does the heavy lifting (spec -> HTTP request
translation via its ``RequestDirector``). We reuse only that engine: FastMCP's
own tool naming slugifies/truncates the operationId, which would diverge from
the real gateway, so we key off the raw ``route.operation_id`` instead and let
the gateway's aggregation add the ``<target>___`` prefix uniformly.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastmcp.server.providers.openapi import OpenAPIProvider

from localcore_gateway.config import (
    GatewayConfig,
    OpenAPIAuthConfig,
    OpenAPITargetConfig,
)
from localcore_gateway.targets.base import Target, ToolDef, ToolOutcome


class _ApiKeyAuth(httpx.Auth):
    """Injects a static API key (header or query) on every request.

    Applied by ``httpx.AsyncClient.send()`` -- which is what FastMCP's
    OpenAPITool uses -- so query-param keys work too (client default params
    would not, since the request is pre-built).
    """

    def __init__(self, where: str, name: str, value: str) -> None:
        self._where, self._name, self._value = where, name, value

    def auth_flow(self, request: httpx.Request):
        if self._where == "header":
            request.headers[self._name] = self._value
        else:
            request.url = request.url.copy_merge_params({self._name: self._value})
        yield request


def _build_auth(a: OpenAPIAuthConfig) -> httpx.Auth | None:
    if a.type == "none":
        return None
    if a.type == "bearer":
        return _ApiKeyAuth("header", "Authorization", f"Bearer {a.value}")
    return _ApiKeyAuth(a.in_, a.name, a.value or "")


class OpenAPITarget(Target):
    def __init__(self, cfg: OpenAPITargetConfig, gw: GatewayConfig) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=gw.openapi_base_url(cfg),
            timeout=cfg.timeout_sec,
            auth=_build_auth(cfg.auth),
        )
        # OpenAPIProvider builds all tools eagerly (synchronously) in __init__.
        # validate_output=False: this is a local emulator, not a contract
        # checker -- don't reject responses that don't match the spec schema.
        provider = OpenAPIProvider(gw.openapi_spec(cfg), client=self._client, validate_output=False)

        self._tools: dict[str, Any] = {}  # operationId -> OpenAPITool
        missing: list[str] = []
        for t in provider._tools.values():  # noqa: SLF001  # eager, pinned <3.3
            route = t._route  # noqa: SLF001  # verbatim operationId for fidelity
            op = route.operation_id
            if not op:
                missing.append(f"{route.method} {route.path}")
                continue
            if op in self._tools:
                raise ValueError(f"openapi target {cfg.name!r}: duplicate operationId {op!r}")
            self._tools[op] = t
        if missing:
            raise ValueError(
                f"openapi target {cfg.name!r}: operationId is required on every "
                f"operation (missing: {', '.join(missing)})"
            )

    @property
    def name(self) -> str:
        return self._cfg.name

    def list_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name=op,
                description=t.description or "",
                input_schema=t.parameters,
            )
            for op, t in self._tools.items()
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolOutcome:
        t = self._tools.get(tool_name)
        if t is None:
            return ToolOutcome(
                payload={
                    "errorMessage": f"unknown tool {tool_name!r} on target {self._cfg.name!r}",
                    "errorType": "ToolNotFound",
                },
                is_error=True,
            )
        try:
            res = await t.run(arguments)
        except Exception as exc:  # noqa: BLE001  # any failure -> tool error
            return ToolOutcome(
                payload={"errorMessage": str(exc), "errorType": type(exc).__name__},
                is_error=True,
            )
        payload = res.structured_content
        if payload is None:
            payload = "".join(getattr(b, "text", "") for b in (res.content or []))
        return ToolOutcome(payload=payload, is_error=False)

    async def aclose(self) -> None:
        await self._client.aclose()
