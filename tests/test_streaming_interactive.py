from __future__ import annotations

import contextlib
from collections.abc import Iterator

import httpx
import pytest
from fastmcp import Client, Context, FastMCP

from conftest import serve_asgi
from localcore_gateway.config import GatewayConfig
from localcore_gateway.gateway import build_gateway
from localcore_gateway.testing import serve_gateway

# ==========================================================================
# serving-mode matrix: sessions+SSE (default) vs stateless (pre-May-2026)
# ==========================================================================


def _upstream() -> FastMCP:
    srv = FastMCP("up")

    @srv.tool
    def add(a: int, b: int) -> int:
        return a + b

    _ = add
    return srv


@contextlib.contextmanager
def _gateway(stateless: bool, upstream_url: str | None = None) -> Iterator[str]:
    """A served gateway (mock target + optional MCP target); yields base URL."""
    targets: list[dict] = [
        {
            "type": "mock",
            "name": "m",
            "tools": [
                {
                    "name": "bad",
                    "outputSchema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                    "response": {"ok": "not-a-bool"},
                },
                {"name": "good", "response": {"fine": 1}},
            ],
        }
    ]
    if upstream_url:
        targets.append({"type": "mcp", "name": "up", "url": upstream_url})
    with serve_gateway({"server": {"stateless": stateless}, "targets": targets}) as gw:
        yield gw.base_url


@pytest.mark.parametrize("stateless", [False, True], ids=["sessions", "stateless"])
async def test_tool_call_and_admin_in_both_modes(stateless):
    with _gateway(stateless) as base:
        async with Client(f"{base}/mcp") as c:
            res = await c.call_tool("m___good", {})
            assert res.structured_content == {"fine": 1}
        # Admin routes are plain HTTP next to the MCP app; lifespan (the
        # SDK session manager in session mode) must be running for both.
        async with httpx.AsyncClient() as hc:
            assert (await hc.get(f"{base}/-/invocations")).status_code == 200
            sync = (await hc.post(f"{base}/-/sync", json={})).json()["targets"]
            assert sync == {"m": "static"}


@pytest.mark.parametrize("stateless", [False, True], ids=["sessions", "stateless"])
async def test_mcp_passthrough_round_trip_in_both_modes(stateless):
    with serve_asgi(_upstream().http_app(path="/mcp")) as ub, _gateway(stateless, f"{ub}/mcp") as base:
        async with Client(f"{base}/mcp") as c:
            res = await c.call_tool("up___add", {"a": 2, "b": 40})
        assert res.structured_content == {"result": 42}


@pytest.mark.parametrize("stateless", [False, True], ids=["sessions", "stateless"])
async def test_contract_bypass_pin_in_both_modes(stateless, monkeypatch):
    # Pin: contract_checks=off means an output-schema-violating payload
    # passes through untouched -- the full-CallToolResult path skipping the
    # SDK's output validation must hold in the SSE mode too.
    async def _noop(_self, _name, _result):
        return None

    from mcp.client.session import ClientSession

    monkeypatch.setattr(ClientSession, "_validate_tool_result", _noop)
    with _gateway(stateless) as base:
        async with Client(f"{base}/mcp") as c:
            res = await c.call_tool("m___bad", {}, raise_on_error=False)
        assert not res.is_error
        assert res.structured_content == {"ok": "not-a-bool"}


async def test_session_id_issued_only_in_session_mode():
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with _gateway(stateless=False) as base:
        async with httpx.AsyncClient() as hc:
            r = await hc.post(f"{base}/mcp", json=init, headers=headers)
        assert r.headers.get("mcp-session-id")  # Mcp-Session-Id on initialize
        assert r.headers["content-type"].startswith("text/event-stream")
    with _gateway(stateless=True) as base:
        async with httpx.AsyncClient() as hc:
            r = await hc.post(f"{base}/mcp", json=init, headers=headers)
        assert r.headers.get("mcp-session-id") is None
        assert r.headers["content-type"].startswith("application/json")


# ==========================================================================
# progress + logging notification passthrough
# ==========================================================================


def _noisy_upstream() -> FastMCP:
    srv = FastMCP("noisy")

    @srv.tool
    async def work(ctx: Context) -> str:
        await ctx.report_progress(1, 2, "halfway")
        await ctx.log("upstream log line", level="warning")
        return "done"

    _ = work
    return srv


async def test_progress_and_logs_stream_to_caller():
    with serve_asgi(_noisy_upstream().http_app(path="/mcp")) as ub:
        cfg = {"targets": [{"type": "mcp", "name": "up", "url": f"{ub}/mcp"}]}
        with serve_gateway(cfg) as gw:
            got: dict = {"progress": [], "logs": []}

            async def on_progress(progress, total, message):
                got["progress"].append((progress, total, message))

            async def on_log(message):
                got["logs"].append((message.level, message.data))

            async with Client(gw.url, log_handler=on_log) as c:
                res = await c.call_tool("up___work", {}, progress_handler=on_progress)
            assert res.data == "done"
            assert got["progress"] == [(1.0, 2.0, "halfway")]
            assert len(got["logs"]) == 1
            level, data = got["logs"][0]
            assert level == "warning"
            assert data["msg"] == "upstream log line"


async def test_notifications_dropped_without_caller_context():
    # Direct target invocation (`lcgw invoke` shape): no server Context to
    # re-emit into -- must be dropped silently, not crash.
    with serve_asgi(_noisy_upstream().http_app(path="/mcp")) as ub:
        gw = GatewayConfig.model_validate({"targets": [{"type": "mcp", "name": "up", "url": f"{ub}/mcp"}]})
        _mcp, targets = build_gateway(gw)
        try:
            out = await targets[0].call_tool("work", {})
            assert not out.is_error
            assert out.payload == {"result": "done"}  # wrapped structured content, as usual
        finally:
            for t in targets:
                await t.aclose()


# ==========================================================================
# elicitation + sampling passthrough
# ==========================================================================


def _interactive_upstream() -> FastMCP:
    srv = FastMCP("interactive")

    @srv.tool
    async def ask(ctx: Context) -> str:
        res = await ctx.elicit("what name?", response_type=str)
        return f"name={res.data}" if res.action == "accept" else f"action={res.action}"

    @srv.tool
    async def think(ctx: Context) -> str:
        block = await ctx.sample("say something")
        return f"sampled={block.text}"

    _ = ask, think
    return srv


async def test_elicitation_round_trip():
    with serve_asgi(_interactive_upstream().http_app(path="/mcp")) as ub:
        cfg = {"targets": [{"type": "mcp", "name": "up", "url": f"{ub}/mcp"}]}
        with serve_gateway(cfg) as gw:
            seen = {}

            async def on_elicit(message, _response_type, _params, _context):
                seen["message"] = message
                return "Ada"

            async with Client(gw.url, elicitation_handler=on_elicit) as c:
                res = await c.call_tool("up___ask", {})
            assert seen["message"] == "what name?"  # the question reached OUR caller
            assert res.data == "name=Ada"  # and the answer travelled back down


async def test_sampling_round_trip():
    with serve_asgi(_interactive_upstream().http_app(path="/mcp")) as ub:
        cfg = {"targets": [{"type": "mcp", "name": "up", "url": f"{ub}/mcp"}]}
        with serve_gateway(cfg) as gw:
            seen = {}

            async def on_sample(messages, _params, _context):
                seen["prompt"] = messages[0].content.text
                return "hello from the outer LLM"

            async with Client(gw.url, sampling_handler=on_sample) as c:
                res = await c.call_tool("up___think", {})
            assert seen["prompt"] == "say something"
            assert res.data == "sampled=hello from the outer LLM"


async def test_elicitation_rejected_in_stateless_mode():
    with serve_asgi(_interactive_upstream().http_app(path="/mcp")) as ub:
        cfg = {
            "server": {"stateless": True},
            "targets": [{"type": "mcp", "name": "up", "url": f"{ub}/mcp"}],
        }
        with serve_gateway(cfg) as gw:
            async with Client(gw.url) as c:
                res = await c.call_tool("up___ask", {}, raise_on_error=False)
            assert res.is_error
            text = "".join(getattr(b, "text", "") for b in res.content)
            assert "requires sessions" in text
