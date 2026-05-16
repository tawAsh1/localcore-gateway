"""Declarative gateway configuration (YAML -> validated models)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class LambdaFunctionConfig(BaseModel):
    """How to run the Lambda behind a target."""

    backend: Literal["native", "sam"] = "native"

    # --- native backend ---
    handler: str | None = Field(
        default=None,
        description="AWS-style 'module.func', or 'path/to/file.py:func'. Required for backend=native.",
    )
    code_root: str | list[str] | None = Field(
        default=None,
        description="Directory (or list of dirs) prepended to sys.path so the "
        "handler imports (native backend). Relative paths resolve against the "
        "config file's directory. Defaults to the config file's directory.",
    )
    python: str | None = Field(
        default=None,
        description="Python executable for the native worker: a path (relative "
        "to the config dir) or a PATH command. Lets each target run under its "
        "own venv/interpreter (its deps + version). Default: the gateway's "
        "interpreter.",
    )

    # --- sam backend ---
    sam_endpoint: str = Field(
        default="http://127.0.0.1:3001",
        description="`sam local start-lambda` endpoint.",
    )
    sam_function: str | None = Field(
        default=None,
        description="Logical function name in the SAM template. Required for backend=sam.",
    )

    # --- shared, faithful Lambda config knobs ---
    function_name: str = "local-function"
    memory_mb: int = 128
    timeout_sec: float = 30.0
    env: dict[str, str] = Field(default_factory=dict)
    env_file: str | None = Field(
        default=None,
        description="Path to a .env-style file (KEY=VALUE per line) merged "
        "into the invoke env (native backend). `env` overrides it. Relative "
        "to the config file's directory.",
    )
    region: str = "us-east-1"

    @model_validator(mode="after")
    def _check_backend(self) -> LambdaFunctionConfig:
        if self.backend == "native" and not self.handler:
            raise ValueError("lambda.handler is required when backend=native")
        if self.backend == "sam" and not self.sam_function:
            raise ValueError("lambda.sam_function is required when backend=sam")
        return self


class ToolSpec(BaseModel):
    """One MCP tool exposed by a target (AgentCore toolSchema.inlinePayload)."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
        alias="inputSchema",
    )
    output_schema: dict[str, Any] | None = Field(
        default=None,
        alias="outputSchema",
        description="Optional output schema (AgentCore ToolDefinition.outputSchema); advertised via MCP tools/list.",
    )

    model_config = {"populate_by_name": True}


class LambdaTargetConfig(BaseModel):
    """A Lambda gateway target: a function + the tools it backs."""

    type: Literal["lambda"] = "lambda"
    name: str = Field(description="Target name; tools are exposed as '<name>___<tool>'.")
    lambda_: LambdaFunctionConfig = Field(alias="lambda")
    tools: list[ToolSpec] = Field(default_factory=list)
    tool_schema_file: str | None = Field(
        default=None,
        description="Path to a JSON file holding the tool schema list "
        "(AgentCore toolSchema.inlinePayload shape: a list of "
        "{name, description, inputSchema}). Merged with inline `tools` "
        "(inline wins on name clash). Relative to the config file's directory.",
    )

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check_tools(self) -> LambdaTargetConfig:
        if not self.tools and not self.tool_schema_file:
            raise ValueError("target needs `tools` and/or `tool_schema_file`")
        return self


class OpenAPIAuthConfig(BaseModel):
    """Outbound auth to the REST API (AgentCore credential-provider analog).

    Mirrors what AgentCore supports for OpenAPI targets: a static API key in a
    header or query param (custom name), or a bearer token. OAuth 2LO is
    intentionally out of scope locally.
    """

    type: Literal["none", "apikey", "bearer"] = "none"
    in_: Literal["header", "query"] = Field(default="header", alias="in")
    name: str = "X-API-Key"
    value: str | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check(self) -> OpenAPIAuthConfig:
        if self.type in ("apikey", "bearer") and not self.value:
            raise ValueError(f"auth.value is required when type={self.type}")
        return self


class OpenAPITargetConfig(BaseModel):
    """An OpenAPI gateway target: a REST API exposed as MCP tools.

    Faithful to AgentCore: tool name = the operation's ``operationId``
    (verbatim; operationId is REQUIRED on every operation), spec-level
    security schemes are ignored (auth is configured here, out of band).
    """

    type: Literal["openapi"] = "openapi"
    name: str = Field(description="Target name; tools are '<name>___<operationId>'.")
    spec: dict[str, Any] | None = Field(default=None, description="Inline OpenAPI 3.0/3.1 spec.")
    spec_file: str | None = Field(
        default=None,
        description="Path to an OpenAPI spec (JSON/YAML), relative to the "
        "config file's directory. Exactly one of spec / spec_file.",
    )
    base_url: str | None = Field(
        default=None,
        description="Override the API base URL (else the spec's first servers[].url is used).",
    )
    timeout_sec: float = 30.0
    auth: OpenAPIAuthConfig = Field(default_factory=OpenAPIAuthConfig)

    @model_validator(mode="after")
    def _check_spec(self) -> OpenAPITargetConfig:
        if bool(self.spec) == bool(self.spec_file):
            raise ValueError("exactly one of `spec` / `spec_file` is required")
        return self


# Discriminated by `type`.
TargetConfig = Annotated[LambdaTargetConfig | OpenAPITargetConfig, Field(discriminator="type")]


class ServerConfig(BaseModel):
    name: str = "localcore-gateway"
    host: str = "127.0.0.1"
    port: int = 8080
    path: str = "/mcp"


class GatewayConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    targets: list[TargetConfig] = Field(default_factory=list)

    # Set by the loader; the directory of the config file.
    source_dir: str | None = None

    def _resolve(self, rel: str) -> Path:
        """Resolve a path relative to the config file's directory."""
        p = Path(rel).expanduser()
        if p.is_absolute():
            return p.resolve()
        base = Path(self.source_dir) if self.source_dir else Path()
        return (base / p).resolve()

    def resolved_code_roots(self, lc: LambdaFunctionConfig) -> list[str]:
        cr = lc.code_root
        if cr is None:
            roots = [self.source_dir or "."]
        elif isinstance(cr, str):
            roots = [cr]
        else:
            roots = list(cr)
        return [str(self._resolve(r)) for r in roots]

    def resolved_env_file(self, lc: LambdaFunctionConfig) -> str | None:
        return str(self._resolve(lc.env_file)) if lc.env_file else None

    def resolved_python(self, lc: LambdaFunctionConfig) -> str | None:
        p = lc.python
        if not p:
            return None
        # A bare command (no separator) is resolved via PATH (e.g.
        # "python3.12"); pass it through untouched.
        if not (os.sep in p or p.startswith(("~", "."))):
            return p
        # A path: make it absolute (relative to the config dir) but DO NOT
        # follow symlinks. A venv's bin/python is normally a symlink;
        # resolving it would point at the underlying interpreter and lose the
        # venv (its site-packages). Lexical normalize only.
        e = Path(p).expanduser()
        base = Path(self.source_dir) if self.source_dir else Path()
        target = e if e.is_absolute() else base / e
        return os.path.normpath(str(target))

    def effective_tools(self, tc: LambdaTargetConfig) -> list[ToolSpec]:
        """Tools from tool_schema_file (if any) then inline; inline wins."""
        merged: dict[str, ToolSpec] = {}
        if tc.tool_schema_file:
            raw = json.loads(self._resolve(tc.tool_schema_file).read_text())
            if not isinstance(raw, list):
                raise ValueError(f"{tc.tool_schema_file}: expected a JSON list of tool specs")
            for item in raw:
                spec = ToolSpec.model_validate(item)
                merged[spec.name] = spec
        for spec in tc.tools:
            merged[spec.name] = spec
        return list(merged.values())

    def openapi_spec(self, tc: OpenAPITargetConfig) -> dict[str, Any]:
        """The OpenAPI spec dict (inline or loaded from spec_file; JSON/YAML)."""
        if tc.spec is not None:
            return tc.spec
        raw = yaml.safe_load(self._resolve(tc.spec_file).read_text())
        if not isinstance(raw, dict):
            # ValueError (not TypeError): a malformed config/spec file, surfaced
            # like the rest of config validation.
            raise ValueError(  # noqa: TRY004
                f"{tc.spec_file}: OpenAPI spec must be a mapping"
            )
        return raw

    def openapi_base_url(self, tc: OpenAPITargetConfig) -> str:
        """base_url override, else the spec's first servers[].url."""
        if tc.base_url:
            return tc.base_url
        servers = self.openapi_spec(tc).get("servers") or []
        url = servers[0].get("url") if servers else None
        if not url:
            raise ValueError(f"openapi target {tc.name!r}: no base_url and no servers[].url")
        for k, v in (servers[0].get("variables") or {}).items():
            url = url.replace(f"{{{k}}}", str(v.get("default", "")))
        return url


def load_config(path: str | Path) -> GatewayConfig:
    p = Path(path).expanduser().resolve()
    raw = yaml.safe_load(p.read_text()) or {}
    cfg = GatewayConfig.model_validate(raw)
    cfg.source_dir = str(p.parent)
    return cfg
