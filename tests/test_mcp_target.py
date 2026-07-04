from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from conftest import serve_asgi
from localcore_gateway.config import GatewayConfig
from localcore_gateway.gateway import build_gateway
from localcore_gateway.targets.mcp_target import MCPTarget


def _make_upstream() -> FastMCP:
    srv = FastMCP("upstream")

    @srv.tool
    def add(a: int, b: int) -> int:
        return a + b

    @srv.tool(output_schema=None)
    def greet(name: str) -> str:
        return f"hello, {name}"

    @srv.tool
    def boom() -> str:
        raise ValueError("kaboom")

    return srv


@pytest.fixture
def upstream_url() -> Iterator[str]:
    """A tiny FastMCP server, live on an ephemeral port over streamable HTTP."""
    with serve_asgi(_make_upstream().http_app(path="/mcp")) as base:
        yield f"{base}/mcp"


@pytest.fixture
def upstream_spy() -> Iterator[tuple[str, dict]]:
    """Like upstream_url, but also records each request's headers/query."""
    seen: dict = {}
    inner = _make_upstream().http_app(path="/mcp")

    async def app(scope, receive, send):
        if scope["type"] == "http":
            seen["query"] = scope["query_string"].decode()
            seen["headers"] = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        await inner(scope, receive, send)

    with serve_asgi(app) as base:
        yield f"{base}/mcp", seen


def _mcp_target(url: str, **extra) -> MCPTarget:
    gw = GatewayConfig.model_validate({"targets": [{"type": "mcp", "name": "up", "url": url, **extra}]})
    return MCPTarget(gw.targets[0], gw)


async def test_http_list_tools_names_and_schemas(upstream_url):
    tgt = _mcp_target(upstream_url)
    try:
        names = {td.name for td in tgt.list_tools()}
        assert names == {"add", "greet", "boom"}
        add = next(td for td in tgt.list_tools() if td.name == "add")
        assert add.input_schema["type"] == "object"
        assert set(add.input_schema["required"]) == {"a", "b"}
    finally:
        await tgt.aclose()


async def test_http_call_tool_structured_content(upstream_url):
    tgt = _mcp_target(upstream_url)
    try:
        out = await tgt.call_tool("add", {"a": 2, "b": 40})
        assert not out.is_error
        assert out.payload == {"result": 42}
    finally:
        await tgt.aclose()


async def test_http_call_tool_text_content(upstream_url):
    tgt = _mcp_target(upstream_url)
    try:
        out = await tgt.call_tool("greet", {"name": "world"})
        assert not out.is_error
        # No output schema declared on `greet` -> plain text content.
        assert "world" in out.payload
    finally:
        await tgt.aclose()


async def test_http_call_tool_error_mapping(upstream_url):
    tgt = _mcp_target(upstream_url)
    try:
        out = await tgt.call_tool("boom", {})
        assert out.is_error
        assert "kaboom" in out.payload["errorMessage"]
        assert out.payload["errorType"] == "ToolError"
    finally:
        await tgt.aclose()


async def test_unknown_tool_mapping(upstream_url):
    tgt = _mcp_target(upstream_url)
    try:
        out = await tgt.call_tool("nope", {})
        assert out.is_error
        assert out.payload["errorType"] == "ToolNotFound"
        assert "nope" in out.payload["errorMessage"]
    finally:
        await tgt.aclose()


async def test_gateway_aggregation_uses_prefix(upstream_url):
    gw = GatewayConfig.model_validate({"targets": [{"type": "mcp", "name": "up", "url": upstream_url}]})
    mcp, targets = build_gateway(gw)
    try:
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
        assert {"up___add", "up___greet", "up___boom"} <= names
    finally:
        for t in targets:
            await t.aclose()


async def test_allowlist_filters_tools(upstream_url):
    tgt = _mcp_target(upstream_url, tools=["add", "greet"])
    try:
        assert {td.name for td in tgt.list_tools()} == {"add", "greet"}
    finally:
        await tgt.aclose()


def test_allowlist_missing_tool_raises(upstream_url):
    gw = GatewayConfig.model_validate(
        {"targets": [{"type": "mcp", "name": "up", "url": upstream_url, "tools": ["add", "nonexistent"]}]}
    )
    with pytest.raises(ValueError, match="nonexistent"):
        MCPTarget(gw.targets[0], gw)


# --- config validation ---


def test_config_rejects_both_url_and_command():
    with pytest.raises(ValueError, match="exactly one of"):
        GatewayConfig.model_validate(
            {"targets": [{"type": "mcp", "name": "x", "url": "http://x/mcp", "command": "python"}]}
        )


def test_config_rejects_neither_url_nor_command():
    with pytest.raises(ValueError, match="exactly one of"):
        GatewayConfig.model_validate({"targets": [{"type": "mcp", "name": "x"}]})


def test_config_rejects_env_with_url():
    with pytest.raises(ValueError, match="`env` requires `command`"):
        GatewayConfig.model_validate(
            {"targets": [{"type": "mcp", "name": "x", "url": "http://x/mcp", "env": {"A": "b"}}]}
        )


def test_config_rejects_cwd_with_url():
    with pytest.raises(ValueError, match="`cwd` requires `command`"):
        GatewayConfig.model_validate({"targets": [{"type": "mcp", "name": "x", "url": "http://x/mcp", "cwd": "."}]})


def test_config_rejects_env_file_with_url():
    with pytest.raises(ValueError, match="`env_file` requires `command`"):
        GatewayConfig.model_validate(
            {"targets": [{"type": "mcp", "name": "x", "url": "http://x/mcp", "env_file": "a.env"}]}
        )


def test_config_rejects_headers_with_command():
    with pytest.raises(ValueError, match="`headers` requires `url`"):
        GatewayConfig.model_validate(
            {"targets": [{"type": "mcp", "name": "x", "command": "python", "headers": {"A": "b"}}]}
        )


def test_config_rejects_auth_with_command():
    with pytest.raises(ValueError, match="`auth` requires `url`"):
        GatewayConfig.model_validate(
            {
                "targets": [
                    {
                        "type": "mcp",
                        "name": "x",
                        "command": "python",
                        "auth": {"type": "bearer", "value": "tok"},
                    }
                ]
            }
        )


# --- outbound auth / headers (http mode; same engine as OpenAPI targets) ---


async def test_http_bearer_and_custom_headers_sent(upstream_spy):
    url, seen = upstream_spy
    tgt = _mcp_target(url, auth={"type": "bearer", "value": "tok"}, headers={"X-Custom": "v"})
    try:
        assert tgt.list_tools()  # discovery itself already authenticated
        assert seen["headers"]["authorization"] == "Bearer tok"
        assert seen["headers"]["x-custom"] == "v"
    finally:
        await tgt.aclose()


async def test_http_query_api_key_injected(upstream_spy):
    url, seen = upstream_spy
    tgt = _mcp_target(url, auth={"type": "apikey", "in": "query", "name": "api_key", "value": "qk"})
    try:
        out = await tgt.call_tool("add", {"a": 1, "b": 2})
        assert not out.is_error
        assert "api_key=qk" in seen["query"]
    finally:
        await tgt.aclose()


async def test_session_reopened_after_death(upstream_url):
    tgt = _mcp_target(upstream_url)
    try:
        out = await tgt.call_tool("add", {"a": 1, "b": 1})
        assert not out.is_error
        await tgt._client.close()  # simulate a dropped upstream session
        assert not tgt._client.is_connected()
        out = await tgt.call_tool("add", {"a": 2, "b": 3})
        assert not out.is_error
        assert out.payload == {"result": 5}
    finally:
        await tgt.aclose()


# --- stdio mode ---

_STDIO_SERVER = textwrap.dedent(
    """
    from fastmcp import FastMCP

    srv = FastMCP("stdio-upstream")

    @srv.tool
    def double(n: int) -> int:
        return n * 2

    if __name__ == "__main__":
        srv.run()
    """
)


async def test_stdio_round_trip(tmp_path):
    script = tmp_path / "stdio_server.py"
    script.write_text(_STDIO_SERVER)

    gw = GatewayConfig.model_validate(
        {
            "targets": [
                {
                    "type": "mcp",
                    "name": "local",
                    "command": sys.executable,
                    "args": [str(script)],
                }
            ]
        }
    )
    tgt = MCPTarget(gw.targets[0], gw)
    try:
        assert {td.name for td in tgt.list_tools()} == {"double"}
        out = await tgt.call_tool("double", {"n": 21})
        assert not out.is_error
        assert out.payload == {"result": 42}
    finally:
        await tgt.aclose()


# --- stdio spawn semantics: command resolution, cwd default, env assembly ---


def test_resolved_command_bare_passes_through(tmp_path):
    gw = GatewayConfig.model_validate({"targets": [{"type": "mcp", "name": "x", "command": "python3"}]})
    gw.source_dir = str(tmp_path)
    assert gw.resolved_command(gw.targets[0]) == "python3"  # PATH lookup, untouched


def test_resolved_command_pathlike_resolves_against_config_dir(tmp_path):
    gw = GatewayConfig.model_validate({"targets": [{"type": "mcp", "name": "x", "command": "./venv/bin/python"}]})
    gw.source_dir = str(tmp_path)
    # Lexically normalized against the config dir; symlinks NOT followed
    # (same rule as lambda.python -- a venv interpreter must stay a venv path).
    assert gw.resolved_command(gw.targets[0]) == str(tmp_path / "venv" / "bin" / "python")


# Unannotated tools -> plain-text payloads (no structured wrapping to unpick).
_INTROSPECT_SERVER = textwrap.dedent(
    """
    import os

    from fastmcp import FastMCP

    srv = FastMCP("introspect-upstream")

    @srv.tool
    def cwd():
        return os.getcwd()

    @srv.tool
    def getenv(name):
        return os.environ.get(name, "<unset>")

    if __name__ == "__main__":
        srv.run()
    """
)


def _introspect_target(tmp_path, **extra) -> MCPTarget:
    script = tmp_path / "introspect_server.py"
    script.write_text(_INTROSPECT_SERVER)
    gw = GatewayConfig.model_validate(
        {"targets": [{"type": "mcp", "name": "local", "command": sys.executable, "args": [str(script)], **extra}]}
    )
    gw.source_dir = str(tmp_path)
    return MCPTarget(gw.targets[0], gw)


async def test_stdio_cwd_defaults_to_config_dir(tmp_path):
    tgt = _introspect_target(tmp_path)
    try:
        out = await tgt.call_tool("cwd", {})
        assert not out.is_error
        # .resolve() both sides: the child's getcwd() is symlink-free while
        # tmp_path may go through e.g. /tmp -> /private/tmp on macOS.
        assert Path(out.payload).resolve() == tmp_path.resolve()  # noqa: ASYNC240  # test-only local stat
    finally:
        await tgt.aclose()


async def test_stdio_env_file_merge_precedence_and_no_full_inheritance(tmp_path, monkeypatch):
    (tmp_path / "vars.env").write_text("FROM_FILE=file-value\nSHARED=file-value\n")
    monkeypatch.setenv("LCGW_PARENT_ONLY", "should-not-leak")
    tgt = _introspect_target(tmp_path, env_file="vars.env", env={"SHARED": "inline-value"})
    try:
        out = await tgt.call_tool("getenv", {"name": "FROM_FILE"})
        assert out.payload == "file-value"
        # Inline `env` wins over env_file.
        out = await tgt.call_tool("getenv", {"name": "SHARED"})
        assert out.payload == "inline-value"
        # The gateway's own env is NOT inherited: the MCP SDK spawns the
        # child with its safe default subset plus the configured env only.
        out = await tgt.call_tool("getenv", {"name": "LCGW_PARENT_ONLY"})
        assert out.payload == "<unset>"
    finally:
        await tgt.aclose()
