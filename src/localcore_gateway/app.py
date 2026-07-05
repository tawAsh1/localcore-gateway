"""ASGI app assembly.

``build_app`` returns the FastMCP Streamable-HTTP ASGI app (mounted at the
configured path -- the same wire surface AgentCore Gateway exposes) plus a
small local admin surface outside the MCP path:

* ``POST /-/sync`` -- re-sync targets (the SynchronizeGatewayTargets analog;
  synchronous, unlike AWS's 202-and-poll API).
* ``GET /-/invocations`` -- the invocation history ring buffer (cursor-based
  polling; backs ``lcgw tail``).

The admin surface is local-only and unauthenticated by design, same stance as
the MCP endpoint itself (see SECURITY.md), and has no AWS analog (AgentCore's
control plane is a separate API). Routes are registered via FastMCP's
``custom_route`` so they ride in the same app with the same lifespan.

``asgi`` is a uvicorn ``--factory`` entrypoint used by ``lcgw dev`` for hot
reload; it reads the config path from ``$LCGW_CONFIG``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from localcore_gateway.config import GatewayConfig, load_config
from localcore_gateway.gateway import build_gateway, sync_targets
from localcore_gateway.history import InvocationLog


def build_app(cfg: GatewayConfig) -> tuple[Any, Any, list[Any]]:
    """Return ``(asgi_app, fastmcp_server, targets)``."""
    history = InvocationLog(cfg.server.history)
    mcp, targets = build_gateway(cfg, history=history)

    @mcp.custom_route("/-/sync", methods=["POST"])
    async def _sync(request: Request) -> Response:
        body = await request.body()
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        if not isinstance(data, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        only = data.get("target")
        results = await sync_targets(mcp, targets, history, only=only, contract_checks=cfg.server.contract_checks)
        return JSONResponse({"targets": results})

    @mcp.custom_route("/-/invocations", methods=["GET"])
    async def _invocations(request: Request) -> Response:
        try:
            since = int(request.query_params.get("since", "0"))
            raw_limit = request.query_params.get("limit")
            limit = int(raw_limit) if raw_limit is not None else None
        except ValueError:
            return JSONResponse({"error": "since/limit must be integers"}, status_code=400)
        records, next_seq = history.since(since, limit=limit)
        return JSONResponse({"invocations": records, "next": next_seq})

    # Default (stateless=false): the modern gateway wire behavior (since May
    # 2026) -- stateful MCP sessions (Mcp-Session-Id issued on initialize)
    # and SSE-streamed responses, so mid-call notifications and
    # elicitation/sampling passthrough can flow. `server.stateless: true`
    # restores the pre-May-2026 behavior (buffered JSON, no sessions).
    app = mcp.http_app(
        path=cfg.server.path,
        json_response=cfg.server.stateless,
        stateless_http=cfg.server.stateless,
    )
    return app, mcp, targets


def asgi() -> Any:
    """uvicorn --factory entrypoint (reads $LCGW_CONFIG)."""
    cfg_path = os.environ.get("LCGW_CONFIG")
    if not cfg_path:
        raise RuntimeError("LCGW_CONFIG is not set")
    cfg = load_config(cfg_path)
    app, _, _ = build_app(cfg)
    return app
