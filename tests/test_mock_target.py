from __future__ import annotations

import pytest
from fastmcp import Client

from localcore_gateway.config import GatewayConfig
from localcore_gateway.gateway import build_gateway
from localcore_gateway.targets.mock_target import MockTarget
from localcore_gateway.testing import call_tool, serve_gateway

CONFIG = {
    "targets": [
        {
            "type": "mock",
            "name": "mymock",
            "tools": [
                {"name": "profile", "response": {"user": "ada", "plan": "pro"}},
                {"name": "tags", "response": ["a", "b"]},
                {"name": "count", "response": 42},
                {"name": "nothing", "response": None},
                {"name": "denied", "error": {"message": "quota exceeded", "type": "QuotaError"}},
                {"name": "vague", "error": {"message": "nope"}},  # type defaults to MockError
            ],
        }
    ]
}


def _target() -> MockTarget:
    gw = GatewayConfig.model_validate(CONFIG)
    return MockTarget(gw.targets[0])


async def test_canned_responses_returned_verbatim():
    tgt = _target()
    assert (await tgt.call_tool("profile", {})).payload == {"user": "ada", "plan": "pro"}
    assert (await tgt.call_tool("tags", {"ignored": 1})).payload == ["a", "b"]
    assert (await tgt.call_tool("count", {})).payload == 42
    assert (await tgt.call_tool("nothing", {})).payload is None  # `response: null` is a valid canned value


async def test_canned_errors_use_standard_envelope():
    tgt = _target()
    out = await tgt.call_tool("denied", {})
    assert out.is_error
    assert out.payload == {"errorMessage": "quota exceeded", "errorType": "QuotaError"}
    out = await tgt.call_tool("vague", {})
    assert out.payload["errorType"] == "MockError"  # the default


async def test_unknown_tool_mapping():
    out = await _target().call_tool("nope", {})
    assert out.is_error
    assert out.payload["errorType"] == "ToolNotFound"


def test_exactly_one_of_response_or_error():
    for tool in (
        {"name": "both", "response": 1, "error": {"message": "x"}},
        {"name": "neither"},
    ):
        with pytest.raises(ValueError, match="exactly one of"):
            GatewayConfig.model_validate({"targets": [{"type": "mock", "name": "m", "tools": [tool]}]})


def test_tools_required_non_empty():
    with pytest.raises(ValueError, match="at least one tool"):
        GatewayConfig.model_validate({"targets": [{"type": "mock", "name": "m", "tools": []}]})


async def test_aggregation_naming_and_schemas():
    cfg = {
        "targets": [
            {
                "type": "mock",
                "name": "mymock",
                "tools": [
                    {
                        "name": "profile",
                        "description": "canned profile",
                        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
                        "response": {"user": "ada"},
                    }
                ],
            }
        ]
    }
    mcp, targets = build_gateway(GatewayConfig.model_validate(cfg))
    try:
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
        assert "mymock___profile" in tools
        assert tools["mymock___profile"].description == "canned profile"
        assert tools["mymock___profile"].inputSchema["properties"] == {"id": {"type": "string"}}
    finally:
        for t in targets:
            await t.aclose()


def test_end_to_end_through_serve_gateway():
    with serve_gateway(CONFIG) as gw:
        assert call_tool(gw.url, "mymock___profile", {}) == {"user": "ada", "plan": "pro"}
