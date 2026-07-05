"""Smithy gateway target: a Smithy 2.0 model's operations as MCP tools.

AgentCore-faithful (see gateway-building-smithy-targets in the devguide):
the model is the **Smithy 2.0 JSON AST**, the service must carry the
``aws.protocols#restJson1`` trait (the only protocol the real gateway
supports -- restXml/awsJson/awsQuery/ec2Query and streaming operations are
rejected there too), and models are capped at 10 MB. One MCP tool per
operation; the tool name is the operation's **shape name without its
namespace** (``example.weather#GetCurrentWeather`` -> ``GetCurrentWeather``),
the Smithy analog of the verbatim ``operationId``. The gateway's aggregation
adds the ``<target>___`` prefix uniformly.

Local divergences, deliberate for a dev tool: any restJson1 model is
accepted (AWS restricts custom models to AWS services and ships the
api-models-aws set), and ``base_url`` is required because endpoint rule sets
are not implemented. Timestamps are advertised (and passed through) as
date-time strings rather than restJson1's epoch-seconds body default -- a
documented simplification.

Invocation is httpx (like OpenAPI targets), applying the restJson1 bindings:
``httpLabel`` members interpolate the URI path, ``httpQuery`` /
``httpQueryParams`` become query params, ``httpHeader`` headers,
``httpPayload`` the raw body, and every unbound member lands in the JSON
request body. Non-2xx responses map to the standard error envelope.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from localcore_gateway.config import GatewayConfig, SmithyAuthConfig, SmithyTargetConfig

# The `_SigV4Auth` engine is shared with aws-gateway targets; a Smithy target
# signs for the actual AWS service (auth.service) instead of bedrock-agentcore.
from localcore_gateway.targets.aws_gateway_target import _SigV4Auth
from localcore_gateway.targets.base import Target, ToolDef, ToolOutcome
from localcore_gateway.targets.openapi_target import _ApiKeyAuth

_RESTJSON1 = "aws.protocols#restJson1"
_MAX_MODEL_BYTES = 10_000_000  # the real gateway's documented 10 MB cap

# Smithy prelude simple shapes -> JSON Schema.
_PRELUDE = {
    "smithy.api#String": {"type": "string"},
    "smithy.api#Boolean": {"type": "boolean"},
    "smithy.api#PrimitiveBoolean": {"type": "boolean"},
    "smithy.api#Byte": {"type": "integer"},
    "smithy.api#Short": {"type": "integer"},
    "smithy.api#Integer": {"type": "integer"},
    "smithy.api#PrimitiveInteger": {"type": "integer"},
    "smithy.api#Long": {"type": "integer"},
    "smithy.api#PrimitiveLong": {"type": "integer"},
    "smithy.api#BigInteger": {"type": "integer"},
    "smithy.api#Float": {"type": "number"},
    "smithy.api#Double": {"type": "number"},
    "smithy.api#BigDecimal": {"type": "number"},
    "smithy.api#Timestamp": {"type": "string", "format": "date-time"},
    "smithy.api#Blob": {"type": "string", "contentEncoding": "base64"},
    "smithy.api#Document": {},
    "smithy.api#Unit": {"type": "object", "properties": {}},
}


def _build_smithy_auth(a: SmithyAuthConfig) -> httpx.Auth | None:
    if a.type == "none":
        return None
    if a.type == "bearer":
        return _ApiKeyAuth("header", "Authorization", f"Bearer {a.value}")
    if a.type == "apikey":
        return _ApiKeyAuth(a.in_, a.name, a.value or "")
    return _SigV4Auth(a, service=a.service or "", kind="smithy")


class _Model:
    """A parsed Smithy 2.0 JSON AST: shape lookups + schema conversion."""

    def __init__(self, name: str, ast: dict[str, Any]) -> None:
        self.target = name  # target name, for error messages
        self.shapes: dict[str, Any] = ast.get("shapes") or {}
        if not self.shapes:
            raise ValueError(f"smithy target {name!r}: model has no `shapes` (expected the Smithy 2.0 JSON AST)")

    def shape(self, shape_id: str) -> dict[str, Any]:
        try:
            return self.shapes[shape_id]
        except KeyError:
            raise ValueError(f"smithy target {self.target!r}: model references unknown shape {shape_id!r}") from None

    def service(self) -> tuple[str, dict[str, Any]]:
        """The single service shape; must carry aws.protocols#restJson1."""
        services = [(sid, s) for sid, s in self.shapes.items() if s.get("type") == "service"]
        if len(services) != 1:
            raise ValueError(
                f"smithy target {self.target!r}: expected exactly one service shape, found {len(services)}"
            )
        sid, service = services[0]
        if _RESTJSON1 not in (service.get("traits") or {}):
            raise ValueError(
                f"smithy target {self.target!r}: service {sid!r} does not use aws.protocols#restJson1 -- "
                "the only protocol supported (matching the real gateway; restXml/awsJson/awsQuery are not)"
            )
        return sid, service

    def operation_ids(self) -> list[str]:
        """All operation shape ids of the service, resources included."""
        _sid, service = self.service()
        ops: list[str] = [ref["target"] for ref in service.get("operations") or []]
        pending = [ref["target"] for ref in service.get("resources") or []]
        while pending:
            resource = self.shape(pending.pop())
            for key in ("operations", "collectionOperations"):
                ops.extend(ref["target"] for ref in resource.get(key) or [])
            # Lifecycle operations (create/put/read/update/delete/list).
            ops.extend(
                resource[key]["target"]
                for key in ("create", "put", "read", "update", "delete", "list")
                if key in resource
            )
            pending.extend(ref["target"] for ref in resource.get("resources") or [])
        return ops

    def members(self, structure_id: str) -> dict[str, Any]:
        if structure_id == "smithy.api#Unit":
            return {}
        return self.shape(structure_id).get("members") or {}

    def reject_streaming(self, op_name: str, structure_id: str) -> None:
        for member_name, member in self.members(structure_id).items():
            target_shape = self.shapes.get(member["target"]) or {}
            traits = target_shape.get("traits") or {}
            if "smithy.api#streaming" in traits:
                # Event streams and streaming blobs alike: the real gateway
                # rejects streaming operations, and so do we -- loudly, per
                # operation, instead of skipping silently.
                raise ValueError(
                    f"smithy target {self.target!r}: operation {op_name!r} member {member_name!r} "
                    "uses smithy.api#streaming -- streaming operations are unsupported "
                    "(matching the real gateway)"
                )

    def schema(self, shape_id: str, visited: frozenset[str] = frozenset()) -> dict[str, Any]:
        """Shape -> JSON Schema. Recursive references collapse to bare {}."""
        if shape_id in _PRELUDE:
            return dict(_PRELUDE[shape_id])
        if shape_id in visited:
            return {}  # recursive shape: bare schema instead of infinite descent
        visited = visited | {shape_id}
        shape = self.shape(shape_id)
        kind = shape.get("type")
        traits = shape.get("traits") or {}
        doc = traits.get("smithy.api#documentation")

        if kind == "structure":
            properties: dict[str, Any] = {}
            required: list[str] = []
            for member_name, member in (shape.get("members") or {}).items():
                mtraits = member.get("traits") or {}
                mschema = self.schema(member["target"], visited)
                mdoc = mtraits.get("smithy.api#documentation")
                if mdoc:
                    mschema["description"] = mdoc
                if "smithy.api#default" in mtraits:
                    mschema["default"] = mtraits["smithy.api#default"]
                if "smithy.api#required" in mtraits:
                    required.append(member_name)
                properties[member_name] = mschema
            out: dict[str, Any] = {"type": "object", "properties": properties}
            if required:
                out["required"] = required
        elif kind == "list":
            out = {"type": "array", "items": self.schema(shape["member"]["target"], visited)}
        elif kind == "map":
            out = {"type": "object", "additionalProperties": self.schema(shape["value"]["target"], visited)}
        elif kind in ("enum", "intEnum"):
            values = [
                (m.get("traits") or {}).get("smithy.api#enumValue", name)
                for name, m in (shape.get("members") or {}).items()
            ]
            out = {"type": "string" if kind == "enum" else "integer", "enum": values}
        elif kind == "string":
            out = {"type": "string"}
        elif kind in ("byte", "short", "integer", "long", "bigInteger"):
            out = {"type": "integer"}
        elif kind in ("float", "double", "bigDecimal"):
            out = {"type": "number"}
        elif kind == "boolean":
            out = {"type": "boolean"}
        elif kind == "timestamp":
            out = {"type": "string", "format": "date-time"}
        elif kind == "blob":
            out = {"type": "string", "contentEncoding": "base64"}
        elif kind == "document":
            out = {}
        else:
            # Unions and anything else restJson1 can't express as a flat tool
            # schema: reject, mirroring the real gateway's unsupported list.
            raise ValueError(f"smithy target {self.target!r}: shape {shape_id!r} has unsupported type {kind!r}")
        if doc and "description" not in out:
            out["description"] = doc
        return out


class _Operation:
    """One restJson1 operation: schema + the HTTP binding plan."""

    def __init__(self, model: _Model, op_id: str) -> None:
        op = model.shape(op_id)
        self.name = op_id.split("#", 1)[1]
        traits = op.get("traits") or {}
        http = traits.get("smithy.api#http")
        if not http:
            raise ValueError(
                f"smithy target {model.target!r}: operation {self.name!r} has no smithy.api#http "
                "trait -- required for restJson1"
            )
        self.method: str = http["method"]
        self.uri: str = http["uri"]
        self.description: str = traits.get("smithy.api#documentation") or ""

        input_id = (op.get("input") or {}).get("target", "smithy.api#Unit")
        output_id = (op.get("output") or {}).get("target", "smithy.api#Unit")
        model.reject_streaming(self.name, input_id)
        model.reject_streaming(self.name, output_id)

        self.input_schema = (
            model.schema(input_id) if input_id != "smithy.api#Unit" else {"type": "object", "properties": {}}
        )
        self.output_schema = model.schema(output_id) if output_id != "smithy.api#Unit" else None

        # Binding plan: member name -> (kind, binding detail).
        self.bindings: dict[str, tuple[str, str]] = {}
        for member_name, member in model.members(input_id).items():
            mtraits = member.get("traits") or {}
            if "smithy.api#httpLabel" in mtraits:
                self.bindings[member_name] = ("label", member_name)
            elif "smithy.api#httpQuery" in mtraits:
                self.bindings[member_name] = ("query", mtraits["smithy.api#httpQuery"])
            elif "smithy.api#httpQueryParams" in mtraits:
                self.bindings[member_name] = ("query_params", member_name)
            elif "smithy.api#httpHeader" in mtraits:
                self.bindings[member_name] = ("header", mtraits["smithy.api#httpHeader"])
            elif "smithy.api#httpPayload" in mtraits:
                self.bindings[member_name] = ("payload", member_name)
            else:
                self.bindings[member_name] = ("body", member_name)

    def request(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """The httpx request kwargs for ``arguments`` (restJson1 bindings)."""
        uri = self.uri
        params: dict[str, Any] = {}
        headers: dict[str, str] = {}
        body: dict[str, Any] = {}
        payload: Any = None
        has_payload = False

        for member_name, (kind, detail) in self.bindings.items():
            if member_name not in arguments:
                if kind == "label":
                    raise ValueError(f"missing required path parameter {member_name!r}")
                continue
            value = arguments[member_name]
            if kind == "label":
                text = _param_str(value)
                uri = uri.replace(f"{{{detail}}}", text).replace(f"{{{detail}+}}", text)
            elif kind == "query":
                params[detail] = _param_str(value) if not isinstance(value, list) else [_param_str(v) for v in value]
            elif kind == "query_params":
                params.update({k: _param_str(v) for k, v in dict(value).items()})
            elif kind == "header":
                headers[detail] = _param_str(value)
            elif kind == "payload":
                payload, has_payload = value, True
            else:
                body[member_name] = value

        kwargs: dict[str, Any] = {"method": self.method, "url": uri, "params": params, "headers": headers}
        if has_payload:
            if isinstance(payload, str):
                kwargs["content"] = payload
            else:
                kwargs["json"] = payload
        elif body:
            kwargs["json"] = body
        return kwargs


def _param_str(value: Any) -> str:
    """restJson1 text form for path/query/header values."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class SmithyTarget(Target):
    def __init__(self, cfg: SmithyTargetConfig, gw: GatewayConfig) -> None:
        self._cfg = cfg
        ast = gw.smithy_model(cfg)
        size = len(json.dumps(ast).encode())
        if size > _MAX_MODEL_BYTES:
            raise ValueError(
                f"smithy target {cfg.name!r}: model is {size} bytes, over the real gateway's "
                f"{_MAX_MODEL_BYTES}-byte (10 MB) limit"
            )
        model = _Model(cfg.name, ast)
        self._ops: dict[str, _Operation] = {}
        for op_id in model.operation_ids():
            op = _Operation(model, op_id)
            if op.name in self._ops:
                raise ValueError(f"smithy target {cfg.name!r}: duplicate operation name {op.name!r}")
            self._ops[op.name] = op
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            timeout=cfg.timeout_sec,
            auth=_build_smithy_auth(cfg.auth),
        )

    @property
    def name(self) -> str:
        return self._cfg.name

    def list_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name=op.name,
                description=op.description,
                input_schema=op.input_schema,
                output_schema=op.output_schema,
            )
            for op in self._ops.values()
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> ToolOutcome:
        op = self._ops.get(tool_name)
        if op is None:
            return ToolOutcome(
                payload={
                    "errorMessage": f"unknown tool {tool_name!r} on target {self._cfg.name!r}",
                    "errorType": "ToolNotFound",
                },
                is_error=True,
            )
        try:
            resp = await self._client.request(**op.request(arguments))
        except Exception as exc:  # noqa: BLE001  # any failure -> tool error
            return ToolOutcome(
                payload={"errorMessage": str(exc), "errorType": type(exc).__name__},
                is_error=True,
            )
        if resp.status_code >= 300:
            return ToolOutcome(
                payload={
                    "errorMessage": f"{op.method} {op.uri} returned HTTP {resp.status_code}: {resp.text[:500]}",
                    "errorType": "HttpError",
                },
                is_error=True,
            )
        try:
            payload = resp.json()
        except ValueError:
            payload = resp.text  # non-JSON body -> pass through as text
        return ToolOutcome(payload=payload, is_error=False)

    async def aclose(self) -> None:
        await self._client.aclose()
