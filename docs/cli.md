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

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | `invoke`: the tool returned an error |
| `2` | bad arguments / unknown target or selector |
