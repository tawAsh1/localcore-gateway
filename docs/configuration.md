# Configuration reference

The gateway is driven by one YAML file (`-c path/to/config.yaml`). It is parsed
into validated pydantic models (`localcore_gateway.config`). Paths are resolved relative to
the **config file's directory** unless absolute.

Full working example: [`examples/config.yaml`](../examples/config.yaml).

## Top level (`GatewayConfig`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `server` | object | see below | HTTP server / MCP endpoint |
| `targets` | list | `[]` | gateway targets; each is `type: lambda` or `type: openapi` (mixable) |

## `server` (`ServerConfig`)

| Key | Type | Default |
|---|---|---|
| `name` | string | `localcore-gateway` |
| `host` | string | `127.0.0.1` |
| `port` | int | `8080` |
| `path` | string | `/mcp` |

The MCP endpoint is `http://{host}:{port}{path}`.

There is **no inbound authentication** (this is a local dev tool). Bind to
loopback only; front it with your own proxy/auth if you must expose it. See
[connecting-agents.md](connecting-agents.md#authentication).

`targets` is a list; each entry is discriminated by `type` (`lambda` or
`openapi`). You can mix both.

## A Lambda target (`type: lambda`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `type` | `lambda` | `lambda` | |
| `name` | string | required | tools are exposed as `<name>___<tool>` |
| `lambda` | object | required | the Lambda behind this target (below) |
| `tools` | list | `[]` | inline tool specs (below) |
| `tool_schema_file` | string | – | path to a JSON file of tool specs (AgentCore `toolSchema.inlinePayload` shape). Merged with `tools`; inline wins on name clash. Relative to the config dir |

At least one of `tools` / `tool_schema_file` is required.

## An OpenAPI target (`type: openapi`)

A REST API's OpenAPI spec becomes MCP tools. **Faithful to AgentCore**: the
tool name is each operation's `operationId` **verbatim** (operationId is
**required** on every operation — a missing one is a config error), and the
spec's own `securitySchemes` are ignored — outbound auth is configured here.
FastMCP does the spec→HTTP translation; OpenAPI 3.0/3.1, JSON-centric (same
support envelope as the real gateway — `oneOf`/`anyOf`/`allOf` and complex
parameter serializers are unsupported upstream).

| Key | Type | Default | Notes |
|---|---|---|---|
| `type` | `openapi` | `openapi` | |
| `name` | string | required | tools are `<name>___<operationId>` |
| `spec` | object | – | inline OpenAPI 3.0/3.1 spec |
| `spec_file` | string | – | path to a spec (JSON/YAML), relative to the config dir |
| `base_url` | string | spec `servers[0].url` | override the API base URL |
| `timeout_sec` | float | `30.0` | per-request timeout |
| `auth` | object | `{type: none}` | outbound auth (below) |

Exactly one of `spec` / `spec_file` is required.

### `auth` (`OpenAPIAuthConfig`) — outbound

| Key | Type | Default | Notes |
|---|---|---|---|
| `type` | `none` \| `apikey` \| `bearer` | `none` | |
| `in` | `header` \| `query` | `header` | where the API key goes (`apikey`) |
| `name` | string | `X-API-Key` | header/query param name (`apikey`) |
| `value` | string | – | the key / token (required for `apikey` / `bearer`) |

`bearer` sends `Authorization: Bearer <value>`. OAuth 2LO is not supported
(intentionally out of scope locally).

```yaml
targets:
  - type: openapi
    name: weather
    spec_file: openapi.yaml
    auth: { type: apikey, in: header, name: X-API-Key, value: "${KEY}" }
```

## `lambda` (`LambdaFunctionConfig`)

| Key | Type | Default | Applies to |
|---|---|---|---|
| `backend` | `native` \| `sam` | `native` | – |
| `handler` | string | – | **native** (required): `module.func` or `path/to/file.py:func` |
| `code_root` | string \| list[string] | config file dir | **native**: dir(s) prepended to `sys.path`. Relative paths resolve against the config dir |
| `python` | string | gateway's interpreter | **native**: Python executable for this target's worker (a path relative to the config dir, or a PATH command like `python3.12`). Path form is made absolute but **symlinks are not followed** (a venv's `bin/python` is a symlink — following it would lose the venv). Lets each target run under its own venv → its own deps + version |
| `sam_endpoint` | string | `http://127.0.0.1:3001` | **sam** |
| `sam_function` | string | – | **sam** (required): logical name in the SAM template |
| `function_name` | string | `local-function` | both (→ `context.function_name`) |
| `memory_mb` | int | `128` | both (→ `context.memory_limit_in_mb`) |
| `timeout_sec` | float | `30.0` | both (native hard-kills the worker on timeout) |
| `env` | map<str,str> | `{}` | both (process env during invoke) |
| `env_file` | string | – | **native**: `.env`-style file merged into the invoke env; `env` overrides it. Relative to the config dir |
| `region` | string | `us-east-1` | both (ARN / `AWS_REGION`) |

Validation: `backend: native` requires `handler`; `backend: sam` requires
`sam_function`.

## A tool (`ToolSpec`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | un-prefixed tool name |
| `description` | string | `""` | shown in `tools/list` |
| `inputSchema` | object | `{"type":"object","properties":{}}` | JSON Schema for the tool's arguments (AgentCore `toolSchema.inlinePayload`) |
| `outputSchema` | object | – | optional output schema (AgentCore `ToolDefinition.outputSchema`); advertised via MCP `tools/list` |

`inputSchema` / `outputSchema` may also be written as `input_schema` /
`output_schema`.

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
