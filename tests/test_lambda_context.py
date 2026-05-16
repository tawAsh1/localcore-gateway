from __future__ import annotations

from fastmcp import Client

from localcore_gateway.config import GatewayConfig
from localcore_gateway.gateway import build_gateway

CTX_HANDLER = "def handler(e, c): return dict(c.client_context.custom)\n"


def _gw(tmp_path, target):
    (tmp_path / "h.py").write_text(CTX_HANDLER)
    gw = GatewayConfig.model_validate({"server": {"name": "gw-local"}, "targets": [target]})
    gw.source_dir = str(tmp_path)
    return gw


async def test_client_context_is_agentcore_faithful(tmp_path):
    """Custom map matches the AWS docs: prefixed toolName + the right keys."""
    from localcore_gateway.gateway import build_targets

    gw = _gw(
        tmp_path,
        {
            "type": "lambda",
            "name": "demo",
            "lambda": {"backend": "native", "handler": "h.handler"},
            "tools": [{"name": "probe"}],
        },
    )
    [tgt] = build_targets(gw)
    out = await tgt.call_tool("probe", {})
    await tgt.aclose()
    cc = out.payload
    # Prefixed (handler is expected to strip `___` itself, per AWS).
    assert cc["bedrockAgentCoreToolName"] == "demo___probe"
    assert cc["bedrockAgentCoreMessageVersion"] == "1.0"
    assert cc["bedrockAgentCoreGatewayId"] == "gw-local"
    assert cc["bedrockAgentCoreTargetId"] == "demo"
    assert cc["bedrockAgentCoreAwsRequestId"]
    assert cc["bedrockAgentCoreMcpMessageId"]
    assert "bedrockAgentCoreTargetName" not in cc  # not a real AWS key


async def test_output_schema_is_advertised(tmp_path):
    out_schema = {
        "type": "object",
        "properties": {"custom": {"type": "object"}},
    }
    gw = _gw(
        tmp_path,
        {
            "type": "lambda",
            "name": "demo",
            "lambda": {"backend": "native", "handler": "h.handler"},
            "tools": [{"name": "probe", "outputSchema": out_schema}],
        },
    )
    mcp, targets = build_gateway(gw)
    try:
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
        assert tools["demo___probe"].outputSchema == out_schema
    finally:
        for t in targets:
            await t.aclose()
