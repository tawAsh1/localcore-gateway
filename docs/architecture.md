# Architecture

## What this is

A local stand-in for **AWS Bedrock AgentCore Gateway**. The gateway is, on the
wire, just an MCP server (Streamable HTTP) that aggregates many *targets* into
one tool catalog and translates MCP tool calls into backend invocations. We
reproduce that contract locally, with a pluggable local Lambda backend.

We do **not** hand-roll the MCP protocol — the wire surface is provided by
[FastMCP](https://github.com/jlowin/fastmcp) 3.x. Our code is a thin
AgentCore-compat layer on top.

## Request flow

```
MCP client / agent
   │  MCP Streamable HTTP  (POST /mcp, JSON-RPC)
   ▼
FastMCP server  (no inbound auth — local dev tool)
   │  tools/list  → aggregated catalog: "<target>___<tool>"
   │  tools/call  → GatewayTool.run(arguments)
   ▼
GatewayTool.dispatch  →  Target.call_tool(tool, arguments)
   ▼
LambdaTarget
   │  event          = tool arguments
   │  client_context = {bedrockAgentCoreToolName: <tool>, ...}
   ▼
LambdaInvoker  ──┬── native  (subprocess worker per target, no Docker)
                 └── sam     (sam local start-lambda → real Lambda runtime)
   ▼
return value  →  MCP tool result   (errors → MCP isError / ToolError)
```

## AgentCore contract mapping

| AgentCore Gateway | Here |
|---|---|
| MCP Streamable HTTP at `/mcp` | `FastMCP.http_app(path="/mcp", stateless_http=True, json_response=True)` |
| Tool naming `target___tool` | `gateway.NAME_SEP = "___"`, one MCP tool per `(target, tool)` |
| Lambda target: args as event | `LambdaTarget.call_tool` passes `arguments` as the Lambda `event` |
| `context.client_context.custom['bedrockAgentCoreToolName']` | injected by `LambdaTarget` (plus `bedrockAgentCoreGatewayId`, `bedrockAgentCoreTargetName`) |
| Lambda return → tool result | `gateway._to_tool_result` (dict → structured content) |
| `toolSchema.inlinePayload` | each `ToolSpec.input_schema` in config |
| Inbound authorizer (OAuth/JWT \| IAM) | **not implemented** — no inbound auth (local dev tool) |
| `x_amz_bedrock_agentcore_search` | **not implemented** (intentionally omitted) |

## Component map

| Module | Responsibility |
|---|---|
| `localcore_gateway.config` | YAML → validated pydantic models (`GatewayConfig`, …) |
| `localcore_gateway.gateway` | builds the FastMCP server, `GatewayTool`, target aggregation |
| `localcore_gateway.targets.base` | `Target` interface, `ToolDef`, `ToolOutcome` |
| `localcore_gateway.targets.lambda_target` | AgentCore MCP ↔ Lambda translation |
| `localcore_gateway.targets.openapi_target` | OpenAPI → MCP (FastMCP engine, verbatim `operationId` naming, outbound auth) |
| `localcore_gateway.lambda_emu.base` | `LambdaInvoker` interface, `make_invoker` factory |
| `localcore_gateway.lambda_emu.native` | subprocess-worker manager (default) |
| `localcore_gateway.lambda_emu._worker` | the per-target subprocess runtime |
| `localcore_gateway.lambda_emu.sam` | drives `sam local start-lambda` |
| `localcore_gateway.app` | ASGI app assembly + uvicorn `--factory` entrypoint |
| `localcore_gateway.__main__` | `lcgw` CLI |

## Design decisions

- **FastMCP 3.x, pinned `>=3.2,<3.3`.** The MCP/aggregation surface is reused,
  not reimplemented. Pinned because the 3.x API moves fast.
- **One `LambdaInvoker` interface, two backends.** `native` runs one
  subprocess worker per target (no Docker, real `sys.path`/`sys.modules`
  isolation → monorepo-safe, hard timeout); `sam` gives full Linux-runtime
  fidelity. Switch per target via config.
- **Tools registered directly, not via FastMCP mount/namespace.** Each
  `(target, tool)` becomes a `GatewayTool` named `target___tool` with the
  tool's explicit JSON Schema and a closure that dispatches into the target.
  This gives exact AgentCore naming with no dependence on FastMCP's namespace
  separator internals.

## Known limitations

- `native` is process-isolated but **not a security sandbox** (no
  filesystem/network jail); it serializes invokes per target (one warm
  environment — no concurrent-environment scaling).
- `sam` per-invoke logs surface in the `sam local` console (out-of-band for
  the Invoke API), so that backend reports only an invoke summary.
- AgentCore's builtin semantic tool search
  (`x_amz_bedrock_agentcore_search`) is intentionally not implemented.
- Lambda and OpenAPI target types are implemented; MCP-passthrough and
  Smithy are not. OpenAPI reuses FastMCP's spec→HTTP engine but overrides
  naming to the verbatim `operationId` for AgentCore fidelity; outbound auth
  is static API key (header/query) or bearer only (no OAuth 2LO).
