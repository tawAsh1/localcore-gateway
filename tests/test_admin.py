from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import yaml
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from conftest import free_port as _free_port
from conftest import serve_asgi as _serve
from localcore_gateway.__main__ import main
from localcore_gateway.app import build_app
from localcore_gateway.config import GatewayConfig
from localcore_gateway.history import PREVIEW_LIMIT, InvocationLog


def _make_upstream() -> FastMCP:
    srv = FastMCP("upstream")

    @srv.tool
    def add(a: int, b: int) -> int:
        return a + b

    @srv.tool
    def temp() -> str:
        return "temporary"

    return srv


def _mutate_upstream(srv: FastMCP) -> None:
    """Runtime tool-set change: add `extra`, drop `temp`, redefine `add`."""

    @srv.tool
    def extra(x: int) -> int:
        return x + 1

    srv.local_provider.remove_tool("temp")
    srv.local_provider.remove_tool("add")

    @srv.tool(name="add", description="now with a description")
    def add2(a: int, b: int) -> int:
        return a + b

    _ = extra, add2


@pytest.fixture
def env(tmp_path) -> Iterator[dict]:
    """A full gateway (MCP target + static lambda target) behind real HTTP.

    Targets are deliberately NOT aclose()d here: their persistent upstream
    sessions live on the uvicorn thread's event loop, which tears them down
    (task cancellation) when the server stops; closing them from the test's
    own loop would be a cross-loop await. No subprocesses are involved (the
    lambda target is never invoked).
    """
    upstream = _make_upstream()
    with _serve(upstream.http_app(path="/mcp")) as up_base:
        (tmp_path / "h.py").write_text("def handler(e, c): return {}\n")
        cfg = GatewayConfig.model_validate(
            {
                "server": {"history": 5},
                "targets": [
                    {"type": "mcp", "name": "up", "url": f"{up_base}/mcp"},
                    {
                        "type": "lambda",
                        "name": "st",
                        "lambda": {"backend": "native", "handler": "h.handler"},
                        "tools": [{"name": "ping"}],
                    },
                ],
            }
        )
        cfg.source_dir = str(tmp_path)
        app, _mcp, _targets = build_app(cfg)
        with _serve(app) as gw_base:
            yield {"gw": gw_base, "upstream": upstream, "tmp": tmp_path}


async def _tool_names(gw_base: str) -> set[str]:
    async with Client(f"{gw_base}/mcp") as c:
        return {t.name for t in await c.list_tools()}


# --- POST /-/sync ---


async def test_sync_reflects_upstream_changes(env):
    gw = env["gw"]
    assert {"up___add", "up___temp", "st___ping"} <= await _tool_names(gw)

    _mutate_upstream(env["upstream"])
    async with httpx.AsyncClient() as hc:
        resp = await hc.post(f"{gw}/-/sync", json={})
    assert resp.status_code == 200
    res = resp.json()["targets"]
    assert res["st"] == "static"
    assert res["up"] == {"added": ["extra"], "removed": ["temp"], "updated": ["add"]}

    names = await _tool_names(gw)
    assert "up___extra" in names
    assert "up___temp" not in names

    async with Client(f"{gw}/mcp") as c:
        # New tool is invocable; the updated one carries its new description.
        out = await c.call_tool("up___extra", {"x": 41})
        assert out.structured_content == {"result": 42}
        add = next(t for t in await c.list_tools() if t.name == "up___add")
        assert add.description == "now with a description"
        # The removed tool is gone from the registry entirely.
        with pytest.raises(ToolError, match="temp"):
            await c.call_tool("up___temp", {})


async def test_sync_target_filter_and_unknown_target(env):
    gw = env["gw"]
    async with httpx.AsyncClient() as hc:
        resp = await hc.post(f"{gw}/-/sync", json={"target": "st"})
        assert resp.json()["targets"] == {"st": "static"}
        resp = await hc.post(f"{gw}/-/sync", json={"target": "nope"})
        assert resp.json()["targets"] == {"nope": {"error": "unknown target 'nope'"}}


async def test_sync_rejects_non_json_body(env):
    async with httpx.AsyncClient() as hc:
        resp = await hc.post(f"{env['gw']}/-/sync", content=b"not json")
        assert resp.status_code == 400


async def test_sync_reports_per_target_error_for_lost_allowlisted_tool():
    """An allowlisted name gone at resync time errors that target only."""
    upstream = _make_upstream()
    with _serve(upstream.http_app(path="/mcp")) as up_base:
        cfg = GatewayConfig.model_validate(
            {"targets": [{"type": "mcp", "name": "up", "url": f"{up_base}/mcp", "tools": ["add", "temp"]}]}
        )
        app, _mcp, _targets = build_app(cfg)
        with _serve(app) as gw:
            upstream.local_provider.remove_tool("temp")
            async with httpx.AsyncClient() as hc:
                res = (await hc.post(f"{gw}/-/sync", json={})).json()["targets"]
            assert "temp" in res["up"]["error"]
            # The registry is untouched on a failed resync.
            assert {"up___add", "up___temp"} <= await _tool_names(gw)


# --- GET /-/invocations + ring buffer ---


async def test_invocations_endpoint_and_ring_wraparound(env):
    gw = env["gw"]
    async with Client(f"{gw}/mcp") as c:
        for i in range(7):  # history size is 5 -> the first 2 fall off
            await c.call_tool("up___add", {"a": i, "b": 1})

    async with httpx.AsyncClient() as hc:
        data = (await hc.get(f"{gw}/-/invocations")).json()
    seqs = [r["seq"] for r in data["invocations"]]
    assert seqs == [3, 4, 5, 6, 7]  # wraparound: oldest two evicted
    assert data["next"] == 7
    rec = data["invocations"][-1]
    assert rec["tool"] == "up___add"
    assert not rec["is_error"]
    assert rec["duration_ms"] >= 0
    assert '"a": 6' in rec["arguments"]

    # Cursor semantics: since + limit page through, next advances.
    async with httpx.AsyncClient() as hc:
        page = (await hc.get(f"{gw}/-/invocations", params={"since": 3, "limit": 2})).json()
        assert [r["seq"] for r in page["invocations"]] == [4, 5]
        assert page["next"] == 5
        rest = (await hc.get(f"{gw}/-/invocations", params={"since": page["next"]})).json()
        assert [r["seq"] for r in rest["invocations"]] == [6, 7]
        # limit=0 bootstrap: no records, cursor at "now".
        boot = (await hc.get(f"{gw}/-/invocations", params={"since": 0, "limit": 0})).json()
        assert boot == {"invocations": [], "next": 7}
        # Bad params -> 400.
        assert (await hc.get(f"{gw}/-/invocations", params={"since": "x"})).status_code == 400


def test_invocation_log_truncates_large_fields():
    log = InvocationLog(10)
    rec = log.record(
        tool="t___x",
        arguments={"blob": "x" * (2 * PREVIEW_LIMIT)},
        payload="small",
        is_error=False,
        duration_ms=1.0,
        logs=[],
    )
    assert rec["arguments_truncated"]
    assert len(rec["arguments"].encode()) <= PREVIEW_LIMIT
    assert not rec["payload_truncated"]


def test_invocation_log_since_is_restart_safe():
    log = InvocationLog(3)
    for i in range(3):
        log.record(tool="t", arguments={}, payload=i, is_error=False, duration_ms=0, logs=[])
    # A stale cursor from before a gateway restart (ahead of the log) heals.
    records, next_seq = log.since(99)
    assert records == []
    assert next_seq == 3


# --- MCP endpoint + admin routes coexist (lifespan regression) ---


async def test_admin_routes_coexist_with_mcp(env):
    gw = env["gw"]
    # MCP still works through the same app the admin routes ride in...
    async with Client(f"{gw}/mcp") as c:
        out = await c.call_tool("up___add", {"a": 2, "b": 40})
        assert out.structured_content == {"result": 42}
    # ...and both admin routes respond on the same server.
    async with httpx.AsyncClient() as hc:
        assert (await hc.get(f"{gw}/-/invocations")).status_code == 200
        assert (await hc.post(f"{gw}/-/sync", json={})).status_code == 200


# --- lcgw sync CLI ---


def test_cli_sync_against_running_gateway(env, tmp_path, capsys):
    # A minimal config pointing the CLI at the running gateway's port.
    port = int(env["gw"].rsplit(":", 1)[1])
    cli_cfg = tmp_path / "cli.yaml"
    cli_cfg.write_text(yaml.safe_dump({"server": {"host": "127.0.0.1", "port": port}}))

    _mutate_upstream(env["upstream"])
    rc = main(["sync", "-c", str(cli_cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "st: static (nothing to sync)" in out
    assert "up: +1 added, -1 removed, ~1 updated" in out
    assert "  + extra" in out
    assert "  - temp" in out


def test_tail_format_line():
    from localcore_gateway.__main__ import _format_invocation

    rec = {
        "time": "2026-07-04T07:27:47.092701+00:00",
        "tool": "demo___add",
        "is_error": False,
        "duration_ms": 12.3,
        "arguments": '{"a": 2, "b": 40}',
        "payload": '{"sum": 42.0}',
    }
    line = _format_invocation(rec)
    assert line.startswith("07:27:47 OK")
    assert "demo___add (12 ms)" in line
    assert 'args={"a": 2, "b": 40} -> {"sum": 42.0}' in line
    long_payload = "x" * 500
    line = _format_invocation({**rec, "is_error": True, "payload": long_payload})
    assert " ERROR " in line
    assert long_payload not in line  # previews are ellipsized


def test_cli_sync_unreachable_server(tmp_path, capsys):
    cli_cfg = tmp_path / "cli.yaml"
    cli_cfg.write_text(yaml.safe_dump({"server": {"host": "127.0.0.1", "port": _free_port()}}))
    rc = main(["sync", "-c", str(cli_cfg)])
    assert rc == 1
    assert "sync failed" in capsys.readouterr().err
