"""Run a Strands agent against the local gateway (or real AgentCore Gateway).

localcore-gateway exposes the *same* MCP Streamable-HTTP surface that Strands
uses to talk to a real AgentCore Gateway, so only the URL (and, for AWS, an
auth token) changes between environments.

This is example-only code; `strands-agents` is NOT a runtime dependency of
localcore-gateway. Install it where your agent runs:

    uv pip install "strands-agents"          # or:  pip install strands-agents

Then, with the gateway serving (``lcgw serve -c examples/config.yaml``):

    python examples/strands_agent.py "What's the weather in Osaka? add 19 and 23"

Point at real AWS instead by setting LCGW_MCP_URL to the gateway endpoint and
supplying a bearer token (see the AgentCore docs); the rest is unchanged.
"""

from __future__ import annotations

import os
import sys

from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

MCP_URL = os.environ.get("LCGW_MCP_URL", "http://127.0.0.1:8080/mcp")


def main() -> None:
    prompt = " ".join(sys.argv[1:]) or "List the tools you have, then add 19 and 23."

    # No auth header: the local gateway is unauthenticated by design. For real
    # AgentCore Gateway, pass headers={"Authorization": f"Bearer {token}"} and
    # refresh the token before it expires.
    mcp_client = MCPClient(lambda: streamablehttp_client(MCP_URL))

    # Start the client once and keep it open while the agent runs.
    with mcp_client:
        tools = mcp_client.list_tools_sync()
        print(f"gateway tools: {[t.tool_name for t in tools]}", file=sys.stderr)
        agent = Agent(model=BedrockModel(), tools=tools)
        agent(prompt)


if __name__ == "__main__":
    main()
