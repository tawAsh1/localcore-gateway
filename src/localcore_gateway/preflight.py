"""Preflight: validate a config against real-AgentCore deploy constraints.

Catches "worked locally, rejected by CreateGateway / CreateGatewayTarget"
before you deploy. Three severities:

* ERROR  -- hard API validation; the deploy WILL be rejected.
* WARN   -- default service quotas; adjustable, may differ per account.
* NOTICE -- local-only constructs with no AWS analog; nothing to deploy.

Constraint sources (checked as of 2026-07; quotas are account-adjustable
defaults):

* https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateGateway.html
* https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateGatewayTarget.html
* https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html

Deliberately config-only: ``preflight()`` constructs NO targets (no network,
no subprocesses -- MCP discovery must not fire). Tool sets that only exist
at runtime (openapi specs, MCP/aws-gateway upstream catalogs) are therefore
not preflighted; only what the config declares is.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from localcore_gateway.config import GatewayConfig, ToolSpec

# --- CreateGateway / CreateGatewayTarget name patterns (hard validation) ---
_GATEWAY_NAME_RE = re.compile(r"([0-9a-zA-Z][-]?){1,48}")
_TARGET_NAME_RE = re.compile(r"([0-9a-zA-Z][-]?){1,100}")

# --- default service quotas (account-adjustable) ---
_MAX_TARGETS_PER_GATEWAY = 100
_MAX_TOOLS_PER_TARGET = 1000
_MAX_TOOL_NAME_LEN = 256
_MAX_INLINE_SCHEMA_BYTES = 1_000_000  # 1 MB inline toolSchema payload per target
_MAX_TIMEOUT_SEC = 900.0  # gateway invocation timeout: 15 minutes

# Hard documented limit for Smithy models (gateway-building-smithy-targets).
_MAX_SMITHY_MODEL_BYTES = 10_000_000

# One obvious place to classify local-only constructs (extend when adding
# target types): first matching rule wins, no match = deployable.
_LOCAL_ONLY_RULES: tuple[tuple[str, Callable[[Any], bool]], ...] = (
    ("mock target", lambda tc: tc.type == "mock"),
    ("aws-gateway passthrough target", lambda tc: tc.type == "aws-gateway"),
    ("MCP target in stdio `command` mode", lambda tc: tc.type == "mcp" and bool(tc.command)),
)

Severity = Literal["ERROR", "WARN", "NOTICE"]


@dataclass
class Finding:
    """One preflight finding: where, how bad, and what."""

    severity: Severity
    location: str
    message: str


def _declared_tools(cfg: GatewayConfig, tc: Any) -> list[ToolSpec]:
    """The config-declared tool specs of a target ([] when runtime-discovered)."""
    if tc.type == "lambda":
        return cfg.effective_tools(tc)
    if tc.type == "mock":
        return list(tc.tools)
    return []


def _target_timeout(tc: Any) -> float | None:
    if tc.type == "lambda":
        return tc.lambda_.timeout_sec
    return getattr(tc, "timeout_sec", None)


def preflight(cfg: GatewayConfig) -> list[Finding]:
    """All findings for ``cfg``, config-only (no targets are constructed)."""
    findings: list[Finding] = []

    if not _GATEWAY_NAME_RE.fullmatch(cfg.server.name):
        findings.append(
            Finding(
                "ERROR",
                "server.name",
                f"gateway name {cfg.server.name!r} is rejected by CreateGateway: "
                "must be alphanumeric with single hyphens in between, at most 48 characters "
                "(pattern ([0-9a-zA-Z][-]?){1,48})",
            )
        )

    if len(cfg.targets) > _MAX_TARGETS_PER_GATEWAY:
        findings.append(
            Finding(
                "WARN",
                "targets",
                f"{len(cfg.targets)} targets exceeds the default quota of "
                f"{_MAX_TARGETS_PER_GATEWAY} targets per gateway",
            )
        )

    for tc in cfg.targets:
        loc = f"targets[{tc.name}]"

        for reason, matches in _LOCAL_ONLY_RULES:
            if matches(tc):
                findings.append(
                    Finding(
                        "NOTICE",
                        loc,
                        f"{reason}: local-only, no AWS analog -- will need replacement before deploying",
                    )
                )
                break

        if not _TARGET_NAME_RE.fullmatch(tc.name):
            findings.append(
                Finding(
                    "ERROR",
                    loc,
                    f"target name {tc.name!r} is rejected by CreateGatewayTarget: "
                    "must be alphanumeric with single hyphens in between, at most 100 characters "
                    "(pattern ([0-9a-zA-Z][-]?){1,100}) -- underscores are NOT allowed",
                )
            )

        timeout = _target_timeout(tc)
        if timeout is not None and timeout > _MAX_TIMEOUT_SEC:
            findings.append(
                Finding(
                    "WARN",
                    f"{loc}.timeout_sec",
                    f"{timeout:g}s exceeds the gateway invocation timeout of {_MAX_TIMEOUT_SEC:g}s (15 minutes)",
                )
            )

        if tc.type == "smithy":
            # Hard documented limit (deploy rejected), so ERROR not WARN.
            model_size = len(json.dumps(cfg.smithy_model(tc)).encode())
            if model_size > _MAX_SMITHY_MODEL_BYTES:
                findings.append(
                    Finding(
                        "ERROR",
                        f"{loc}.model",
                        f"Smithy model is {model_size} bytes, over the {_MAX_SMITHY_MODEL_BYTES}-byte (10 MB) limit",
                    )
                )

        tools = _declared_tools(cfg, tc)
        if not tools:
            continue

        if len(tools) > _MAX_TOOLS_PER_TARGET:
            findings.append(
                Finding(
                    "WARN",
                    loc,
                    f"{len(tools)} tools exceeds the default quota of {_MAX_TOOLS_PER_TARGET} tools per target",
                )
            )

        for tool in tools:
            if not tool.description:
                findings.append(
                    Finding(
                        "ERROR",
                        f"{loc}.tools[{tool.name}]",
                        "description is required by AgentCore's ToolDefinition (empty descriptions are rejected)",
                    )
                )
            if len(tool.name) > _MAX_TOOL_NAME_LEN:
                findings.append(
                    Finding(
                        "WARN",
                        f"{loc}.tools[{tool.name[:32]}...]",
                        f"tool name is {len(tool.name)} characters, over the "
                        f"{_MAX_TOOL_NAME_LEN}-character tool name limit",
                    )
                )

        # AgentCore's toolSchema.inlinePayload shape, measured as JSON.
        payload = [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
                **({"outputSchema": t.output_schema} if t.output_schema is not None else {}),
            }
            for t in tools
        ]
        size = len(json.dumps(payload, ensure_ascii=False).encode())
        if size > _MAX_INLINE_SCHEMA_BYTES:
            findings.append(
                Finding(
                    "WARN",
                    loc,
                    f"inline tool schema payload is {size} bytes, over the "
                    f"{_MAX_INLINE_SCHEMA_BYTES}-byte (1 MB) maximum inline schema size",
                )
            )

    return findings
