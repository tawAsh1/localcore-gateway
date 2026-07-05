from __future__ import annotations

import json
import sys
from collections.abc import Iterator

import pytest
from botocore.credentials import Credentials
from fastmcp import Client

from conftest import serve_asgi
from localcore_gateway.config import GatewayConfig
from localcore_gateway.gateway import build_gateway
from localcore_gateway.preflight import preflight
from localcore_gateway.targets.smithy_target import SmithyTarget
from localcore_gateway.testing import call_tool, serve_gateway

# The AWS doc's weather-service example, lightly adapted (a second operation
# exercises httpLabel / httpHeader / body binding).
WEATHER_MODEL = {
    "smithy": "2.0",
    "shapes": {
        "example.weather#Weather": {
            "type": "service",
            "version": "2006-03-01",
            "traits": {"aws.protocols#restJson1": {}},
            "operations": [
                {"target": "example.weather#GetCurrentWeather"},
                {"target": "example.weather#CreateReport"},
            ],
        },
        "example.weather#GetCurrentWeather": {
            "type": "operation",
            "input": {"target": "example.weather#GetCurrentWeatherInput"},
            "output": {"target": "example.weather#GetCurrentWeatherOutput"},
            "traits": {
                "smithy.api#http": {"method": "GET", "uri": "/weather", "code": 200},
                "smithy.api#documentation": "Get current weather.",
            },
        },
        "example.weather#GetCurrentWeatherInput": {
            "type": "structure",
            "members": {
                "location": {
                    "target": "smithy.api#String",
                    "traits": {
                        "smithy.api#required": {},
                        "smithy.api#httpQuery": "location",
                        "smithy.api#documentation": "City name.",
                    },
                },
                "units": {
                    "target": "example.weather#TemperatureUnits",
                    "traits": {"smithy.api#httpQuery": "units", "smithy.api#default": "celsius"},
                },
            },
        },
        "example.weather#TemperatureUnits": {
            "type": "enum",
            "members": {
                "CELSIUS": {"target": "smithy.api#Unit", "traits": {"smithy.api#enumValue": "celsius"}},
                "FAHRENHEIT": {"target": "smithy.api#Unit", "traits": {"smithy.api#enumValue": "fahrenheit"}},
            },
        },
        "example.weather#GetCurrentWeatherOutput": {
            "type": "structure",
            "members": {
                "temperature": {"target": "smithy.api#Float"},
                "conditions": {"target": "smithy.api#String"},
            },
        },
        "example.weather#CreateReport": {
            "type": "operation",
            "input": {"target": "example.weather#CreateReportInput"},
            "traits": {"smithy.api#http": {"method": "POST", "uri": "/stations/{stationId}/reports", "code": 201}},
        },
        "example.weather#CreateReportInput": {
            "type": "structure",
            "members": {
                "stationId": {
                    "target": "smithy.api#String",
                    "traits": {"smithy.api#required": {}, "smithy.api#httpLabel": {}},
                },
                "requestId": {"target": "smithy.api#String", "traits": {"smithy.api#httpHeader": "X-Request-Id"}},
                "temperature": {"target": "smithy.api#Float"},
                "notes": {"target": "smithy.api#String"},
            },
        },
    },
}


@pytest.fixture
def spy() -> Iterator[tuple[str, dict]]:
    """An echo ASGI server recording every request; status/body settable."""
    state: dict = {"status": 200, "body": {"ok": True}, "requests": []}

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        state["requests"].append(
            {
                "method": scope["method"],
                "path": scope["path"],
                "query": scope["query_string"].decode(),
                "headers": {k.decode().lower(): v.decode() for k, v in scope["headers"]},
                "body": body.decode(),
            }
        )
        payload = json.dumps(state["body"]).encode()
        await send(
            {
                "type": "http.response.start",
                "status": state["status"],
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    with serve_asgi(app) as base:
        yield base, state


def _target(base_url: str, **extra) -> SmithyTarget:
    gw = GatewayConfig.model_validate(
        {"targets": [{"type": "smithy", "name": "weather", "model": WEATHER_MODEL, "base_url": base_url, **extra}]}
    )
    return SmithyTarget(gw.targets[0], gw)


# --- discovery ---


async def test_discovery_naming_and_schemas():
    gw = GatewayConfig.model_validate(
        {"targets": [{"type": "smithy", "name": "weather", "model": WEATHER_MODEL, "base_url": "http://h"}]}
    )
    mcp, targets = build_gateway(gw)
    try:
        async with Client(mcp) as c:
            tools = {t.name: t for t in await c.list_tools()}
        # Operation shape name without its namespace, target-prefixed.
        assert "weather___GetCurrentWeather" in tools
        schema = tools["weather___GetCurrentWeather"].inputSchema
        assert schema["required"] == ["location"]
        assert schema["properties"]["location"]["description"] == "City name."
        assert schema["properties"]["units"] == {
            "type": "string",
            "enum": ["celsius", "fahrenheit"],
            "default": "celsius",
        }
        assert tools["weather___GetCurrentWeather"].outputSchema["properties"]["temperature"] == {"type": "number"}
    finally:
        for t in targets:
            await t.aclose()


# --- invocation: restJson1 bindings ---


async def test_get_operation_binds_query_params(spy):
    base, state = spy
    tgt = _target(base)
    try:
        out = await tgt.call_tool("GetCurrentWeather", {"location": "Seattle", "units": "fahrenheit"})
        assert not out.is_error
        assert out.payload == {"ok": True}
        req = state["requests"][-1]
        assert req["method"] == "GET"
        assert req["path"] == "/weather"
        assert "location=Seattle" in req["query"]
        assert "units=fahrenheit" in req["query"]
    finally:
        await tgt.aclose()


async def test_post_operation_binds_label_header_and_body(spy):
    base, state = spy
    tgt = _target(base)
    try:
        out = await tgt.call_tool(
            "CreateReport",
            {"stationId": "st-1", "requestId": "r9", "temperature": 21.5, "notes": "windy"},
        )
        assert not out.is_error
        req = state["requests"][-1]
        assert req["method"] == "POST"
        assert req["path"] == "/stations/st-1/reports"  # httpLabel interpolated
        assert req["headers"]["x-request-id"] == "r9"  # httpHeader
        assert json.loads(req["body"]) == {"temperature": 21.5, "notes": "windy"}  # unbound -> JSON body
    finally:
        await tgt.aclose()


async def test_non_2xx_maps_to_error_envelope(spy):
    base, state = spy
    state["status"] = 503
    state["body"] = {"message": "downstream unavailable"}
    tgt = _target(base)
    try:
        out = await tgt.call_tool("GetCurrentWeather", {"location": "x"})
        assert out.is_error
        assert out.payload["errorType"] == "HttpError"
        assert "HTTP 503" in out.payload["errorMessage"]
    finally:
        await tgt.aclose()


async def test_unknown_tool_mapping(spy):
    base, _state = spy
    tgt = _target(base)
    try:
        out = await tgt.call_tool("Nope", {})
        assert out.is_error
        assert out.payload["errorType"] == "ToolNotFound"
    finally:
        await tgt.aclose()


# --- schema conversion units ---


def _model_with(shapes: dict) -> dict:
    return {
        "smithy": "2.0",
        "shapes": {
            "x#Svc": {
                "type": "service",
                "version": "1",
                "traits": {"aws.protocols#restJson1": {}},
                "operations": [{"target": "x#Op"}],
            },
            "x#Op": {
                "type": "operation",
                "input": {"target": "x#Input"},
                "traits": {"smithy.api#http": {"method": "POST", "uri": "/op"}},
            },
            **shapes,
        },
    }


def _input_schema(shapes: dict) -> dict:
    gw = GatewayConfig.model_validate(
        {"targets": [{"type": "smithy", "name": "t", "model": _model_with(shapes), "base_url": "http://h"}]}
    )
    tgt = SmithyTarget(gw.targets[0], gw)
    return tgt.list_tools()[0].input_schema


def test_schema_conversion_composites():
    schema = _input_schema(
        {
            "x#Input": {
                "type": "structure",
                "members": {
                    "nested": {"target": "x#Nested"},
                    "tags": {"target": "x#TagList"},
                    "attrs": {"target": "x#AttrMap"},
                    "when": {"target": "smithy.api#Timestamp"},
                    "data": {"target": "smithy.api#Blob"},
                },
            },
            "x#Nested": {"type": "structure", "members": {"id": {"target": "smithy.api#Long"}}},
            "x#TagList": {"type": "list", "member": {"target": "smithy.api#String"}},
            "x#AttrMap": {
                "type": "map",
                "key": {"target": "smithy.api#String"},
                "value": {"target": "smithy.api#Integer"},
            },
        }
    )
    props = schema["properties"]
    assert props["nested"] == {"type": "object", "properties": {"id": {"type": "integer"}}}
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert props["attrs"] == {"type": "object", "additionalProperties": {"type": "integer"}}
    assert props["when"] == {"type": "string", "format": "date-time"}
    assert props["data"] == {"type": "string", "contentEncoding": "base64"}


def test_schema_conversion_recursive_shape_is_safe():
    schema = _input_schema(
        {
            "x#Input": {"type": "structure", "members": {"root": {"target": "x#Node"}}},
            "x#Node": {
                "type": "structure",
                "members": {"name": {"target": "smithy.api#String"}, "next": {"target": "x#Node"}},
            },
        }
    )
    node = schema["properties"]["root"]
    assert node["properties"]["name"] == {"type": "string"}
    assert node["properties"]["next"] == {}  # recursion collapses to a bare schema


def test_union_shape_rejected():
    with pytest.raises(ValueError, match="unsupported type 'union'"):
        _input_schema(
            {
                "x#Input": {"type": "structure", "members": {"choice": {"target": "x#Choice"}}},
                "x#Choice": {"type": "union", "members": {"a": {"target": "smithy.api#String"}}},
            }
        )


def test_streaming_operation_rejected():
    shapes = {
        "x#Input": {"type": "structure", "members": {"stream": {"target": "x#StreamBlob"}}},
        "x#StreamBlob": {"type": "blob", "traits": {"smithy.api#streaming": {}}},
    }
    gw = GatewayConfig.model_validate(
        {"targets": [{"type": "smithy", "name": "t", "model": _model_with(shapes), "base_url": "http://h"}]}
    )
    with pytest.raises(ValueError, match="streaming operations are unsupported"):
        SmithyTarget(gw.targets[0], gw)


# --- config / build validation ---


def test_model_and_model_file_exactly_one(tmp_path):
    with pytest.raises(ValueError, match="exactly one of"):
        GatewayConfig.model_validate({"targets": [{"type": "smithy", "name": "t", "base_url": "http://h"}]})
    path = tmp_path / "m.json"
    path.write_text(json.dumps(WEATHER_MODEL))
    with pytest.raises(ValueError, match="exactly one of"):
        GatewayConfig.model_validate(
            {
                "targets": [
                    {
                        "type": "smithy",
                        "name": "t",
                        "model": WEATHER_MODEL,
                        "model_file": str(path),
                        "base_url": "http://h",
                    }
                ]
            }
        )


def test_base_url_required():
    with pytest.raises(ValueError, match="base_url"):
        GatewayConfig.model_validate({"targets": [{"type": "smithy", "name": "t", "model": WEATHER_MODEL}]})


def test_non_restjson1_service_rejected():
    model = json.loads(json.dumps(WEATHER_MODEL))
    model["shapes"]["example.weather#Weather"]["traits"] = {"aws.protocols#restXml": {}}
    gw = GatewayConfig.model_validate(
        {"targets": [{"type": "smithy", "name": "t", "model": model, "base_url": "http://h"}]}
    )
    with pytest.raises(ValueError, match="restJson1"):
        SmithyTarget(gw.targets[0], gw)


def _oversized_model() -> dict:
    model = json.loads(json.dumps(WEATHER_MODEL))
    model["shapes"]["example.weather#Weather"]["traits"]["smithy.api#documentation"] = "x" * 10_000_001
    return model


def test_oversized_model_rejected_at_build():
    gw = GatewayConfig.model_validate(
        {"targets": [{"type": "smithy", "name": "t", "model": _oversized_model(), "base_url": "http://h"}]}
    )
    with pytest.raises(ValueError, match="10 MB"):
        SmithyTarget(gw.targets[0], gw)


def test_sigv4_requires_service():
    with pytest.raises(ValueError, match=r"auth\.service is required"):
        GatewayConfig.model_validate(
            {
                "targets": [
                    {
                        "type": "smithy",
                        "name": "t",
                        "model": WEATHER_MODEL,
                        "base_url": "http://h",
                        "auth": {"type": "sigv4", "region": "us-east-1"},
                    }
                ]
            }
        )


def test_sigv4_without_boto3_says_install_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)  # simulate missing extra
    gw = GatewayConfig.model_validate(
        {
            "targets": [
                {
                    "type": "smithy",
                    "name": "t",
                    "model": WEATHER_MODEL,
                    "base_url": "http://h",
                    "auth": {"type": "sigv4", "region": "us-east-1", "service": "lambda"},
                }
            ]
        }
    )
    with pytest.raises(ValueError, match=r"pip install 'localcore-gateway\[aws\]'"):
        SmithyTarget(gw.targets[0], gw)


# --- sigv4 signing smoke ---


async def test_sigv4_signs_for_the_configured_service(spy, monkeypatch):
    base, state = spy
    monkeypatch.setattr(
        "boto3.session.Session.get_credentials",
        lambda _self: Credentials("AKIDEXAMPLE", "SECRET"),
    )
    tgt = _target(base, auth={"type": "sigv4", "region": "us-east-1", "service": "lambda"})
    try:
        out = await tgt.call_tool("CreateReport", {"stationId": "s", "temperature": 1.0})
        assert not out.is_error
        auth = state["requests"][-1]["headers"]["authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
        assert "/us-east-1/lambda/aws4_request" in auth  # the ACTUAL service scope
        assert "x-amz-date" in state["requests"][-1]["headers"]
    finally:
        await tgt.aclose()


# --- preflight ---


def test_preflight_smithy_is_deployable_and_size_checked():
    cfg = GatewayConfig.model_validate(
        {"targets": [{"type": "smithy", "name": "weather", "model": WEATHER_MODEL, "base_url": "http://h"}]}
    )
    assert preflight(cfg) == []  # deployable: no NOTICE, nothing else to flag
    cfg = GatewayConfig.model_validate(
        {"targets": [{"type": "smithy", "name": "weather", "model": _oversized_model(), "base_url": "http://h"}]}
    )
    findings = preflight(cfg)
    assert [f.severity for f in findings] == ["ERROR"]
    assert "10 MB" in findings[0].message


# --- end to end ---


def test_end_to_end_through_serve_gateway(spy):
    base, state = spy
    state["body"] = {"temperature": 11.5, "conditions": "rain"}
    config = {"targets": [{"type": "smithy", "name": "weather", "model": WEATHER_MODEL, "base_url": base}]}
    with serve_gateway(config) as gw:
        out = call_tool(gw.url, "weather___GetCurrentWeather", {"location": "Osaka"})
    assert out == {"temperature": 11.5, "conditions": "rain"}
