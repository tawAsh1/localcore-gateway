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
| `SynchronizeGatewayTargets` (`PUT /gateways/{id}/synchronize`, 202 + async) | `POST /-/sync` / `lcgw sync` — **synchronous**, returns the per-target diff directly |

The admin surface (`POST /-/sync`, `GET /-/invocations` — invocation history
for `lcgw tail`) lives outside the MCP path on the same app. It is local-only
and unauthenticated by design (see SECURITY.md); AgentCore's control plane is
a separate API, so it has no wire-level analog.

## Component map

| Module | Responsibility |
|---|---|
| `localcore_gateway.config` | YAML → validated pydantic models (`GatewayConfig`, …) |
| `localcore_gateway.gateway` | builds the FastMCP server, `GatewayTool`, target aggregation |
| `localcore_gateway.targets.base` | `Target` interface, `ToolDef`, `ToolOutcome` |
| `localcore_gateway.targets.lambda_target` | AgentCore MCP ↔ Lambda translation |
| `localcore_gateway.targets.openapi_target` | OpenAPI → MCP (FastMCP engine, verbatim `operationId` naming, outbound auth) |
| `localcore_gateway.targets.mcp_target` | MCP-passthrough: proxies another MCP server (streamable HTTP, or local stdio as a convenience), verbatim tool names |
| `localcore_gateway.targets.aws_gateway_target` | proxies a REAL deployed AgentCore Gateway (hybrid debugging): MCPTarget subclass, un-prefixed verbatim names, bearer/SigV4 auth |
| `localcore_gateway.targets.mock_target` | mock target: config-declared tools with canned responses/errors (local-only, no AWS analog) |
| `localcore_gateway.lambda_emu.base` | `LambdaInvoker` interface, `make_invoker` factory |
| `localcore_gateway.lambda_emu.native` | subprocess-worker manager (default) |
| `localcore_gateway.lambda_emu._worker` | the per-target subprocess runtime |
| `localcore_gateway.lambda_emu.sam` | drives `sam local start-lambda` |
| `localcore_gateway.lambda_emu.aws` | invokes a REAL deployed function via boto3 (`aws` extra) |
| `localcore_gateway.aws_deps` | optional-`aws`-extra gate (actionable error when boto3 is missing) |
| `localcore_gateway.history` | in-memory invocation ring buffer (`lcgw tail` backend) |
| `localcore_gateway.testing` | **public** pytest helpers: `serve_gateway` / `call_tool` / `serve_asgi` (see [testing.md](testing.md)) |
| `localcore_gateway.app` | ASGI app assembly (MCP endpoint + `/-/sync`, `/-/invocations` admin routes) + uvicorn `--factory` entrypoint |
| `localcore_gateway.__main__` | `lcgw` CLI |

## Design decisions

- **FastMCP 3.x, pinned `>=3.2,<3.3`.** The MCP/aggregation surface is reused,
  not reimplemented. Pinned because the 3.x API moves fast.
- **One `LambdaInvoker` interface, three backends.** `native` runs one
  subprocess worker per target (no Docker, real `sys.path`/`sys.modules`
  isolation → monorepo-safe, hard timeout); `sam` gives full Linux-runtime
  fidelity; `aws` invokes the real deployed function (hybrid debugging;
  boto3 via the optional `aws` extra, retries off so a side-effecting invoke
  is never silently retried). Switch per target via config.
- **Tools registered directly, not via FastMCP mount/namespace.** Each
  `(target, tool)` becomes a `GatewayTool` named `target___tool` with the
  tool's explicit JSON Schema and a closure that dispatches into the target.
  This gives exact AgentCore naming with no dependence on FastMCP's namespace
  separator internals. One exception: `aws-gateway` passthrough targets set
  `Target.prefix_tools = False` and register the remote gateway's
  already-prefixed names **verbatim** (re-prefixing would double them);
  build and `lcgw sync` reject name collisions instead of shadowing.
- **boto3 is optional.** The core gateway stays AWS-SDK-free; the real-AWS
  passthrough features gate their imports through
  `localcore_gateway.aws_deps` so a missing extra fails with the install
  command in the message.
- **Contract checks are ours, opt-in, and uniform.** The real gateway
  validates neither arguments nor results, so `server.contract_checks`
  defaults to off. The MCP SDK's wire layer would independently hard-error
  on output-schema violations; the gateway bypasses that (results are sent
  as full CallToolResults — viable because fastmcp is pinned `<3.3`) so the
  default stays faithful and one switch controls both sides. Note the
  python MCP SDK's *client* also validates results on its own — that's the
  consuming agent's stack, out of the gateway's hands.

## Known limitations

- `native` is process-isolated but **not a security sandbox** (no
  filesystem/network jail); it serializes invokes per target (one warm
  environment — no concurrent-environment scaling).
- `sam` per-invoke logs surface in the `sam local` console (out-of-band for
  the Invoke API), so that backend reports only an invoke summary.
- AgentCore's builtin semantic tool search
  (`x_amz_bedrock_agentcore_search`) is intentionally not implemented.
- Lambda, OpenAPI, and MCP-passthrough target types are implemented (plus
  local-only mock targets); Smithy is not. OpenAPI reuses FastMCP's
  spec→HTTP engine but overrides naming to the verbatim `operationId` for
  AgentCore fidelity; outbound auth is static API key (header/query) or
  bearer only (no OAuth 2LO).
- The hybrid features (`type: aws-gateway`, `lambda.backend: aws`) talk to
  real AWS and have no AgentCore analog as *local* concepts; they need the
  `aws` extra and credentials, and inherit AWS-side behavior (cold starts,
  IAM, service quotas) that is out of this project's hands.
- MCP-passthrough keeps one persistent upstream `Client` session (opened
  lazily on first call); a call that hits a dead session (upstream
  restart/dropped connection) fails as a tool error, then the session is
  re-opened on a later call. Upstream tool-list changes are picked up via
  `lcgw sync`, not automatically. Outbound auth (streamable HTTP mode) reuses
  the OpenAPI targets' engine — static headers, bearer, or an API key in a
  header/query param. The stdio `command` mode is a local-only convenience
  with no AgentCore analog.
- Invocation history (`lcgw tail`) is an in-memory ring buffer
  (`server.history` entries, 4 KB per args/payload preview) — no
  persistence, gone on restart.
