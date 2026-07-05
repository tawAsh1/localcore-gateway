from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastmcp import Client, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import METHOD_NOT_FOUND, ErrorData

from conftest import serve_asgi
from localcore_gateway.config import GatewayConfig
from localcore_gateway.gateway import build_gateway, sync_targets
from localcore_gateway.history import InvocationLog
from localcore_gateway.targets.aws_gateway_target import AWSGatewayTarget
from localcore_gateway.targets.mcp_target import MCPTarget


def _make_upstream() -> FastMCP:
    srv = FastMCP("upstream")

    @srv.tool
    def add(a: int, b: int) -> int:
        return a + b

    @srv.prompt
    def review(code: str) -> str:
        """Ask for a code review."""
        return f"Please review: {code}"

    @srv.resource("data://config")
    def config() -> str:
        return "cfg-content"

    @srv.resource("data://items/{item_id}")
    def item(item_id: str) -> str:
        return f"item-{item_id}"

    _ = add, review, config, item
    return srv


@pytest.fixture
def upstream() -> Iterator[tuple[FastMCP, str]]:
    """A full-featured upstream (tool + prompt + resource + template), live."""
    srv = _make_upstream()
    with serve_asgi(srv.http_app(path="/mcp")) as base:
        yield srv, f"{base}/mcp"


def _gw(*target_cfgs: dict) -> GatewayConfig:
    return GatewayConfig.model_validate({"targets": list(target_cfgs)})


async def test_discovery_exposes_prompts_and_resources(upstream):
    _srv, url = upstream
    mcp, targets = build_gateway(_gw({"type": "mcp", "name": "up", "url": url}))
    try:
        async with Client(mcp) as c:
            prompts = {p.name: p for p in await c.list_prompts()}
            resources = [str(r.uri) for r in await c.list_resources()]
            templates = [t.uriTemplate for t in await c.list_resource_templates()]
        # Prompts carry the <target>___ prefix (AWS-documented convention);
        # resource URIs are verbatim.
        assert "up___review" in prompts
        args = {a.name: a for a in prompts["up___review"].arguments}
        assert args["code"].required
        assert "data://config" in resources
        assert "data://items/{item_id}" in templates
    finally:
        for t in targets:
            await t.aclose()


async def test_get_prompt_forwards_arguments(upstream):
    _srv, url = upstream
    mcp, targets = build_gateway(_gw({"type": "mcp", "name": "up", "url": url}))
    try:
        async with Client(mcp) as c:
            res = await c.get_prompt("up___review", {"code": "def f(): ..."})
        assert res.messages[0].content.text == "Please review: def f(): ..."
    finally:
        for t in targets:
            await t.aclose()


async def test_read_resource_static_and_template(upstream):
    _srv, url = upstream
    mcp, targets = build_gateway(_gw({"type": "mcp", "name": "up", "url": url}))
    try:
        async with Client(mcp) as c:
            static = await c.read_resource("data://config")
            templated = await c.read_resource("data://items/42")
        assert static[0].text == "cfg-content"
        assert templated[0].text == "item-42"  # expanded URI forwarded upstream
    finally:
        for t in targets:
            await t.aclose()


async def test_upstream_without_prompts_or_resources_unchanged():
    srv = FastMCP("plain")

    @srv.tool
    def ping() -> str:
        return "pong"

    _ = ping
    with serve_asgi(srv.http_app(path="/mcp")) as base:
        gw = _gw({"type": "mcp", "name": "up", "url": f"{base}/mcp"})
        tgt = MCPTarget(gw.targets[0], gw)
        try:
            assert [td.name for td in tgt.list_tools()] == ["ping"]
            assert tgt.list_prompts() == []
            assert tgt.list_resources() == []
        finally:
            await tgt.aclose()


async def test_advertised_but_unimplemented_listing_tolerated(upstream, monkeypatch):
    # A server may advertise the prompts capability yet not implement the
    # method; "method not found" must degrade to "no prompts", not an error.
    _srv, url = upstream

    async def method_not_found(_self, **_kwargs):
        raise McpError(ErrorData(code=METHOD_NOT_FOUND, message="Method not found"))

    monkeypatch.setattr(Client, "list_prompts", method_not_found)
    gw = _gw({"type": "mcp", "name": "up", "url": url})
    tgt = MCPTarget(gw.targets[0], gw)
    try:
        assert [td.name for td in tgt.list_tools()] == ["add"]
        assert tgt.list_prompts() == []
        assert tgt.list_resources() != []  # resources listing unaffected
    finally:
        await tgt.aclose()


def _prompt_only_upstream() -> FastMCP:
    srv = FastMCP("remote-gateway")

    @srv.prompt(name="orders___review")
    def review() -> str:
        return "remote prompt"

    _ = review
    return srv


async def test_aws_gateway_prompts_verbatim():
    with serve_asgi(_prompt_only_upstream().http_app(path="/mcp")) as base:
        gw = _gw({"type": "aws-gateway", "name": "prod", "url": f"{base}/mcp"})
        assert isinstance(build_gateway(gw)[1][0], AWSGatewayTarget)
        mcp, targets = build_gateway(gw)
        try:
            async with Client(mcp) as c:
                names = {p.name for p in await c.list_prompts()}
            assert "orders___review" in names  # verbatim, NOT prod___orders___review
            assert not any(n.startswith("prod___") for n in names)
        finally:
            for t in targets:
                await t.aclose()


async def test_prompt_name_collision_rejected_at_build():
    # Two aws-gateway targets exposing the same verbatim prompt name --
    # unlike resources, prompts hard-error (AWS documents no priority rule).
    with serve_asgi(_prompt_only_upstream().http_app(path="/mcp")) as base:
        cfg = _gw(
            {"type": "aws-gateway", "name": "one", "url": f"{base}/mcp"},
            {"type": "aws-gateway", "name": "two", "url": f"{base}/mcp"},
        )
        with pytest.raises(ValueError, match=r"prompt name collision.*orders___review"):
            build_gateway(cfg)


# --- resource priority routing (AWS: lowest resourcePriority wins) ---


def _shared_upstream(content: str) -> FastMCP:
    srv = FastMCP("shared")

    @srv.tool
    def ping() -> str:
        return content

    @srv.resource("shared://doc")
    def doc() -> str:
        return content

    _ = ping, doc
    return srv


async def test_lower_resource_priority_wins():
    a, b = _shared_upstream("from-a"), _shared_upstream("from-b")
    with serve_asgi(a.http_app(path="/mcp")) as ba, serve_asgi(b.http_app(path="/mcp")) as bb:
        cfg = _gw(
            {"type": "mcp", "name": "a", "url": f"{ba}/mcp"},
            {"type": "mcp", "name": "b", "url": f"{bb}/mcp", "resource_priority": 10},
        )
        mcp, targets = build_gateway(cfg)
        try:
            async with Client(mcp) as c:
                out = await c.read_resource("shared://doc")
            assert out[0].text == "from-b"  # priority 10 beats the default 100
        finally:
            for t in targets:
                await t.aclose()


async def test_priority_tie_goes_to_config_order_with_warning(caplog):
    a, b = _shared_upstream("from-a"), _shared_upstream("from-b")
    with serve_asgi(a.http_app(path="/mcp")) as ba, serve_asgi(b.http_app(path="/mcp")) as bb:
        cfg = _gw(
            {"type": "mcp", "name": "a", "url": f"{ba}/mcp"},
            {"type": "mcp", "name": "b", "url": f"{bb}/mcp"},
        )
        with caplog.at_level(logging.WARNING, logger="lcgw"):
            mcp, targets = build_gateway(cfg)
        try:
            assert any("equal resource_priority" in r.getMessage() for r in caplog.records)
            async with Client(mcp) as c:
                out = await c.read_resource("shared://doc")
            assert out[0].text == "from-a"  # first in config order
        finally:
            for t in targets:
                await t.aclose()


async def test_resync_flips_resource_ownership():
    a, b = _shared_upstream("from-a"), _shared_upstream("from-b")
    with serve_asgi(a.http_app(path="/mcp")) as ba, serve_asgi(b.http_app(path="/mcp")) as bb:
        cfg = _gw(
            {"type": "mcp", "name": "a", "url": f"{ba}/mcp"},
            {"type": "mcp", "name": "b", "url": f"{bb}/mcp", "resource_priority": 10},
        )
        mcp, targets = build_gateway(cfg)
        history = InvocationLog(10)
        try:
            # The winner drops the resource upstream -> the loser's copy
            # must now be served (ownership re-evaluated on resync).
            b.local_provider.remove_resource("shared://doc")
            res = await sync_targets(mcp, targets, history)
            assert res["b"]["resources"]["updated"] == ["shared://doc"]  # owner flip, URI stays
            async with Client(mcp) as c:
                out = await c.read_resource("shared://doc")
            assert out[0].text == "from-a"
        finally:
            for t in targets:
                await t.aclose()


# --- resync: prompts/resources sub-objects ---


async def test_resync_reports_and_serves_prompt_and_resource_changes(upstream):
    srv, url = upstream
    mcp, targets = build_gateway(_gw({"type": "mcp", "name": "up", "url": url}))
    history = InvocationLog(10)
    try:

        @srv.prompt
        def extra() -> str:
            return "extra prompt"

        @srv.resource("data://new")
        def new() -> str:
            return "new-content"

        _ = extra, new
        res = await sync_targets(mcp, targets, history)
        sub = res["up"]
        assert sub["added"] == []  # tools untouched -> top-level shape backward compatible
        assert sub["prompts"] == {"added": ["extra"], "removed": [], "updated": []}
        assert sub["resources"] == {"added": ["data://new"], "removed": [], "updated": []}

        async with Client(mcp) as c:
            got = await c.get_prompt("up___extra")
            assert got.messages[0].content.text == "extra prompt"
            out = await c.read_resource("data://new")
            assert out[0].text == "new-content"

        srv.local_provider.remove_prompt("extra")
        srv.local_provider.remove_resource("data://new")
        res = await sync_targets(mcp, targets, history)
        assert res["up"]["prompts"]["removed"] == ["extra"]
        assert res["up"]["resources"]["removed"] == ["data://new"]
    finally:
        for t in targets:
            await t.aclose()
