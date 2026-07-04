# Testing your handlers

`localcore_gateway.testing` is the supported way to integration-test tool
handlers (and the agents that call them) against a **real** running gateway
from pytest — no AWS, no fixtures to hand-roll.

> Stability: this module is public API. The package is 0.x (the config
> schema may still move), but these names and signatures are intended to
> stay stable.

## The API

| Helper | What it does |
|---|---|
| `serve_gateway(config)` | context manager: runs the full gateway (uvicorn thread, ephemeral port) and yields a handle with `.url` (the MCP endpoint) and `.base_url` (server root, where `/-/sync` and `/-/invocations` also live). `config` is a `GatewayConfig`, an inline dict, or a path to a YAML file. Targets are closed cleanly on exit |
| `call_tool(url, name, arguments=None)` | one-shot synchronous tool call; returns the payload (structured content, else joined text). Tool errors raise `fastmcp.exceptions.ToolError` |
| `serve_asgi(app)` | context manager: serve any ASGI app on an ephemeral port; yields the base URL (the lower-level building block) |
| `free_port()` | an OS-assigned free TCP port |

## A complete example

One gateway, two targets: the **real handler under test** (native Lambda
backend) plus a **mock** standing in for a tool that doesn't exist yet — so
the agent-facing catalog is complete from day one.

```python
# test_my_tools.py
import pytest
from fastmcp.exceptions import ToolError

from localcore_gateway.testing import call_tool, serve_gateway

CONFIG = {
    "targets": [
        # The real handler under test (handlers.py next to this file).
        {
            "type": "lambda",
            "name": "demo",
            "lambda": {"backend": "native", "handler": "handlers.handler", "code_root": "."},
            "tools": [
                {
                    "name": "add",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                        "required": ["a", "b"],
                    },
                }
            ],
        },
        # A tool that doesn't exist yet, mocked out with a canned response.
        {
            "type": "mock",
            "name": "billing",
            "tools": [{"name": "invoice", "response": {"total": 12.5, "currency": "USD"}}],
        },
    ]
}


def test_add_handler():
    with serve_gateway(CONFIG) as gw:
        assert call_tool(gw.url, "demo___add", {"a": 2, "b": 40}) == {"sum": 42}


def test_mocked_invoice():
    with serve_gateway(CONFIG) as gw:
        assert call_tool(gw.url, "billing___invoice", {}) == {"total": 12.5, "currency": "USD"}


def test_tool_errors_raise():
    with serve_gateway(CONFIG) as gw, pytest.raises(ToolError):
        call_tool(gw.url, "demo___add", {"a": "not-a-number", "b": 2})  # handler TypeError
```

Notes:

- `call_tool` is synchronous (it runs its own event loop) — call it from
  plain `def` tests. From `async def` tests, use `fastmcp.Client(gw.url)`
  directly instead.
- Relative paths in an inline dict config (like `code_root: "."`) resolve
  against the process working directory; pass absolute paths (e.g. built
  from `tmp_path` or `__file__`) for robustness.
- The whole admin surface is live too: POST `{gw.base_url}/-/sync`, GET
  `{gw.base_url}/-/invocations` (see [cli.md](cli.md)).

## Catching schema drift

Turn on [contract checks](configuration.md#server-serverconfig) in the test
config to fail tests when a handler's arguments or results stop matching the
declared schemas:

```python
CONFIG = {"server": {"contract_checks": "error"}, "targets": [...]}
```

A violating call then returns a `ContractViolation` error (raised as
`ToolError` by `call_tool`) instead of drifting silently. `warn` mode logs
and flags the invocation record instead — visible in `lcgw tail`.
