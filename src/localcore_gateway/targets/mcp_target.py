"""MCP-passthrough gateway target: another MCP server's catalog, proxied.

AgentCore-faithful: real AgentCore Gateway MCP targets connect to the
upstream server over streamable HTTP and index its whole catalog -- tools
(``tools/list``), prompts (``prompts/list``), and resources
(``resources/list`` + ``resources/templates/list``). Remote tool and prompt
names are used **verbatim** here -- the gateway adds the ``<target>___``
prefix uniformly, matching AWS's documented convention for both (see
gateway-using-mcp-prompts-get in the devguide). Resource URIs are exposed
**as-is** (AWS: "The original URI from the MCP server is returned as-is");
when several targets expose the same URI, ``resource_priority`` decides who
serves it (the AgentCore ``resourcePriority`` analog). Outbound auth is
configured here, the AgentCore credential-provider analog, with the same
shape as OpenAPI targets.

The stdio ``command`` mode is a local-only convenience (point the gateway at
a local MCP server you're developing, without standing up an HTTP listener
for it first); it has no AWS analog. Its subprocess does NOT inherit the
gateway's full environment -- the MCP SDK spawns it with a safe default
subset (HOME, PATH, SHELL, TERM, USER, LOGNAME on POSIX) plus the configured
``env_file`` / ``env``; ``command`` resolves like ``lambda.python`` (PATH
command, or a config-dir-relative path with symlinks not followed), while
``args`` are opaque -- script paths in them are resolved by the child,
relative to its ``cwd`` (default: the config file's directory).

FastMCP's own ``Client`` is the upstream connection: we reuse it as-is rather
than re-implementing MCP client logic (same "don't hand-roll the protocol"
stance as the rest of this project). Discovery happens eagerly in
``__init__`` (matching ``OpenAPITarget``): connect briefly, fetch the
catalog, disconnect -- prompts/resources only when the upstream advertises
the capability (and "method not found" from servers that advertise but don't
implement is tolerated). Invocation -- tools, ``prompts/get``, and
``resources/read`` alike -- forwards live over one persistent ``Client``
session, opened lazily on the first call and re-opened if the upstream
session died (``Client.is_connected()`` goes false once its background
session task unwinds -- upstream restart, dropped connection).

Mid-call passthrough, matching the real gateway (see gateway-mcp-progress /
-logging / -elicitation / -sampling in the devguide): upstream progress and
logging notifications re-emit to the calling client as they arrive, and
upstream elicitation (form mode) and sampling requests are relayed to OUR
caller, the answer travelling back down. These only stream in the default
session/SSE serving mode (``server.stateless: false``); elicitation/sampling
are rejected in stateless mode, and outside a gateway request (``lcgw
invoke``, direct target calls) notifications are dropped silently.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.client.transports import ClientTransport, StdioTransport, StreamableHttpTransport
from fastmcp.server.dependencies import get_context
from mcp.shared.exceptions import McpError
from mcp.types import METHOD_NOT_FOUND, ElicitRequestFormParams

from localcore_gateway.config import GatewayConfig, MCPTargetConfig
from localcore_gateway.targets.base import PromptDef, ResourceDef, Target, ToolDef, ToolOutcome

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


async def _tolerate_unimplemented(coro: Any) -> list[Any]:
    """The listing, or [] from a server that advertises a capability it
    doesn't implement (capability flags are advisory)."""
    try:
        return await coro
    except McpError as exc:
        if exc.error.code == METHOD_NOT_FOUND:
            return []
        raise


@dataclass
class _Catalog:
    """One upstream discovery pass: everything the gateway indexes."""

    tools: list[ToolDef]
    prompts: list[PromptDef] = field(default_factory=list)
    resources: list[ResourceDef] = field(default_factory=list)


class MCPTarget(Target):
    # In error messages; the aws-gateway subclass overrides it.
    _kind = "mcp"

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
        self.resource_priority = cfg.resource_priority
        # Server Contexts of in-flight call_tool callers, most recent last.
        # Client-LEVEL upstream handlers (log / elicitation / sampling) run
        # on the upstream session's own task where get_context() cannot see
        # the caller, so they route through here instead. With concurrent
        # callers the most recent one is picked (documented caveat).
        self._callers: list[Any] = []

        self._apply_catalog(_run_sync(self._discover()))

    def _apply_catalog(self, catalog: _Catalog) -> None:
        # The `tools:` allowlist filters tools only; prompts/resources pass.
        self._tools = self._apply_allowlist({td.name: td for td in catalog.tools})
        self._prompts = {p.name: p for p in catalog.prompts}
        self._resources = {r.uri: r for r in catalog.resources}

    def _apply_allowlist(self, discovered: dict[str, ToolDef]) -> dict[str, ToolDef]:
        cfg = self._cfg
        if not cfg.tools:
            return discovered
        missing = [n for n in cfg.tools if n not in discovered]
        if missing:
            raise ValueError(
                f"{self._kind} target {cfg.name!r}: allowlisted tool(s) not found "
                f"on upstream server: {', '.join(missing)}"
            )
        return {n: discovered[n] for n in cfg.tools}

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

    async def _discover(self) -> _Catalog:
        async with Client(self._transport(keep_alive=False), timeout=self._timeout) as client:
            tools = [
                ToolDef(
                    name=t.name,
                    description=t.description or "",
                    input_schema=t.inputSchema,
                    output_schema=t.outputSchema,
                )
                for t in await client.list_tools()
            ]
            # Prompts/resources only when the upstream advertises the
            # capability -- servers without them behave exactly as before
            # (no extra round trips, no errors).
            caps = client.initialize_result.capabilities if client.initialize_result else None
            prompts: list[PromptDef] = []
            resources: list[ResourceDef] = []
            if caps is not None and caps.prompts is not None:
                prompts = [
                    PromptDef(
                        name=p.name,
                        description=p.description or "",
                        arguments=[
                            {"name": a.name, "description": a.description, "required": bool(a.required)}
                            for a in (p.arguments or [])
                        ],
                    )
                    for p in await _tolerate_unimplemented(client.list_prompts())
                ]
            if caps is not None and caps.resources is not None:
                resources = [
                    ResourceDef(
                        uri=str(r.uri),
                        name=r.name,
                        description=r.description or "",
                        mime_type=r.mimeType or "text/plain",
                    )
                    for r in await _tolerate_unimplemented(client.list_resources())
                ] + [
                    ResourceDef(
                        uri=t.uriTemplate,
                        name=t.name,
                        description=t.description or "",
                        mime_type=t.mimeType or "text/plain",
                        template=True,
                    )
                    for t in await _tolerate_unimplemented(client.list_resource_templates())
                ]
        return _Catalog(tools=tools, prompts=prompts, resources=resources)

    @property
    def name(self) -> str:
        return self._cfg.name

    def list_tools(self) -> list[ToolDef]:
        return list(self._tools.values())

    def list_prompts(self) -> list[PromptDef]:
        return list(self._prompts.values())

    def list_resources(self) -> list[ResourceDef]:
        return list(self._resources.values())

    async def resync(self) -> list[ToolDef]:
        """Re-run upstream discovery and re-apply the allowlist.

        The SynchronizeGatewayTargets analog (driven by ``POST /-/sync`` /
        ``lcgw sync``). Refreshes the prompt/resource catalog too (the
        gateway re-reads it via list_prompts()/list_resources()). An
        allowlisted tool name missing at resync time raises, which the
        gateway sync layer reports as that target's error. Uses a fresh
        short-lived connection like ``__init__``; the persistent invocation
        session is untouched.
        """
        self._apply_catalog(await self._discover())
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
                # The upstream-initiated flows (logging notifications,
                # elicitation, sampling) are forwarded to the current caller
                # -- the AgentCore passthrough behavior.
                client = Client(
                    self._transport(),
                    timeout=self._timeout,
                    log_handler=self._forward_log,
                    elicitation_handler=self._forward_elicitation,
                    sampling_handler=self._forward_sampling,
                )
                await client.__aenter__()  # held open across calls; closed in aclose()
                self._client = client
            return self._client

    @staticmethod
    def _caller_context() -> Any | None:
        """The active fastmcp server Context, or None (direct invocation)."""
        try:
            return get_context()
        except RuntimeError:
            return None

    async def _forward_log(self, message: Any) -> None:
        """Upstream logging notification -> the current caller's session.

        No active caller (direct invocation, `lcgw invoke`, idle chatter
        between calls): dropped silently.
        """
        if not self._callers:
            return
        data = message.data
        text = data.get("msg") if isinstance(data, dict) and "msg" in data else str(data)
        extra = data.get("extra") if isinstance(data, dict) else None
        await self._callers[-1].log(str(text), level=message.level, logger_name=message.logger, extra=extra)

    def _interactive_caller(self, what: str) -> Any:
        """The caller Context an upstream {elicitation,sampling} goes to."""
        if self._gw.server.stateless:
            raise RuntimeError(f"{what} passthrough requires sessions (set server.stateless: false)")
        if not self._callers:
            raise RuntimeError(f"{what} passthrough requires an in-flight gateway tool call")
        return self._callers[-1]

    async def _forward_elicitation(self, message: str, _response_type: Any, params: Any, _context: Any) -> ElicitResult:
        """Upstream elicitation request -> our caller, answer travels back."""
        ctx = self._interactive_caller("elicitation")
        if not isinstance(params, ElicitRequestFormParams):
            # URL-mode elicitation: documented limitation (form mode only).
            # RuntimeError (not TypeError): an unsupported-feature condition
            # surfaced to the downstream server, not a coding bug.
            raise RuntimeError("URL-mode elicitation passthrough is not supported")  # noqa: TRY004
        # ctx.session is the SDK ServerSession: the raw requestedSchema is
        # forwarded 1:1 (no re-typing), and the raw answer travels back.
        res = await ctx.session.elicit_form(message, params.requestedSchema, related_request_id=ctx.request_id)
        return ElicitResult(action=res.action, content=res.content)

    async def _forward_sampling(self, _messages: Any, params: Any, _context: Any) -> Any:
        """Upstream sampling request -> our caller's LLM, result back down."""
        ctx = self._interactive_caller("sampling")
        return await ctx.session.create_message(
            messages=params.messages,
            max_tokens=params.maxTokens,
            system_prompt=params.systemPrompt,
            include_context=params.includeContext,
            temperature=params.temperature,
            stop_sequences=params.stopSequences,
            metadata=params.metadata,
            model_preferences=params.modelPreferences,
            related_request_id=ctx.request_id,
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if tool_name not in self._tools:
            return ToolOutcome(
                payload={
                    "errorMessage": f"unknown tool {tool_name!r} on target {self._cfg.name!r}",
                    "errorType": "ToolNotFound",
                },
                is_error=True,
            )
        # The caller's Context (None when invoked outside a server request,
        # e.g. `lcgw invoke` or direct target calls in tests): upstream
        # progress re-emits through it, and it anchors the log/elicitation/
        # sampling forwarding for the duration of the call.
        ctx = self._caller_context()
        progress_handler = None
        if ctx is not None:
            self._callers.append(ctx)

            async def progress_handler(  # matches fastmcp's ProgressHandler
                progress: float, total: float | None, message: str | None
            ) -> None:
                # No-op unless OUR caller sent a progressToken (the SDK keys
                # the notification to it) -- faithful passthrough semantics.
                await ctx.report_progress(progress, total, message)

        try:
            client = await self._connected_client()
            # call_tool_mcp (not call_tool): the raw protocol result carries
            # isError as data instead of raising, which maps 1:1 onto
            # ToolOutcome.
            res = await client.call_tool_mcp(tool_name, arguments, progress_handler=progress_handler)
        except Exception as exc:  # noqa: BLE001  # any failure -> tool error
            return ToolOutcome(
                payload={"errorMessage": str(exc), "errorType": type(exc).__name__},
                is_error=True,
            )
        finally:
            if ctx is not None:
                self._callers.remove(ctx)
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

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """``prompts/get``, proxied live to the upstream (AgentCore behavior)."""
        client = await self._connected_client()
        return await client.get_prompt(name, arguments)

    async def read_resource(self, uri: str) -> Any:
        """``resources/read``, proxied live to the upstream."""
        client = await self._connected_client()
        return await client.read_resource(uri)

    async def aclose(self) -> None:
        async with self._lock:
            if self._client is not None:
                # close() (not __aexit__): force-disconnects the session AND
                # closes the transport (stdio: terminates the subprocess,
                # which __aexit__ would keep alive for reuse).
                await self._client.close()
                self._client = None
