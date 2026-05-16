# Writing Lambda handlers

A handler here is **plain AWS Lambda code** — it runs unchanged on real Lambda.
The gateway reproduces the AgentCore Gateway → Lambda contract.

Reference handler: [`examples/handlers.py`](../examples/handlers.py).

## The contract

AgentCore Gateway invokes the Lambda with:

- **`event`** = the tool's input arguments (the MCP `arguments` object).
- **`context.client_context.custom`** = identity of the call:
  - `bedrockAgentCoreToolName` — the **un-prefixed** tool name (`add`, not
    `demo___add`)
  - `bedrockAgentCoreGatewayId`, `bedrockAgentCoreTargetName` — the target name

The handler's **return value** becomes the MCP tool result. A raised exception
becomes a Lambda error envelope and surfaces to the MCP client as a tool error.

```python
def handler(event, context):
    custom = context.client_context.custom
    tool = custom.get("bedrockAgentCoreToolName")
    if tool == "add":
        return {"sum": event["a"] + event["b"]}
    if tool == "weather":
        return {"city": event.get("city", "Tokyo"), "tempC": 21}
    raise ValueError(f"no branch for tool {tool!r}")
```

## One Lambda, many tools

A single Lambda target backs every tool listed under its `tools:`. The handler
branches on `bedrockAgentCoreToolName` (exactly as with the real gateway). Each
tool still gets its own `inputSchema` in config, so MCP clients see distinct,
well-typed tools.

## Handler reference forms (native backend)

- `module.func` — AWS-style. Dotted module path + final attribute, e.g.
  `handlers.handler`, `pkg.sub.mod.lambda_handler`. Imported from `code_root`.
- `path/to/file.py:func` — explicit file path (searched across the
  `code_root`s) + the function name.

`code_root` defaults to the config file's directory and may be a **list** of
directories (all go on the *worker's* `sys.path`; the first has priority).
Relative paths resolve against the config file's directory.

`env_file` (native) points at a `.env`-style file (`KEY=VALUE` per line,
`#` comments, optional quotes, optional `export`) whose values are merged into
the invoke environment; the inline `env` map overrides it.

### Process model & isolation

The `native` backend runs **each target's Lambda in its own Python
subprocess** (one warm execution environment per target). Therefore:

- The gateway process's `sys.path` / `sys.modules` are never touched.
- Two targets whose handlers share the **same top-level module name** — the
  monorepo norm where every service has `handler.py` — do **not** collide;
  each worker only sees its own `code_root`s. No renaming needed.
- Set `python` per target to its own venv interpreter
  (`services/orders/.venv/bin/python`): the worker then runs with **that
  sub-project's dependencies and Python version**. The worker is launched by
  file path, so that interpreter does *not* need localcore-gateway installed.
- `timeout_sec` is a **hard** timeout: the worker is killed and respawned.
- It is process-isolated but **not a security sandbox** (no filesystem /
  network jail). Only run trusted handler code; use `sam` for container-grade
  isolation.

## `context` object

Faithful to AWS (`localcore_gateway.lambda_emu.context.LambdaContext`):

`function_name`, `function_version` (`$LATEST`), `invoked_function_arn`,
`memory_limit_in_mb` (string, like AWS), `aws_request_id`, `log_group_name`,
`log_stream_name`, `identity`, `client_context`, and
`get_remaining_time_in_millis()` (driven by `timeout_sec`).

Async handlers (`async def handler(event, context)`) are supported.

## Return values → MCP result

| Handler returns | MCP tool result |
|---|---|
| `dict` | text = JSON, plus structured content |
| `str` | text content |
| `list` / number / bool / `None` | text = JSON |
| (raises) | tool error (`isError`) carrying the Lambda error envelope |

## Errors & logs

A raised exception produces the Lambda error envelope:

```json
{"errorMessage": "...", "errorType": "RuntimeError", "stackTrace": ["..."]}
```

`print()` / stdout / stderr from the handler is captured and emitted in
CloudWatch-style framing on the gateway console:

```
START RequestId: <uuid> Version: $LATEST
<your handler output>
END RequestId: <uuid>
REPORT RequestId: <uuid> Duration: 1.23 ms Billed Duration: 2 ms Memory Size: 128 MB Max Memory Used: 41 MB
```

On a cold start an `INIT_START` line precedes `START`. A cold start happens on
the first call, and whenever **any `*.py` under the `code_root`s changes**
(not just the handler file) — the handler's module subtree is purged from
`sys.modules` and re-imported, so helper-module edits are picked up too.

## native vs sam

| | `native` | `sam` |
|---|---|---|
| Docker | no | yes |
| Isolation | subprocess per target (own `sys.path`/`sys.modules`) | container per function |
| Start / reload | instant; hot reload (respawn) on any `*.py` change under `code_root` | container lifecycle (restart to pick up changes) |
| `event` / `context` | faithful emulation | the real AWS Lambda Linux runtime |
| Timeout | **hard** (worker killed + respawned) | hard (container) |
| Per-invoke logs | on the gateway console | in the `sam local` console |
| Use for | the fast dev loop | fidelity check before deploying to AWS |

Switch by changing `lambda.backend`; the handler code does not change.

## Quick test without HTTP

```bash
lcgw invoke -c config.yaml demo___add --data '{"a":2,"b":40}'
```

This calls the target directly (bypassing MCP/HTTP) and prints the result plus
the captured Lambda logs.
