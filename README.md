# localcore-gateway

[![CI](https://github.com/tawAsh1/localcore-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/tawAsh1/localcore-gateway/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python ≥3.11](https://img.shields.io/badge/python-%E2%89%A53.11-blue.svg)](pyproject.toml)

A local, faithful-enough reimplementation of **AWS Bedrock AgentCore Gateway**,
with a pluggable **local Lambda backend**. Develop and test agent ↔ gateway ↔
Lambda integrations entirely on your machine, then point the same MCP client at
the real AWS gateway with no code changes.

There is no official local emulator for AgentCore Gateway (AWS's `agentcore dev`
is for the *Runtime*, not the Gateway). This fills that gap.

## What it reproduces

- **MCP Streamable-HTTP at `/mcp`** — the same wire surface as the real gateway
  (built on the [FastMCP](https://github.com/jlowin/fastmcp) 3.x server; no
  hand-rolled JSON-RPC).
- **Target aggregation** — every `(target, tool)` is exposed as one MCP tool
  named `target___tool` (AgentCore's triple-underscore convention).
- **AgentCore Lambda contract** — the tool arguments are passed as the Lambda
  **event**; the tool identity is delivered via
  `context.client_context.custom['bedrockAgentCoreToolName']`; the Lambda's
  return value becomes the tool result.

## Local Lambda backends

| backend  | Docker | fidelity | use it for |
|----------|--------|----------|------------|
| `native` | no     | faithful `event`/`context`, error envelope, CloudWatch-style logs, **hot reload**; *soft* timeout | the fast dev loop |
| `sam`    | yes    | the **real** AWS Lambda Linux runtime via `sam local start-lambda`; hard timeout/isolation | fidelity check before AWS |

## Documentation

- [Architecture](docs/architecture.md) — request flow, AgentCore contract mapping, component map
- [Configuration reference](docs/configuration.md) — every config field
- [Writing Lambda handlers](docs/lambda-handlers.md) — the handler contract, multi-tool, errors, logs, native vs sam
- [CLI reference](docs/cli.md) — `serve` / `dev` / `tools` / `invoke`
- [Connecting agents](docs/connecting-agents.md) — point an MCP client at it; promote to real AWS

## Quick start

```bash
uv sync
uv run lcgw tools  -c examples/config.yaml          # show the tool catalog
uv run lcgw invoke -c examples/config.yaml demo___add --data '{"a":2,"b":40}'
uv run lcgw serve  -c examples/config.yaml           # MCP at http://127.0.0.1:8080/mcp
uv run lcgw dev    -c examples/config.yaml           # same, with hot reload
```

Point any MCP client at `http://127.0.0.1:8080/mcp`.

### Using the `sam` backend

Run `sam local start-lambda` in your SAM project, then set in the target:

```yaml
lambda:
  backend: sam
  sam_endpoint: http://127.0.0.1:3001
  sam_function: DemoFunction
```

## Configuration

See [`examples/config.yaml`](examples/config.yaml). A target declares a Lambda
(`backend`, `handler`/`sam_function`, `memory_mb`, `timeout_sec`, `env`) and the
tools it backs (each with an explicit JSON Schema). One Lambda can back many
tools; the handler branches on `bedrockAgentCoreToolName`.

## Tests

```bash
uv run pytest
```

## Known limitations

- `native` timeout is *soft*: a stuck synchronous handler thread cannot be
  hard-killed in-process (use `sam` for true isolation).
- `sam` per-invoke logs appear in the `sam local` console (out-of-band for the
  Invoke API).
- AgentCore's builtin semantic tool search (`x_amz_bedrock_agentcore_search`)
  is **not implemented** (intentionally omitted).
- Target types beyond Lambda (OpenAPI, MCP passthrough, Smithy) are not yet
  implemented.

## License

[Apache License 2.0](LICENSE). See [`NOTICE`](NOTICE) for attribution and the
trademark disclaimer below.

## Trademarks & disclaimer

This is an unofficial, community project. It is **not affiliated with,
endorsed by, or sponsored by Amazon Web Services, Inc. or its affiliates**.

"AWS", "Amazon Web Services", "Amazon Bedrock", "Amazon Bedrock AgentCore",
and "AWS Lambda" are trademarks of Amazon.com, Inc. or its affiliates. They
are used here only nominatively, to accurately describe the AWS service this
project interoperates with / reimplements locally. No AWS trademark, logo, or
trade dress is used as the name or branding of this project.
