"""ASGI app assembly.

``build_app`` returns the FastMCP Streamable-HTTP ASGI app (mounted at the
configured path -- the same wire surface AgentCore Gateway exposes). ``asgi``
is a uvicorn ``--factory`` entrypoint used by ``lcgw dev`` for hot reload; it
reads the config path from ``$LCGW_CONFIG``.
"""

from __future__ import annotations

import os
from typing import Any

from localcore_gateway.config import GatewayConfig, load_config
from localcore_gateway.gateway import build_gateway


def build_app(cfg: GatewayConfig) -> tuple[Any, Any, list[Any]]:
    """Return ``(asgi_app, fastmcp_server, targets)``."""
    mcp, targets = build_gateway(cfg)
    app = mcp.http_app(
        path=cfg.server.path,
        json_response=True,
        stateless_http=True,
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
