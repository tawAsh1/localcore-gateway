# Connecting agents / MCP clients

The local gateway speaks the same wire protocol as the real AgentCore Gateway:
**MCP over Streamable HTTP**. Any MCP client points at it the same way it would
point at the AWS endpoint — only the URL changes.

```
local : http://127.0.0.1:8080/mcp
AWS   : https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
```

Tool names are identical in both (`<target>___<tool>`), so swapping environments
needs **no code change** in the agent — just the endpoint URL and the auth
token.

## FastMCP / Python client

```python
from fastmcp import Client

async with Client("http://127.0.0.1:8080/mcp") as c:
    tools = await c.list_tools()
    res = await c.call_tool("demo___add", {"a": 2, "b": 40})
    print(res.structured_content)   # {'sum': 42.0}
```

With JWT auth enabled, pass a bearer token via the client's auth/headers
mechanism (e.g. `Authorization: Bearer <token>`).

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

| `auth.mode` | Behaviour | When |
|---|---|---|
| `none` | no inbound auth | local dev (default) |
| `jwt` | bearer JWT verified by FastMCP `JWTVerifier` (`jwks_uri` / `issuer` / `audience` from config) | mirroring AgentCore's OAuth/JWT authorizer |

For `jwt`, point `jwks_uri`/`issuer`/`audience` at your local IdP (Cognito
local, Keycloak, a static JWKS, …) and send `Authorization: Bearer <token>`.
See [configuration.md](configuration.md#auth-authconfig).

## Promote to real AWS

When the integration works locally:

1. Create the real gateway + Lambda target in AgentCore (console / toolkit /
   API).
2. Repoint the agent's MCP URL to the AWS gateway endpoint and supply the real
   OAuth/JWT token.
3. Tool names and call shapes are unchanged, because this gateway reproduces
   the AgentCore contract (`target___tool`, args-as-event,
   `bedrockAgentCoreToolName`, return-as-result).
