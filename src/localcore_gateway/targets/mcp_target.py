"""MCP-passthrough gateway target: another MCP server's tools, proxied.

AgentCore-faithful: real AgentCore Gateway MCP targets connect to the
upstream server over streamable HTTP, and the remote tool names are used
**verbatim** -- the gateway adds the ``<target>___`` prefix uniformly (do not
add another prefix here). Outbound auth is configured here, the AgentCore
credential-provider analog, with the same shape as OpenAPI targets. The stdio
``command`` mode is a local-only convenience (point the gateway at a local
MCP server you're developing, without standing up an HTTP listener for it
first); it has no AWS analog. Its subprocess does NOT inherit the gateway's
full environment -- the MCP SDK spawns it with a safe default subset (HOME,
PATH, SHELL, TERM, USER, LOGNAME on POSIX) plus the configured ``env_file`` /
``env``; ``command`` resolves like ``lambda.python`` (PATH command, or a
config-dir-relative path with symlinks not followed), while ``args`` are
opaque -- script paths in them are resolved by the child, relative to its
``cwd`` (default: the config file's directory).

FastMCP's own ``Client`` is the upstream connection: we reuse it as-is rather
than re-implementing MCP client logic (same "don't hand-roll the protocol"
stance as the rest of this project). Tool discovery happens eagerly in
``__init__`` (matching ``OpenAPITarget``): connect briefly, fetch
``list_tools()``, disconnect. Invocation keeps one persistent ``Client``
session, opened lazily on the first call and re-opened if the upstream
session died (``Client.is_connected()`` goes false once its background
session task unwinds -- upstream restart, dropped connection).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import ClientTransport, StdioTransport, StreamableHttpTransport

from localcore_gateway.config import GatewayConfig, MCPTargetConfig
from localcore_gateway.targets.base import Target, ToolDef, ToolOutcome

# Same outbound-auth semantics as OpenAPI targets (bearer / API key in a
# header or query param): the returned httpx.Auth is applied per request by
# the HTTP transport's underlying httpx client, so query-param keys work too.
from localcore_gateway.targets.openapi_target import _build_auth


def _run_sync(coro: Any) -> Any:
    """Run an async coroutine from sync code, whether or not a loop is running.

    ``__init__`` runs eagerly (matching ``OpenAPITarget``), which may happen
    before the ASGI event loop starts (plain sync context: ``asyncio.run``
    works fine) or inside one that's already running (e.g. pytest-asyncio
    tests construct targets from an async test function). ``asyncio.run``
    raises RuntimeError in the latter case, so fall back to a fresh thread
    running its own event loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class MCPTarget(Target):
    def __init__(self, cfg: MCPTargetConfig, gw: GatewayConfig) -> None:
        self._cfg = cfg
        self._gw = gw
        self._timeout = cfg.timeout_sec
        self._lock = asyncio.Lock()
        # One persistent Client/session for invocation, opened lazily on
        # first call_tool and re-opened if the session died (see
        # _connected_client). Discovery below uses its own short-lived
        # connection on its own event loop, which is gone once __init__
        # returns -- so it gets a fresh transport too (keep_alive=False for
        # stdio: nothing may outlive that loop). For stdio `command` mode
        # this means the subprocess is spawned twice (once for discovery,
        # once -- lazily -- for invocation); acceptable for a local dev tool,
        # but worth knowing if your upstream server is slow to boot.
        self._client: Client | None = None

        discovered = {td.name: td for td in _run_sync(self._discover())}

        if cfg.tools:
            missing = [n for n in cfg.tools if n not in discovered]
            if missing:
                raise ValueError(
                    f"mcp target {cfg.name!r}: allowlisted tool(s) not found on upstream server: {', '.join(missing)}"
                )
            self._tools = {n: discovered[n] for n in cfg.tools}
        else:
            self._tools = discovered

    def _transport(self, *, keep_alive: bool = True) -> ClientTransport:
        cfg = self._cfg
        if cfg.url:
            return StreamableHttpTransport(cfg.url, headers=dict(cfg.headers), auth=_build_auth(cfg.auth))
        return StdioTransport(
            command=self._gw.resolved_command(cfg),
            args=cfg.args,
            env=self._gw.mcp_env(cfg),
            cwd=self._gw.resolved_cwd(cfg),
            keep_alive=keep_alive,
        )

    async def _discover(self) -> list[ToolDef]:
        async with Client(self._transport(keep_alive=False), timeout=self._timeout) as client:
            tools = await client.list_tools()
        return [
            ToolDef(
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema,
                output_schema=t.outputSchema,
            )
            for t in tools
        ]

    @property
    def name(self) -> str:
        return self._cfg.name

    def list_tools(self) -> list[ToolDef]:
        return list(self._tools.values())

    async def _connected_client(self) -> Client:
        """The persistent upstream client, opened lazily; re-opened if dead."""
        async with self._lock:
            if self._client is not None and not self._client.is_connected():
                # is_connected() goes false once the client's background
                # session task unwound (upstream restart / dropped
                # connection): discard and rebuild from scratch.
                await self._client.close()
                self._client = None
            if self._client is None:
                client = Client(self._transport(), timeout=self._timeout)
                await client.__aenter__()  # held open across calls; closed in aclose()
                self._client = client
            return self._client

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if tool_name not in self._tools:
            return ToolOutcome(
                payload={
                    "errorMessage": f"unknown tool {tool_name!r} on target {self._cfg.name!r}",
                    "errorType": "ToolNotFound",
                },
                is_error=True,
            )
        try:
            client = await self._connected_client()
            # call_tool_mcp (not call_tool): the raw protocol result carries
            # isError as data instead of raising, which maps 1:1 onto
            # ToolOutcome.
            res = await client.call_tool_mcp(tool_name, arguments)
        except Exception as exc:  # noqa: BLE001  # any failure -> tool error
            return ToolOutcome(
                payload={"errorMessage": str(exc), "errorType": type(exc).__name__},
                is_error=True,
            )
        if res.isError:
            text = "".join(getattr(b, "text", "") for b in (res.content or []))
            return ToolOutcome(
                payload={"errorMessage": text or f"tool {tool_name!r} returned an error", "errorType": "ToolError"},
                is_error=True,
            )
        payload = res.structuredContent
        if payload is None:
            payload = "".join(getattr(b, "text", "") for b in (res.content or []))
        return ToolOutcome(payload=payload, is_error=False)

    async def aclose(self) -> None:
        async with self._lock:
            if self._client is not None:
                # close() (not __aexit__): force-disconnects the session AND
                # closes the transport (stdio: terminates the subprocess,
                # which __aexit__ would keep alive for reuse).
                await self._client.close()
                self._client = None
