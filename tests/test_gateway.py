from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from localcore_gateway.config import load_config
from localcore_gateway.gateway import build_gateway

CONFIG = Path(__file__).resolve().parents[1] / "examples" / "config.yaml"


@pytest.fixture
async def gateway():
    cfg = load_config(CONFIG)
    mcp, targets = build_gateway(cfg)
    yield mcp, targets
    for t in targets:
        await t.aclose()


async def test_aggregated_tool_naming(gateway):
    mcp, _ = gateway
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert {"demo___echo", "demo___add", "demo___weather", "demo___boom"} <= names
    # builtin search was intentionally removed
    assert "x_amz_bedrock_agentcore_search" not in names


async def test_call_tool_end_to_end(gateway):
    mcp, _ = gateway
    async with Client(mcp) as client:
        res = await client.call_tool("demo___add", {"a": 2, "b": 40})
    assert res.structured_content == {"sum": 42.0}


async def test_lambda_error_surfaces_as_tool_error(gateway):
    mcp, _ = gateway
    async with Client(mcp) as client:
        with pytest.raises(Exception, match="intentional failure"):
            await client.call_tool("demo___boom", {})


async def test_bedrock_tool_name_injected(gateway):
    """The Lambda must see bedrockAgentCoreToolName == the un-prefixed tool."""
    _, targets = gateway
    outcome = await targets[0].call_tool("echo", {"message": "hi"})
    assert not outcome.is_error
    assert outcome.payload == {"echo": "hi"}


async def test_multi_target_aggregation_and_independence(gateway):
    """Two Lambda targets aggregate; same tool name under each is independent."""
    mcp, targets = gateway
    assert {t.name for t in targets} == {"demo", "math"}
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
        assert {"math___add", "math___mul"} <= names
        demo_add = await client.call_tool("demo___add", {"a": 2, "b": 40})
        math_add = await client.call_tool("math___add", {"a": 2, "b": 40})
        math_mul = await client.call_tool("math___mul", {"a": 6, "b": 7})
    # demo___add and math___add are different Lambdas, different shapes.
    assert demo_add.structured_content == {"sum": 42.0}
    assert math_add.structured_content == {"result": 42.0}
    assert math_mul.structured_content == {"result": 42.0}
