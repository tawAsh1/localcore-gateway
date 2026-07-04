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
targets re-discover their upstream tool set and the live catalog is updated
in place — added, removed, and changed tools take effect without a restart.
The local analog of AgentCore's `SynchronizeGatewayTargets`, with one
divergence: the real API is asynchronous (202 + poll), ours is synchronous
and returns the result directly.

```bash
lcgw sync -c examples/config.yaml [--target NAME]
```

- Reads `server.host`/`server.port` from the config and POSTs `/-/sync`.
- `--target NAME`: sync only that target.
- Prints one summary per target: the added/removed/updated tool names,
  `static (nothing to sync)` for Lambda/OpenAPI targets, or the error.
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

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | `invoke`: the tool returned an error; `sync`/`tail`: server unreachable or a target errored |
| `2` | bad arguments / unknown target or selector |
