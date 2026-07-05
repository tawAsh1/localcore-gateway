# CLI reference

```
lcgw <command> -c <config.yaml> [options]
```

All commands take `-c/--config` (path to the gateway config YAML). Run via
`uv run lcgw ...` or, in an activated venv, `lcgw ...`.

## `lcgw serve`

Serve the MCP gateway over Streamable HTTP.

```bash
lcgw serve -c examples/config.yaml [--host H] [--port P]
```

- `--host` / `--port` override `server.host` / `server.port` from config.
- Endpoint: `http://{host}:{port}{path}` (path default `/mcp`).
- No hot reload (use `dev` for that). Targets are closed cleanly on shutdown.

## `lcgw dev`

Same as `serve`, with **hot reload**. Watches the config file's directory and
every target's `code_root`; edits to handlers or config reload automatically
(a cold start, in Lambda terms).

```bash
lcgw dev -c examples/config.yaml [--host H] [--port P]
```

Uses uvicorn's reloader with the `localcore_gateway.app:asgi` factory (config path passed
via `$LCGW_CONFIG`).

## `lcgw tools`

Print the aggregated tool catalog as JSON and exit (no server). Useful for
verifying naming/schemas.

```bash
lcgw tools -c examples/config.yaml
```

Output: a JSON array of `{name, description, inputSchema}`, with names already
prefixed (`demo___add`, …).

## `lcgw invoke`

Call one tool **directly**, bypassing MCP/HTTP. Fastest way to test a handler.

```bash
lcgw invoke -c examples/config.yaml demo___add --data '{"a":2,"b":40}'
lcgw invoke -c examples/config.yaml demo/add   --data '{"a":2,"b":40}'   # / also works
```

- Selector: `target___tool` (or `target/tool`).
- `--data`: JSON object of tool arguments (default `{}`).
- Prints captured Lambda logs to stderr and
  `{"isError": bool, "payload": ...}` to stdout.
- Exit code `1` if the tool errored, `0` on success, `2` on bad selector.

## `lcgw sync`

Re-sync targets on a **running** gateway (`serve`/`dev` must be up): MCP
targets re-discover their upstream catalog (tools, prompts, resources) and
the live registry is updated in place — added, removed, and changed entries
take effect without a restart. The local analog of AgentCore's
`SynchronizeGatewayTargets`, with one divergence: the real API is
asynchronous (202 + poll), ours is synchronous and returns the result
directly.

```bash
lcgw sync -c examples/config.yaml [--target NAME]
```

- Reads `server.host`/`server.port` from the config and POSTs `/-/sync`.
- `--target NAME`: sync only that target.
- Prints one summary per target: the added/removed/updated tool names (plus
  `prompts:`/`resources:` count lines when the target has any),
  `static (nothing to sync)` for Lambda/OpenAPI targets, or the error.
  Shared-resource ownership (`resource_priority`) is re-evaluated too.
- Exit code `1` if the server is unreachable or any target errored.

## `lcgw tail`

Stream invocations from a **running** gateway: one line per tool call (time,
OK/ERROR, tool, duration, compact args/result preview), polling
`GET /-/invocations` (~0.5 s) until Ctrl-C.

```bash
lcgw tail -c examples/config.yaml [-n N] [--json]
```

- `-n N`: show the last N invocations from the backlog first (default: tail
  from "now").
- `--json`: emit raw JSONL records instead of formatted lines.
- Backlog depth is the server's ring buffer (`server.history`, default 1000);
  argument/payload previews are truncated server-side at 4 KB each.
- With `server.contract_checks: warn`, violating invocations are marked with
  a `[contract: ...]` suffix (see [configuration.md](configuration.md)).
- Exit code `0` on Ctrl-C, `1` if the server is unreachable.

Both commands talk to a small local admin surface (`POST /-/sync`,
`GET /-/invocations`) served next to the MCP endpoint. It is local-only and
unauthenticated by design (same stance as the MCP endpoint — see
SECURITY.md) and has no AWS analog.

## `lcgw schema`

Print the config file's JSON Schema (generated from the pydantic models,
matching the YAML surface: `lambda:`, `in:`, `inputSchema`, …).

```bash
lcgw schema                                # JSON to stdout
lcgw schema --output gateway.schema.json   # write to a file
```

Wire it to your editor via the yaml-language-server modeline for completion
and inline validation while editing configs:

```yaml
# yaml-language-server: $schema=./gateway.schema.json
server: { port: 8080 }
targets: []
```

Regenerate the file after upgrading (the schema tracks the config models).

## `lcgw preflight`

Check a config against **real AgentCore deploy constraints** — catches
"worked locally, rejected by `CreateGateway`/`CreateGatewayTarget`" before
you deploy. Config-only: no targets are constructed (no network, no
subprocesses), so tool sets that only exist at runtime (OpenAPI specs,
MCP/aws-gateway upstream catalogs) are not preflighted — only what the
config declares.

```bash
lcgw preflight -c gateway.yaml [--strict]
```

| Severity | Meaning | Checks |
|---|---|---|
| `ERROR` | hard API validation — the deploy **will** be rejected | gateway name pattern `([0-9a-zA-Z][-]?){1,48}`; target name pattern `([0-9a-zA-Z][-]?){1,100}` (underscores are NOT allowed, though they pass locally); empty tool descriptions (required by AgentCore's ToolDefinition) |
| `WARN` | default service quotas — adjustable, may differ per account | >100 targets per gateway; >1000 tools per target; tool names >256 chars; inline tool-schema payload >1 MB per target; `timeout_sec` >900 s (15-minute invocation timeout) |
| `NOTICE` | local-only constructs — nothing to deploy | `type: mock`, `type: aws-gateway`, and MCP targets in stdio `command` mode (url-mode MCP targets are deployable) |

- Exit `1` if any ERROR; with `--strict`, also on any WARN; else `0`
  (`no findings` when clean).
- Constraint sources (checked as of 2026-07; the WARN values are
  account-adjustable defaults):
  [CreateGateway](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateGateway.html),
  [CreateGatewayTarget](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateGatewayTarget.html),
  [AgentCore quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | `invoke`: the tool returned an error; `sync`/`tail`: server unreachable or a target errored; `preflight`: findings at ERROR (or WARN with `--strict`) |
| `2` | bad arguments / unknown target or selector |
