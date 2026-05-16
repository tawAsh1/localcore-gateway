from __future__ import annotations

import httpx
import pytest
from fastmcp import Client

from localcore_gateway.config import GatewayConfig
from localcore_gateway.gateway import build_gateway
from localcore_gateway.targets.openapi_target import OpenAPITarget, _build_auth

# operationId chosen to expose FastMCP's slugify/split: it has '-', '.', and
# '__'. AgentCore uses it verbatim; FastMCP's own naming would mangle it.
OP = "weird-Op.v2__x"
SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://api.test.local/v1"}],
    "paths": {
        "/things": {
            "get": {
                "operationId": OP,
                "parameters": [
                    {
                        "name": "q",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        }
    },
}


def _target(handler, *, auth: dict | None = None) -> OpenAPITarget:
    tcfg = {"type": "openapi", "name": "api", "spec": SPEC}
    if auth is not None:
        tcfg["auth"] = auth
    gw = GatewayConfig.model_validate({"targets": [tcfg]})
    tc = gw.targets[0]
    tgt = OpenAPITarget(tc, gw)
    mc = httpx.AsyncClient(
        base_url=str(tgt._client.base_url),
        transport=httpx.MockTransport(handler),
        auth=_build_auth(tc.auth),
    )
    for t in tgt._tools.values():
        t._client = mc
    tgt._client = mc
    return tgt


def test_operation_id_is_verbatim_not_slugified():
    tgt = _target(lambda _req: httpx.Response(200, json={}))
    names = [td.name for td in tgt.list_tools()]
    assert names == [OP]  # NOT "weird_Op_v2" / split at "__"


async def test_param_translated_to_http_and_result():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["q"] = req.url.params.get("q")
        return httpx.Response(200, json={"ok": 1})

    tgt = _target(handler)
    out = await tgt.call_tool(OP, {"q": "hello"})
    await tgt.aclose()
    assert seen == {"path": "/v1/things", "q": "hello"}
    assert out.payload == {"ok": 1}
    assert not out.is_error


async def test_header_api_key_injected():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["k"] = req.headers.get("X-Key")
        return httpx.Response(200, json={})

    tgt = _target(handler, auth={"type": "apikey", "in": "header", "name": "X-Key", "value": "s3cr3t"})
    await tgt.call_tool(OP, {"q": "x"})
    await tgt.aclose()
    assert seen["k"] == "s3cr3t"


async def test_query_api_key_injected():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["k"] = req.url.params.get("api_key")
        return httpx.Response(200, json={})

    tgt = _target(handler, auth={"type": "apikey", "in": "query", "name": "api_key", "value": "qk"})
    await tgt.call_tool(OP, {"q": "x"})
    await tgt.aclose()
    assert seen["k"] == "qk"


async def test_bearer_auth_injected():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["a"] = req.headers.get("Authorization")
        return httpx.Response(200, json={})

    tgt = _target(handler, auth={"type": "bearer", "value": "tok"})
    await tgt.call_tool(OP, {"q": "x"})
    await tgt.aclose()
    assert seen["a"] == "Bearer tok"


def test_missing_operation_id_is_rejected():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "servers": [{"url": "https://api.test.local"}],
        "paths": {"/p": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    gw = GatewayConfig.model_validate({"targets": [{"type": "openapi", "name": "api", "spec": spec}]})
    with pytest.raises(ValueError, match="operationId is required"):
        OpenAPITarget(gw.targets[0], gw)


async def test_gateway_aggregation_uses_verbatim_prefix():
    gw = GatewayConfig.model_validate({"targets": [{"type": "openapi", "name": "api", "spec": SPEC}]})
    mcp, targets = build_gateway(gw)
    try:
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
        assert f"api___{OP}" in names  # target___<operationId>, verbatim
    finally:
        for t in targets:
            await t.aclose()
