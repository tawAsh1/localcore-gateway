"""Declarative gateway configuration (YAML -> validated models)."""

from __future__ import annotations

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
        description="AWS-style 'module.func', or 'path/to/file.py:func'. "
        "Required for backend=native.",
    )
    code_root: str | None = Field(
        default=None,
        description="Directory prepended to sys.path so the handler imports "
        "(native backend). Defaults to the config file's directory.",
    )

    # --- sam backend ---
    sam_endpoint: str = Field(
        default="http://127.0.0.1:3001",
        description="`sam local start-lambda` endpoint.",
    )
    sam_function: str | None = Field(
        default=None,
        description="Logical function name in the SAM template. "
        "Required for backend=sam.",
    )

    # --- shared, faithful Lambda config knobs ---
    function_name: str = "local-function"
    memory_mb: int = 128
    timeout_sec: float = 30.0
    env: dict[str, str] = Field(default_factory=dict)
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
    name: str = Field(
        description="Target name; tools are exposed as '<name>___<tool>'."
    )
    lambda_: LambdaFunctionConfig = Field(alias="lambda")
    tools: list[ToolSpec]

    model_config = {"populate_by_name": True}


class AuthConfig(BaseModel):
    """Inbound authorizer (AgentCore: OAuth(JWT) | none)."""

    mode: Literal["none", "jwt"] = "none"
    jwks_uri: str | None = None
    issuer: str | None = None
    audience: str | None = None

    @model_validator(mode="after")
    def _check(self) -> AuthConfig:
        if self.mode == "jwt" and not (self.jwks_uri or self.issuer):
            raise ValueError("auth.jwt requires at least jwks_uri or issuer")
        return self


class ServerConfig(BaseModel):
    name: str = "localcore-gateway"
    host: str = "127.0.0.1"
    port: int = 8080
    path: str = "/mcp"


class GatewayConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    targets: list[LambdaTargetConfig] = Field(default_factory=list)

    # Set by the loader; the directory of the config file.
    source_dir: str | None = None

    def resolved_code_root(self, lc: LambdaFunctionConfig) -> str:
        root = lc.code_root or self.source_dir or "."
        return str(Path(root).expanduser().resolve())


def load_config(path: str | Path) -> GatewayConfig:
    p = Path(path).expanduser().resolve()
    raw = yaml.safe_load(p.read_text()) or {}
    cfg = GatewayConfig.model_validate(raw)
    cfg.source_dir = str(p.parent)
    return cfg
