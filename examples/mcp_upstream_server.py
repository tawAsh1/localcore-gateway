"""A tiny standalone MCP server, for the `type: mcp` target example.

Run it however you like (this file is just a FastMCP server; it doesn't need
localcore-gateway itself to run):

    python examples/mcp_upstream_server.py            # stdio (see mcp_config.yaml `local`)
    fastmcp run examples/mcp_upstream_server.py --transport http --port 9000  # streamable HTTP (see `remote`)

Demonstrates what a `type: mcp` target proxies: an unrelated MCP server's own
tools, exposed verbatim (the gateway adds the `<target>___` prefix).
"""

from __future__ import annotations

from fastmcp import FastMCP

mcp = FastMCP("mcp-upstream-example")


@mcp.tool
def convert_temp(celsius: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return celsius * 9 / 5 + 32


@mcp.tool
def reverse(text: str) -> str:
    """Reverse a string."""
    return text[::-1]


if __name__ == "__main__":
    mcp.run()
