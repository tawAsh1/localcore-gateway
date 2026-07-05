# Configuration reference

The gateway is driven by one YAML file (`-c path/to/config.yaml`). It is parsed
into validated pydantic models (`localcore_gateway.config`). Paths are resolved relative to
the **config file's directory** unless absolute.

Full working example: [`examples/config.yaml`](../examples/config.yaml).

Editor support: `lcgw schema --output gateway.schema.json` emits this file's
JSON Schema; point the yaml-language-server at it with a
`# yaml-language-server: $schema=./gateway.schema.json` modeline for
completion and inline validation. `lcgw preflight` additionally checks a
config against real-AgentCore deploy constraints (see [cli.md](cli.md)).

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
| `targets` | list | `[]` | gateway targets; each is `type: lambda`, `type: openapi`, `type: mcp`, `type: aws-gateway`, or `type: mock` (mixable) |

## `server` (`ServerConfig`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | `localcore-gateway` | |
| `host` | string | `127.0.0.1` | |
| `port` | int | `8080` | |
| `path` | string | `/mcp` | |
| `stateless` | bool | `false` | serving mode (below). **Default changed in 0.x**: sessions + SSE |
| `history` | int | `1000` | invocation-history ring buffer size (backs `lcgw tail` / `GET /-/invocations`) |
| `contract_checks` | `off` \| `warn` \| `error` | `off` | validate tool arguments/results against the declared JSON Schemas (below) |

### `stateless` — serving mode

Since May 2026 the real gateway maintains **stateful MCP sessions**
(`Mcp-Session-Id` issued on initialize) and **streams responses over SSE**
([sessions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-sessions.html)).
`stateless: false` (the default) reproduces that: sessions via the MCP SDK's
session manager, SSE responses, and it is what mid-call progress/logging
notifications and elicitation/sampling passthrough need to actually stream.
`stateless: true` restores the pre-May-2026 behavior this project shipped
with previously — buffered JSON responses, no sessions (mid-call
notifications are buffered away; elicitation/sampling passthrough is
rejected).

> **Breaking default (0.x):** older versions always served stateless.
> Set `server: { stateless: true }` to restore the old behavior exactly.

Fidelity notes: AWS scopes sessions per authenticated user with a 1-hour
default timeout; this local gateway has no inbound auth, so sessions are
the SDK's in-memory ones — no per-user scoping or timeout emulation.

### `contract_checks` — catching schema drift locally

A local dev aid, **off by default for fidelity**: the real gateway validates
neither arguments nor results, and neither does this stack by default (note:
the python MCP SDK's *client* validates results on its own — that's the
consumer's stack, not the gateway). With checks on, every invocation is
validated at the gateway — arguments against `inputSchema` before dispatch,
successful results against `outputSchema` (when declared) after:

- `warn` — the payload still passes through, but a WARNING is logged and the
  invocation record gets a `contract_violation` field (`lcgw tail` marks the
  line with `[contract: ...]`).
- `error` — the call returns the standard error envelope with
  `errorType: "ContractViolation"` instead of the result (argument
  violations short-circuit before the backend is invoked).

The MCP endpoint is `http://{host}:{port}{path}`. The server also exposes a
small local **admin surface** outside the MCP path — `POST /-/sync` (target
re-sync) and `GET /-/invocations` (invocation history) — used by `lcgw sync`
/ `lcgw tail` (see [cli.md](cli.md)). It is local-only and unauthenticated by
design, and has no AWS analog (AgentCore's control plane is a separate API).

There is **no inbound authentication** (this is a local dev tool). Bind to
loopback only; front it with your own proxy/auth if you must expose it. See
[connecting-agents.md](connecting-agents.md#authentication).

`targets` is a list; each entry is discriminated by `type` (`lambda`,
`openapi`, `mcp`, `aws-gateway`, or `mock`). You can mix any of these.

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

Another MCP server's **whole catalog**, proxied: tools, prompts
(`prompts/list`), and resources (`resources/list` +
`resources/templates/list`) — the same set the real gateway indexes from an
MCP server target
([devguide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html)).
Remote tool names are used **verbatim**; the gateway adds the `<name>___`
prefix uniformly (same as the other target types). **Faithful to AgentCore**
for the `url` mode: the real gateway only ever speaks streamable HTTP to the
upstream server. The `command` (stdio) mode is a **local-only convenience**
with no AWS analog — it spawns a local MCP server subprocess instead of
requiring one to already be listening over HTTP.

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
| `tools` | list[string] | `[]` | optional allowlist of upstream tool names to expose; unlisted tools are hidden. A name not found on the upstream server is a config-time error. **Tools only** — prompts/resources are never filtered |
| `resource_priority` | int | `100` | AgentCore `resourcePriority` analog: when several targets expose the same resource URI, the **lowest** value serves it (below) |

Validation rejects mixed-mode fields rather than silently ignoring them:
`headers` / `auth` require `url` (stdio has no HTTP headers/auth); `env` /
`env_file` / `cwd` require `command` (not applicable to `url`).

### Prompts & resources passthrough

If the upstream advertises the capabilities, its prompts and resources
(including templates) join the catalog; upstreams without them behave
exactly as before. `prompts/get` and `resources/read` are proxied **live**
to the upstream over the same persistent session as tools.

- **Prompts** are named `<name>___<prompt>` — the convention AWS documents
  for prompts as well as tools
  ([prompts/get](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using-mcp-prompts-get.html)).
  Prompt-name collisions are errors, like tool names.
- **Resource URIs are verbatim** — AWS: "The original URI from the MCP
  server is returned as-is"
  ([resources/list](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using-mcp-resources-list.html)).
  When several targets expose the **same URI**, the target with the lowest
  `resource_priority` serves it, per AWS's documented `resourcePriority`
  routing
  ([resources/read](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using-mcp-resources-read.html)).
  AWS documents no default value (we use `100`) and no tie-break (we serve
  the first target in config order and log a WARNING). Ownership is
  re-evaluated on `lcgw sync`.
- **Caution (from AWS's docs, and true here too):** resource URIs come from
  the downstream server unvalidated — don't auto-fetch untrusted URIs, they
  can point anywhere (SSRF).

**Subprocess environment (`command` mode):** the child does **not** inherit
the gateway's full environment. The MCP SDK spawns it with only a safe
default subset (`HOME`, `PATH`, `SHELL`, `TERM`, `USER`, `LOGNAME` on POSIX),
plus `env_file` then inline `env` merged on top (inline wins). Anything else
the server needs must be passed explicitly.

### Mid-call passthrough: progress, logging, elicitation, sampling

Matching the real gateway, an MCP target relays the interactive parts of a
tool call, not just the result:

- **Progress notifications**
  ([progress](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-mcp-progress.html)):
  upstream `ctx.report_progress` re-emits to the calling client as it
  arrives, keyed to the caller's own `progressToken` (no token → dropped,
  standard MCP semantics).
- **Logging notifications**
  ([logging](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-mcp-logging.html)):
  upstream log messages forward to the caller with their level preserved.
- **Elicitation**
  ([elicitation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-mcp-elicitation.html)):
  a downstream server's form-mode elicitation request travels up to YOUR
  client (raw `requestedSchema`, forwarded 1:1) and the answer back down.
  **URL-mode elicitation is not supported** (documented limitation).
- **Sampling**
  ([sampling](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-mcp-sampling.html)):
  a downstream `sampling/createMessage` request is relayed to your client's
  LLM and the completion returned downstream.

These need the default session/SSE serving mode (`server.stateless: false`,
above): in stateless mode notifications are buffered away and
elicitation/sampling requests fail with a "requires sessions" error. Outside
a gateway request (`lcgw invoke`, direct target calls) notifications are
dropped silently. With **concurrent** calls through one target, upstream
log/elicitation/sampling traffic routes to the most recent in-flight caller
(a local simplification; the real gateway scopes by user session).

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
- Discovery (tools, prompts, resources) happens at construction, connecting
  and disconnecting separately from the persistent invocation session; for
  `command` (stdio) mode this means the subprocess is spawned twice (once at
  startup for discovery, once lazily on first call for invocation). Upstream
  catalog changes after startup are not picked up automatically — run
  `lcgw sync` (the SynchronizeGatewayTargets analog; see [cli.md](cli.md))
  to re-discover without a restart.
- The invocation history (`lcgw tail`) records tool calls only;
  `prompts/get` and `resources/read` are not recorded.

## An AWS-gateway passthrough target (`type: aws-gateway`)

A **real deployed AgentCore Gateway**, proxied into the local one. No AWS
analog as a target type — it exists purely for **hybrid debugging**: run the
one target you're developing locally (any type above) while every other tool
of your production toolset passes through to the deployed gateway, all
behind one local MCP endpoint your agent points at. Requires the `aws` extra
for `sigv4` auth (`pip install 'localcore-gateway[aws]'`).

**Naming:** the deployed gateway's tools and prompts already carry
AgentCore's `remoteTarget___name` form and are exposed **verbatim** — no
local `<name>___` prefix (re-prefixing would double it). `name` is for
identification/logging only (`lcgw sync --target`, log lines). A tool or
prompt name collision with any other target is an error: at build a
`ValueError`, at `lcgw sync` that target's error in the response (resolve
the collision and restart). Resource URIs follow the same priority routing
as `mcp` targets (above).

| Key | Type | Default | Notes |
|---|---|---|---|
| `type` | `aws-gateway` | `aws-gateway` | |
| `name` | string | required | identification/logging only; tools are NOT prefixed |
| `url` | string | required | the deployed gateway's MCP endpoint (streamable HTTP) |
| `headers` | map<str,str> | `{}` | static headers sent with every request |
| `auth` | object | `{type: none}` | `bearer` or `sigv4` (below) |
| `timeout_sec` | float | `30.0` | per-request timeout |
| `tools` | list[string] | `[]` | optional allowlist of remote tool names (already-prefixed form). Tools only — prompts/resources are never filtered |
| `resource_priority` | int | `100` | same semantics as on `mcp` targets |

Everything else (eager discovery of tools/prompts/resources, one persistent
re-opened session, `lcgw sync` re-discovery, error mapping) behaves exactly
like an `mcp` target.

### `auth` (`AWSGatewayAuthConfig`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `type` | `none` \| `bearer` \| `sigv4` | `none` | |
| `value` | string | – | the token (required for `bearer`) — OAuth/JWT-configured gateways |
| `region` | string | profile chain's region | `sigv4`: signing region; no region anywhere is a startup error |
| `profile` | string | default credential chain | `sigv4`: AWS profile |

`sigv4` (IAM-configured gateways) SigV4-signs every request for the
`bedrock-agentcore` service via botocore, body hash included.

```yaml
targets:
  - type: aws-gateway
    name: prod
    url: https://<gateway-id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp
    auth: { type: sigv4, region: us-east-1 }
```

Full hybrid workflow example: [`examples/hybrid_config.yaml`](../examples/hybrid_config.yaml).

## A mock target (`type: mock`)

Tools declared entirely in config with **canned outcomes** — develop and
test the agent before the real tools exist, then swap the target type
without touching the agent. **Local-only, no AWS analog.** Static for
`lcgw sync` purposes.

| Key | Type | Default | Notes |
|---|---|---|---|
| `type` | `mock` | `mock` | |
| `name` | string | required | tools are exposed as `<name>___<tool>` |
| `tools` | list | required, non-empty | mocked tools (below) |

Each tool is a normal [ToolSpec](#a-tool-toolspec) (name / description /
`inputSchema` / `outputSchema`, advertised via `tools/list` as usual) plus
**exactly one** of:

| Key | Type | Notes |
|---|---|---|
| `response` | any YAML value | returned verbatim as the tool payload (`response: null` is a valid canned value) |
| `error` | `{message, type}` | returned as the standard error envelope (`errorMessage`/`errorType`); `type` defaults to `MockError` |

Arguments are accepted and ignored (with `contract_checks` on they are still
schema-validated first).

```yaml
targets:
  - type: mock
    name: billing
    tools:
      - name: invoice
        description: Canned invoice while the real tool is being built.
        inputSchema:
          type: object
          properties: { order_id: { type: string } }
        response: { total: 12.5, currency: USD }
      - name: refund
        error: { message: refunds not implemented yet, type: NotImplemented }
```

Pairs well with [`localcore_gateway.testing`](testing.md) for pytest use.

## `lambda` (`LambdaFunctionConfig`)

| Key | Type | Default | Applies to |
|---|---|---|---|
| `backend` | `native` \| `sam` \| `aws` | `native` | – |
| `handler` | string | – | **native** (required): `module.func` or `path/to/file.py:func` |
| `code_root` | string \| list[string] | config file dir | **native**: dir(s) prepended to `sys.path`. Relative paths resolve against the config dir |
| `python` | string | gateway's interpreter | **native**: Python executable for this target's worker (a path relative to the config dir, or a PATH command like `python3.12`). Path form is made absolute but **symlinks are not followed** (a venv's `bin/python` is a symlink — following it would lose the venv). Lets each target run under its own venv → its own deps + version |
| `sam_endpoint` | string | `http://127.0.0.1:3001` | **sam** |
| `sam_function` | string | – | **sam** (required): logical name in the SAM template |
| `aws_function` | string | – | **aws** (required): deployed function name or full ARN |
| `aws_profile` | string | default credential chain | **aws**: AWS profile for the boto3 session |
| `function_name` | string | `local-function` | native/sam (→ `context.function_name`) |
| `memory_mb` | int | `128` | native/sam (→ `context.memory_limit_in_mb`) |
| `timeout_sec` | float | `30.0` | all (native hard-kills the worker; aws: the boto3 read timeout) |
| `env` | map<str,str> | `{}` | native/sam (process env during invoke) |
| `env_file` | string | – | **native**: `.env`-style file merged into the invoke env; `env` overrides it. Relative to the config dir |
| `region` | string | `us-east-1` | native/sam: ARN / `AWS_REGION`; **aws**: the boto3 client region |

Validation: `backend: native` requires `handler`; `backend: sam` requires
`sam_function`; `backend: aws` requires `aws_function`.

### `backend: aws` — invoke a REAL deployed function

The gateway runs locally, the handler is your **deployed** Lambda (hybrid
debugging; nothing emulated). Requires the `aws` extra
(`pip install 'localcore-gateway[aws]'` — a clear error tells you if it's
missing) and AWS credentials (`aws_profile` or the default chain). Same
AgentCore contract as the other backends — event = tool arguments,
`bedrockAgentCoreToolName` via the standard Lambda ClientContext — plus
`LogType: Tail`, so the invocation's last 4 KB of CloudWatch logs surface in
the usual logs channel (`lcgw invoke` stderr, `lcgw tail`). Retries are
**disabled** (`max_attempts: 0`): a tool invoke is side-effecting, and
botocore's silent retry-on-timeout could double-invoke it.

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
