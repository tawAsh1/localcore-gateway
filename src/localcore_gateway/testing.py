"""Public testing helpers: run a real gateway in-process for handler tests.

The supported way to integration-test tool handlers (and agents) against
this gateway from pytest -- see docs/testing.md for a complete example:

    from localcore_gateway.testing import call_tool, serve_gateway

    def test_add():
        with serve_gateway({"targets": [...]}) as gw:
            assert call_tool(gw.url, "demo___add", {"a": 2, "b": 40}) == {"sum": 42}

Stability: this module is public API. The package is 0.x (config schema may
still move), but these names and signatures are intended to stay stable.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn

from localcore_gateway.config import GatewayConfig, load_config

__all__ = ["GatewayHandle", "call_tool", "free_port", "serve_asgi", "serve_gateway"]


def free_port() -> int:
    """An OS-assigned free TCP port on 127.0.0.1."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start(server: uvicorn.Server, run) -> threading.Thread:
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return thread


@contextlib.contextmanager
def serve_asgi(app: Any) -> Iterator[str]:
    """Serve any ASGI app on an ephemeral port (uvicorn thread); yields the base URL."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = _start(server, server.run)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@dataclass
class GatewayHandle:
    """A live test gateway: ``url`` is the MCP endpoint, ``base_url`` the server root."""

    base_url: str
    url: str


@contextlib.contextmanager
def serve_gateway(config: GatewayConfig | dict[str, Any] | str | Path) -> Iterator[GatewayHandle]:
    """Run a real gateway (uvicorn thread, ephemeral port) for the ``with`` block.

    ``config`` is a loaded :class:`GatewayConfig`, an inline config dict
    (the YAML structure as Python), or a path to a config YAML file. The full
    stack is live -- MCP endpoint plus the `/-/sync` / `/-/invocations` admin
    routes -- and targets are closed cleanly on exit.
    """
    if isinstance(config, GatewayConfig):
        cfg = config
    elif isinstance(config, dict):
        cfg = GatewayConfig.model_validate(config)
    else:
        cfg = load_config(config)

    # Lazy: pulls the whole gateway stack, keep `import localcore_gateway.testing` light.
    from localcore_gateway.app import build_app

    app, _mcp, targets = build_app(cfg)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    async def _run() -> None:
        try:
            await server.serve()
        finally:
            # On the server's own loop -- where target sessions/workers live.
            for t in targets:
                await t.aclose()

    thread = _start(server, lambda: asyncio.run(_run()))
    try:
        yield GatewayHandle(
            base_url=f"http://127.0.0.1:{port}",
            url=f"http://127.0.0.1:{port}{cfg.server.path}",
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def call_tool(url: str, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """One-shot tool call against a running gateway; returns the payload.

    Synchronous (runs its own event loop; call from plain sync tests).
    Returns the structured content when present, else the joined text
    content. A tool error raises ``fastmcp.exceptions.ToolError``.
    """
    from fastmcp import Client

    async def _call() -> Any:
        async with Client(url) as c:
            res = await c.call_tool(name, arguments or {})
        if res.structured_content is not None:
            return res.structured_content
        return "".join(getattr(b, "text", "") for b in (res.content or []))

    return asyncio.run(_call())
