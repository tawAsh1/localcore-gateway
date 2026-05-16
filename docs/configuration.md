# Configuration reference

The gateway is driven by one YAML file (`-c path/to/config.yaml`). It is parsed
into validated pydantic models (`localcore_gateway.config`). Paths are resolved relative to
the **config file's directory** unless absolute.

Full working example: [`examples/config.yaml`](../examples/config.yaml).

## Top level (`GatewayConfig`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `server` | object | see below | HTTP server / MCP endpoint |
| `auth` | object | `{mode: none}` | inbound authorizer |
| `targets` | list | `[]` | gateway targets (currently `lambda` only) |

## `server` (`ServerConfig`)

| Key | Type | Default |
|---|---|---|
| `name` | string | `localcore-gateway` |
| `host` | string | `127.0.0.1` |
| `port` | int | `8080` |
| `path` | string | `/mcp` |

The MCP endpoint is `http://{host}:{port}{path}`.

## `auth` (`AuthConfig`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `mode` | `none` \| `jwt` | `none` | `none` = no inbound auth (dev default) |
| `jwks_uri` | string | – | JWKS endpoint (jwt) |
| `issuer` | string | – | expected `iss` (jwt) |
| `audience` | string | – | expected `aud` (jwt) |

Validation: `mode: jwt` requires at least `jwks_uri` **or** `issuer`. JWT
verification is delegated to FastMCP's `JWTVerifier`. See
[connecting-agents.md](connecting-agents.md#authentication).

## A target (`LambdaTargetConfig`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `type` | `lambda` | `lambda` | only Lambda is implemented |
| `name` | string | required | tools are exposed as `<name>___<tool>` |
| `lambda` | object | required | the Lambda behind this target (below) |
| `tools` | list | required | tools this target backs (below) |

## `lambda` (`LambdaFunctionConfig`)

| Key | Type | Default | Applies to |
|---|---|---|---|
| `backend` | `native` \| `sam` | `native` | – |
| `handler` | string | – | **native** (required): `module.func` or `path/to/file.py:func` |
| `code_root` | string | config file dir | **native**: dir prepended to `sys.path` |
| `sam_endpoint` | string | `http://127.0.0.1:3001` | **sam** |
| `sam_function` | string | – | **sam** (required): logical name in the SAM template |
| `function_name` | string | `local-function` | both (→ `context.function_name`) |
| `memory_mb` | int | `128` | both (→ `context.memory_limit_in_mb`) |
| `timeout_sec` | float | `30.0` | both (native = soft timeout) |
| `env` | map<str,str> | `{}` | both (process env during invoke) |
| `region` | string | `us-east-1` | both (ARN / `AWS_REGION`) |

Validation: `backend: native` requires `handler`; `backend: sam` requires
`sam_function`.

## A tool (`ToolSpec`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | un-prefixed tool name |
| `description` | string | `""` | shown in `tools/list` |
| `inputSchema` | object | `{"type":"object","properties":{}}` | JSON Schema for the tool's arguments (AgentCore `toolSchema.inlinePayload`) |

`inputSchema` may also be written as `input_schema`.

## Minimal example

```yaml
server: { port: 8080 }
targets:
  - type: lambda
    name: demo
    lambda:
      backend: native
      handler: handlers.handler   # examples/handlers.py:handler
    tools:
      - name: add
        description: Add two numbers.
        inputSchema:
          type: object
          properties: { a: { type: number }, b: { type: number } }
          required: [a, b]
```

Exposed as MCP tool `demo___add`.
