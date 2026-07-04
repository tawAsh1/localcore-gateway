"""The documented docs/testing.md example shape, verified as-is.

The `serve_asgi`/`free_port` halves of `localcore_gateway.testing` are
exercised by the whole suite (conftest re-exports them); this file covers the
public `serve_gateway` + `call_tool` surface.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from localcore_gateway.testing import call_tool, serve_gateway


def test_handler_and_mock_through_one_gateway(tmp_path):
    (tmp_path / "handlers.py").write_text("def handler(event, context):\n    return {'sum': event['a'] + event['b']}\n")
    config = {
        "targets": [
            # The real handler under test.
            {
                "type": "lambda",
                "name": "demo",
                "lambda": {"backend": "native", "handler": "handlers.handler", "code_root": str(tmp_path)},
                "tools": [
                    {
                        "name": "add",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                            "required": ["a", "b"],
                        },
                    }
                ],
            },
            # A tool that doesn't exist yet, mocked out.
            {
                "type": "mock",
                "name": "billing",
                "tools": [{"name": "invoice", "response": {"total": 12.5, "currency": "USD"}}],
            },
        ]
    }
    with serve_gateway(config) as gw:
        assert gw.url == f"{gw.base_url}/mcp"
        # Real handler result.
        assert call_tool(gw.url, "demo___add", {"a": 2, "b": 40}) == {"sum": 42}
        # Mocked result.
        assert call_tool(gw.url, "billing___invoice", {}) == {"total": 12.5, "currency": "USD"}


def test_call_tool_raises_on_tool_error():
    config = {"targets": [{"type": "mock", "name": "m", "tools": [{"name": "boom", "error": {"message": "nope"}}]}]}
    with serve_gateway(config) as gw, pytest.raises(ToolError, match="nope"):
        call_tool(gw.url, "m___boom", {})


def test_serve_gateway_accepts_yaml_path(tmp_path):
    (tmp_path / "gw.yaml").write_text(
        "targets:\n  - type: mock\n    name: m\n    tools:\n      - name: ping\n        response: pong\n"
    )
    with serve_gateway(tmp_path / "gw.yaml") as gw:
        assert call_tool(gw.url, "m___ping") == "pong"
