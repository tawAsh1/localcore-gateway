from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from localcore_gateway.config import load_config
from localcore_gateway.gateway import build_gateway

CONFIG = Path(__file__).resolve().parents[1] / "examples" / "config.yaml"


@pytest.fixture
def gateway():
    cfg = load_config(CONFIG)
    mcp, targets = build_gateway(cfg)
    return mcp, targets


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
        with pytest.raises(Exception) as ei:
            await client.call_tool("demo___boom", {})
    assert "intentional failure" in str(ei.value)


async def test_bedrock_tool_name_injected(gateway):
    """The Lambda must see bedrockAgentCoreToolName == the un-prefixed tool."""
    _, targets = gateway
    outcome = await targets[0].call_tool("echo", {"message": "hi"})
    assert not outcome.is_error
    assert outcome.payload == {"echo": "hi"}
