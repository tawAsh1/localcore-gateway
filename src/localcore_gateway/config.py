"""Declarative gateway configuration (YAML -> validated models)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

# `${NAME}` (expanded from the environment) or `$${NAME}` (escape: literal
# `${NAME}`). Bare `$NAME` and any other `$` are left untouched.
_ENV_REF = re.compile(r"\$(\$\{[A-Za-z_][A-Za-z0-9_]*\})|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_str(s: str) -> str:
    def repl(m: re.Match[str]) -> str:
        escaped = m.group(1)
        if escaped:
            return escaped
        name = m.group(2)
        value = os.environ.get(name)
        if value is None:
            raise ValueError(
                f"config references environment variable ${{{name}}}, which is not set (use $${{{name}}} for a literal)"
            )
        return value

    return _ENV_REF.sub(repl, s)


def _expand_env(node: Any) -> Any:
    """Expand ${VAR} in every string scalar of a parsed-YAML tree."""
    if isinstance(node, str):
        return _expand_env_str(node)
    if isinstance(node, list):
        return [_expand_env(v) for v in node]
    if isinstance(node, dict):
        return {k: _expand_env(v) for k, v in node.items()}
    return node


class LambdaFunctionConfig(BaseModel):
    """How to run the Lambda behind a target."""

    backend: Literal["native", "sam", "aws"] = "native"

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

    # --- aws backend (a REAL deployed function; requires the `aws` extra) ---
    aws_function: str | None = Field(
        default=None,
        description="Deployed function name or full ARN. Required for backend=aws.",
    )
    aws_profile: str | None = Field(
        default=None,
        description="AWS profile for backend=aws (default: the default credential chain).",
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
        if self.backend == "aws" and not self.aws_function:
            raise ValueError("lambda.aws_function is required when backend=aws")
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
        description="Path to a JSON file holding the tool schema "
        "(AgentCore toolSchema.inlinePayload shape): a list of "
        "{name, description, inputSchema} OR a single such object. Merged "
        "with inline `tools` (inline wins on name clash). Relative to the "
        "config file's directory.",
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


class MCPTargetConfig(BaseModel):
    """An MCP-passthrough gateway target: another MCP server's tools, proxied.

    Faithful to AgentCore where it matters (remote tool names are used
    **verbatim**; the gateway adds the `<target>___` prefix uniformly, same
    as the other target types) but the transport split is a local-only
    convenience: the real gateway only ever speaks streamable HTTP to the
    upstream MCP server (``url``); the ``command`` (stdio) mode here has no
    AWS analog -- it exists so you can point the gateway at a local MCP
    server (e.g. one you're developing) without standing up an HTTP listener
    for it first.
    """

    type: Literal["mcp"] = "mcp"
    name: str = Field(description="Target name; tools are exposed as '<name>___<tool>'.")

    # --- remote, streamable HTTP (the AgentCore-faithful mode) ---
    url: str | None = Field(default=None, description="Upstream MCP server endpoint (streamable HTTP).")
    headers: dict[str, str] = Field(
        default_factory=dict, description="Static headers sent with every request (http only)."
    )
    auth: OpenAPIAuthConfig = Field(
        default_factory=OpenAPIAuthConfig,
        description="Outbound auth to the upstream server (http only). Same shape and behavior as "
        "OpenAPI targets: bearer, or a static API key in a header or query param.",
    )

    # --- local, stdio (convenience only; no AWS analog) ---
    command: str | None = Field(
        default=None,
        description="Command to spawn a local MCP server over stdio: a path (relative to the config dir) "
        "or a PATH command. The subprocess does NOT inherit the gateway's full environment -- the MCP SDK "
        "spawns it with a safe default subset (HOME, PATH, SHELL, TERM, USER, LOGNAME on POSIX) plus "
        "`env_file` / `env` below.",
    )
    args: list[str] = Field(
        default_factory=list,
        description="Arguments passed to `command` (stdio only). Opaque to the gateway: script paths in "
        "here are resolved by the child, relative to its `cwd`.",
    )
    env: dict[str, str] = Field(
        default_factory=dict, description="Extra environment variables for the subprocess (stdio only)."
    )
    env_file: str | None = Field(
        default=None,
        description="Path to a .env-style file (KEY=VALUE per line) merged into the subprocess env; "
        "`env` overrides it (stdio only). Relative to the config file's directory.",
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory for the subprocess (stdio only). Relative to the config file's "
        "directory; defaults to it.",
    )

    timeout_sec: float = 30.0
    tools: list[str] = Field(default_factory=list, description="Optional allowlist of upstream tool names to expose.")

    @model_validator(mode="after")
    def _check_transport(self) -> MCPTargetConfig:
        if bool(self.url) == bool(self.command):
            raise ValueError("exactly one of `url` / `command` is required")
        if self.command:
            if self.headers:
                raise ValueError("mcp target: `headers` requires `url` (stdio `command` mode has no HTTP headers)")
            if self.auth.type != "none":
                raise ValueError("mcp target: `auth` requires `url` (stdio `command` mode has no HTTP auth)")
        else:
            if self.env:
                raise ValueError("mcp target: `env` requires `command` (stdio-only; not applicable to `url`)")
            if self.env_file:
                raise ValueError("mcp target: `env_file` requires `command` (stdio-only; not applicable to `url`)")
            if self.cwd:
                raise ValueError("mcp target: `cwd` requires `command` (stdio-only; not applicable to `url`)")
        return self


class AWSGatewayAuthConfig(BaseModel):
    """Outbound auth to a real AgentCore Gateway: bearer (OAuth/JWT) or SigV4 (IAM)."""

    type: Literal["none", "bearer", "sigv4"] = "none"
    value: str | None = Field(default=None, description="Bearer token (type=bearer).")
    region: str | None = Field(
        default=None,
        description="Signing region (type=sigv4). Optional: falls back to the "
        "profile chain's region; no region anywhere is a startup error.",
    )
    profile: str | None = Field(
        default=None,
        description="AWS profile (type=sigv4). Default: the default credential chain.",
    )

    @model_validator(mode="after")
    def _check(self) -> AWSGatewayAuthConfig:
        if self.type == "bearer" and not self.value:
            raise ValueError("auth.value is required when type=bearer")
        return self


class AWSGatewayTargetConfig(BaseModel):
    """A REAL deployed AgentCore Gateway, proxied into the local one.

    No AWS analog as a target type -- this exists for the hybrid debugging
    workflow (develop one target locally, pass the rest of the production
    toolset through). The deployed gateway's tools already carry AgentCore's
    ``remoteTarget___tool`` names and are exposed **verbatim** -- no local
    ``<name>___`` prefix (re-prefixing would double it) -- so ``name`` is for
    identification/logging only. Requires the `aws` extra for sigv4 auth.
    """

    type: Literal["aws-gateway"] = "aws-gateway"
    name: str = Field(description="Target name (identification/logging only; tools are NOT prefixed).")
    url: str = Field(description="The deployed gateway's MCP endpoint (streamable HTTP).")
    headers: dict[str, str] = Field(default_factory=dict, description="Static headers sent with every request.")
    auth: AWSGatewayAuthConfig = Field(
        default_factory=AWSGatewayAuthConfig,
        description="Outbound auth: bearer (OAuth/JWT-configured gateways) or sigv4 (IAM).",
    )
    timeout_sec: float = 30.0
    tools: list[str] = Field(
        default_factory=list,
        description="Optional allowlist of remote tool names to expose (already-prefixed form).",
    )


class MockErrorSpec(BaseModel):
    """The canned error of a mock tool (becomes the standard error envelope)."""

    message: str
    type: str = "MockError"


class MockToolSpec(ToolSpec):
    """A ToolSpec plus exactly one canned outcome: `response` or `error`."""

    response: Any = Field(
        default=None,
        description="Canned payload, returned verbatim (any YAML value; `response: null` is valid).",
    )
    error: MockErrorSpec | None = Field(
        default=None,
        description="Canned error ({message, type}); returned as the standard error envelope.",
    )

    @model_validator(mode="after")
    def _check_outcome(self) -> MockToolSpec:
        # Presence-based (not truthiness): `response: null` counts as set.
        if ("response" in self.model_fields_set) == ("error" in self.model_fields_set):
            raise ValueError("each mock tool needs exactly one of `response` / `error`")
        return self


class MockTargetConfig(BaseModel):
    """A mock gateway target: tools declared entirely in config, canned outcomes.

    Local-only, no AWS analog -- develop and test the agent before the real
    tools exist. Each tool is a normal ToolSpec (schemas advertised as usual)
    plus a canned `response` or `error`.
    """

    type: Literal["mock"] = "mock"
    name: str = Field(description="Target name; tools are exposed as '<name>___<tool>'.")
    tools: list[MockToolSpec] = Field(description="The mocked tools (required, non-empty).")

    @model_validator(mode="after")
    def _check_tools(self) -> MockTargetConfig:
        if not self.tools:
            raise ValueError("mock target needs at least one tool")
        return self


# Discriminated by `type`.
TargetConfig = Annotated[
    LambdaTargetConfig | OpenAPITargetConfig | MCPTargetConfig | AWSGatewayTargetConfig | MockTargetConfig,
    Field(discriminator="type"),
]


class ServerConfig(BaseModel):
    name: str = "localcore-gateway"
    host: str = "127.0.0.1"
    port: int = 8080
    path: str = "/mcp"
    history: int = Field(
        default=1000,
        description="Invocation-history ring buffer size (backs `lcgw tail` / GET /-/invocations).",
    )
    contract_checks: Literal["off", "warn", "error"] = Field(
        default="off",
        description="Validate tool arguments/results against the declared JSON Schemas. "
        "off (default) = faithful (the real gateway does not validate); warn = pass through "
        "but log + flag the invocation record; error = return a ContractViolation error.",
    )


class GatewayConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    targets: list[TargetConfig] = Field(default_factory=list)

    # Set by the loader; the directory of the config file. SkipJsonSchema:
    # loader-internal, kept out of `lcgw schema` (it's not a YAML surface).
    source_dir: SkipJsonSchema[str | None] = None

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

    def _resolve_command(self, p: str) -> str:
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

    def resolved_python(self, lc: LambdaFunctionConfig) -> str | None:
        return self._resolve_command(lc.python) if lc.python else None

    def resolved_command(self, tc: MCPTargetConfig) -> str:
        """`command` for a stdio target: PATH command or config-dir-relative path (same rule as resolved_python)."""
        return self._resolve_command(tc.command or "")

    def resolved_cwd(self, tc: MCPTargetConfig) -> str | None:
        """The stdio subprocess cwd; defaults to the config file's directory."""
        if tc.cwd:
            return str(self._resolve(tc.cwd))
        return self.source_dir

    def mcp_env(self, tc: MCPTargetConfig) -> dict[str, str] | None:
        """env_file (if any) merged under inline `env`; None if neither.

        Passed to the MCP SDK's stdio spawn, which layers it over its safe
        default env subset (HOME, PATH, SHELL, TERM, USER, LOGNAME on POSIX)
        -- the subprocess does NOT inherit the gateway's full environment.
        """
        merged: dict[str, str] = {}
        if tc.env_file:
            merged.update(_parse_env_file(str(self._resolve(tc.env_file))))
        merged.update(tc.env)
        return merged or None

    def effective_tools(self, tc: LambdaTargetConfig) -> list[ToolSpec]:
        """Tools from tool_schema_file (if any) then inline; inline wins."""
        merged: dict[str, ToolSpec] = {}
        if tc.tool_schema_file:
            raw = json.loads(self._resolve(tc.tool_schema_file).read_text())
            # Accept a list of tool specs OR a single tool spec dict.
            if isinstance(raw, dict):
                raw = [raw]
            elif not isinstance(raw, list):
                raise ValueError(
                    f"{tc.tool_schema_file}: expected a JSON list of tool specs or a single tool-spec object"
                )
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


def _parse_env_file(path: str) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE per line; #-comments; optional quotes.

    Shared by the native Lambda backend (`lambda.env_file`) and MCP stdio
    targets (`env_file`).
    """
    out: dict[str, str] = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def load_config(path: str | Path) -> GatewayConfig:
    """Load and validate a gateway config file.

    ``${VAR}`` in any string value is expanded from the environment (the
    AgentCore analog of credential providers: secrets stay out of the config
    file). An unset variable is a config error; ``$${VAR}`` escapes to a
    literal ``${VAR}``. Expansion applies to the config file only -- not to
    files it references (``spec_file``, ``tool_schema_file``, ``env_file``).
    """
    p = Path(path).expanduser().resolve()
    raw = yaml.safe_load(p.read_text()) or {}
    cfg = GatewayConfig.model_validate(_expand_env(raw))
    cfg.source_dir = str(p.parent)
    return cfg
