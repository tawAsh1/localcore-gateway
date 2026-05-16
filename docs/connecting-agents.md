# Connecting agents / MCP clients

The local gateway speaks the same wire protocol as the real AgentCore Gateway:
**MCP over Streamable HTTP**. Any MCP client points at it the same way it would
point at the AWS endpoint — only the URL changes.

```
local : http://127.0.0.1:8080/mcp
AWS   : https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
```

Tool names are identical in both (`<target>___<tool>`), so swapping environments
needs **no code change** in the agent — just the endpoint URL (and, against real
AWS, the auth token; the local gateway accepts requests without one).

## FastMCP / Python client

```python
from fastmcp import Client

async with Client("http://127.0.0.1:8080/mcp") as c:
    tools = await c.list_tools()
    res = await c.call_tool("demo___add", {"a": 2, "b": 40})
    print(res.structured_content)   # {'sum': 42.0}
```

The local gateway has no inbound auth, so no token is required. Any
`Authorization` header an agent sends (for the real AWS endpoint) is simply
ignored locally — agent code does not need to change between environments.

## Use with Strands Agents

[Strands Agents](https://strandsagents.com) talks to a real AgentCore Gateway
over MCP Streamable HTTP — the exact surface this project exposes. So a Strands
agent points at the local gateway by changing only the URL (and dropping the
auth token, since the local gateway is unauthenticated):

```python
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

mcp_client = MCPClient(lambda: streamablehttp_client("http://127.0.0.1:8080/mcp"))
with mcp_client:                          # start once; keep open while the agent runs
    tools = mcp_client.list_tools_sync()  # demo___add, demo___weather, ...
    agent = Agent(model=BedrockModel(), tools=tools)
    agent("What's the weather in Osaka? add 19 and 23")
```

Runnable example: [`examples/strands_agent.py`](../examples/strands_agent.py).

`strands-agents` is **not** a dependency of localcore-gateway and is
deliberately kept out of its lockfile — install it in the environment where
the agent runs (`uv pip install "strands-agents"`). The gateway and the agent
are separate processes; they only share the MCP URL. To promote to real AWS,
set the gateway URL and pass `headers={"Authorization": f"Bearer {token}"}`
(refresh before expiry); the agent code is otherwise identical.

## Generic MCP client config

Most MCP clients accept a Streamable HTTP server entry:

```json
{
  "mcpServers": {
    "localcore-gateway": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8080/mcp"
    }
  }
}
```

## Tool discovery

`tools/list` returns the aggregated catalog (all `<target>___<tool>` names
with their JSON Schemas). Agents discover tools from that list.

AgentCore's builtin semantic tool-search tool
(`x_amz_bedrock_agentcore_search`) is **not implemented** here — it was
intentionally omitted. Agents that depend on it on real AWS should not assume
it exists against this local gateway.

## Authentication

The local gateway has **no inbound authentication** — it is a local dev tool.
Bind it to loopback (the default `127.0.0.1`). If you must expose it (LAN,
shared box), put it behind your own reverse proxy / auth; do not rely on the
gateway itself to gate access.

This does not affect the "no code change" promise: real AgentCore enforces
OAuth/JWT, the agent sends a bearer token there, and the local gateway simply
ignores any token — so the same client works against both.

## Promote to real AWS

When the integration works locally:

1. Create the real gateway + Lambda target in AgentCore (console / toolkit /
   API).
2. Repoint the agent's MCP URL to the AWS gateway endpoint and supply the real
   OAuth/JWT token.
3. Tool names and call shapes are unchanged, because this gateway reproduces
   the AgentCore contract (`target___tool`, args-as-event,
   `bedrockAgentCoreToolName`, return-as-result).
