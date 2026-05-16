"""Declarative gateway configuration (YAML -> validated models)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

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


class ServerConfig(BaseModel):
    name: str = "localcore-gateway"
    host: str = "127.0.0.1"
    port: int = 8080
    path: str = "/mcp"


class GatewayConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    targets: list[LambdaTargetConfig] = Field(default_factory=list)

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


def load_config(path: str | Path) -> GatewayConfig:
    p = Path(path).expanduser().resolve()
    raw = yaml.safe_load(p.read_text()) or {}
    cfg = GatewayConfig.model_validate(raw)
    cfg.source_dir = str(p.parent)
    return cfg
