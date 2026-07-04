# Configuration reference

The gateway is driven by one YAML file (`-c path/to/config.yaml`). It is parsed
into validated pydantic models (`localcore_gateway.config`). Paths are resolved relative to
the **config file's directory** unless absolute.

Full working example: [`examples/config.yaml`](../examples/config.yaml).

## Environment variable expansion

`${VAR}` in any string value is replaced with the environment variable `VAR`
when the config is loaded — the local analog of AgentCore's credential
providers: secrets stay out of the config file. A reference to an **unset**
variable is a config error (so a placeholder is never sent as a credential).
Escape with `$${VAR}` to get a literal `${VAR}`; bare `$VAR` (no braces) is
left untouched. Expansion applies to the config file itself only — not to
files it references (`spec_file`, `tool_schema_file`, `env_file`).

## Top level (`GatewayConfig`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `server` | object | see below | HTTP server / MCP endpoint |
| `targets` | list | `[]` | gateway targets; each is `type: lambda`, `type: openapi`, or `type: mcp` (mixable) |

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

`targets` is a list; each entry is discriminated by `type` (`lambda`,
`openapi`, or `mcp`). You can mix any of these.

## A Lambda target (`type: lambda`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `type` | `lambda` | `lambda` | |
| `name` | string | required | tools are exposed as `<name>___<tool>` |
| `lambda` | object | required | the Lambda behind this target (below) |
| `tools` | list | `[]` | inline tool specs (below) |
| `tool_schema_file` | string | – | path to a JSON file of tool specs (AgentCore `toolSchema.inlinePayload` shape) — a **list** or a **single** tool-spec object. Merged with `tools`; inline wins on name clash. Relative to the config dir |

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
    # ${KEY} is expanded from the environment at load time (see
    # "Environment variable expansion" above).
    auth: { type: apikey, in: header, name: X-API-Key, value: "${KEY}" }
```

## An MCP-passthrough target (`type: mcp`)

Another MCP server's tools, proxied. Remote tool names are used **verbatim**;
the gateway adds the `<name>___` prefix uniformly (same as the other target
types). **Faithful to AgentCore** for the `url` mode: the real gateway only
ever speaks streamable HTTP to the upstream server. The `command` (stdio)
mode is a **local-only convenience** with no AWS analog — it spawns a local
MCP server subprocess instead of requiring one to already be listening over
HTTP.

Exactly one of `url` / `command` is required.

| Key | Type | Default | Notes |
|---|---|---|---|
| `type` | `mcp` | `mcp` | |
| `name` | string | required | tools are `<name>___<tool>` |
| `url` | string | – | upstream MCP server endpoint (streamable HTTP). Mutually exclusive with `command` |
| `headers` | map<str,str> | `{}` | static headers sent with every request (`url` mode only) |
| `auth` | object | `{type: none}` | outbound auth (below); `url` mode only |
| `command` | string | – | command to spawn a local MCP server over stdio: a PATH command (e.g. `python3`), or a path resolved against the config dir with symlinks **not** followed (same rule as `lambda.python` — a venv's `bin/python` must stay a venv path). Mutually exclusive with `url` |
| `args` | list[string] | `[]` | arguments passed to `command` (`command` mode only). Opaque to the gateway: script paths in here are resolved by the **child**, relative to its `cwd` |
| `env` | map<str,str> | `{}` | extra environment variables for the subprocess (`command` mode only) |
| `env_file` | string | – | `.env`-style file (KEY=VALUE per line) merged into the subprocess env; `env` overrides it (`command` mode only). Relative to the config dir |
| `cwd` | string | – | subprocess working directory (`command` mode only), relative to the config dir; **defaults to the config dir** |
| `timeout_sec` | float | `30.0` | per-request timeout |
| `tools` | list[string] | `[]` | optional allowlist of upstream tool names to expose; unlisted tools are hidden. A name not found on the upstream server is a config-time error |

Validation rejects mixed-mode fields rather than silently ignoring them:
`headers` / `auth` require `url` (stdio has no HTTP headers/auth); `env` /
`env_file` / `cwd` require `command` (not applicable to `url`).

**Subprocess environment (`command` mode):** the child does **not** inherit
the gateway's full environment. The MCP SDK spawns it with only a safe
default subset (`HOME`, `PATH`, `SHELL`, `TERM`, `USER`, `LOGNAME` on POSIX),
plus `env_file` then inline `env` merged on top (inline wins). Anything else
the server needs must be passed explicitly.

### `auth` (`OpenAPIAuthConfig`) — outbound, `url` mode only

Reuses the same shape **and** engine as the OpenAPI target's `auth` (see
above): bearer, or a static API key in a header or query param. It is applied
as an `httpx.Auth` on every request the MCP client transport makes, so
query-param keys work here too.

```yaml
targets:
  # remote, streamable HTTP (AgentCore-faithful)
  - type: mcp
    name: mytools
    url: http://127.0.0.1:9000/mcp
    auth: { type: bearer, value: "${TOKEN}" }   # expanded from the environment
    tools: [tool_a, tool_b]   # optional allowlist

  # local, stdio (convenience only; no AWS analog)
  - type: mcp
    name: localtools
    command: python       # PATH command; a path would resolve against this file's dir
    args: [server.py]     # run by the child in its cwd (default: this file's dir)
    env: { FOO: bar }
    env_file: local.env   # optional; inline `env` wins
```

**Known limitations:**

- Invocation keeps one persistent upstream session (opened lazily on first
  call). If it dies (upstream restart, dropped connection), the call that
  hits the dead session fails — surfaced as a tool error — and the session is
  re-opened on a later call once the client has noticed the death.
- Tool discovery happens once, at construction, connecting and disconnecting
  separately from the persistent invocation session; for `command` (stdio)
  mode this means the subprocess is spawned twice (once at startup for
  discovery, once lazily on first call for invocation). Upstream tool-list
  changes after startup are not picked up — restart the gateway.

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
