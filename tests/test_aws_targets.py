from __future__ import annotations

import base64
import io
import json
import sys
from collections.abc import Iterator

import pytest
from botocore.credentials import Credentials
from botocore.response import StreamingBody
from botocore.stub import Stubber
from fastmcp import Client, FastMCP

from conftest import serve_asgi
from localcore_gateway.aws_deps import require_boto3
from localcore_gateway.config import GatewayConfig, LambdaFunctionConfig
from localcore_gateway.gateway import build_gateway, sync_targets
from localcore_gateway.history import InvocationLog
from localcore_gateway.lambda_emu.aws import AwsLambdaInvoker
from localcore_gateway.targets.aws_gateway_target import AWSGatewayTarget

# ==========================================================================
# type: aws-gateway -- proxy a REAL AgentCore Gateway (spy server, no AWS)
# ==========================================================================


def _make_remote_gateway() -> FastMCP:
    """Stands in for a deployed gateway: tools already carry prefixed names."""
    srv = FastMCP("remote-gateway")

    @srv.tool(name="orders___lookup")
    def lookup(order_id: str) -> dict:
        return {"order": order_id, "status": "shipped"}

    @srv.tool(name="orders___cancel")
    def cancel(order_id: str) -> dict:
        return {"order": order_id, "status": "cancelled"}

    _ = lookup, cancel
    return srv


@pytest.fixture
def remote_spy() -> Iterator[tuple[str, FastMCP, dict]]:
    """A fake deployed gateway plus a request spy; yields (mcp_url, server, seen)."""
    seen: dict = {}
    srv = _make_remote_gateway()
    inner = srv.http_app(path="/mcp")

    async def app(scope, receive, send):
        if scope["type"] == "http":
            seen["headers"] = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        await inner(scope, receive, send)

    with serve_asgi(app) as base:
        yield f"{base}/mcp", srv, seen


def _fake_creds(monkeypatch) -> None:
    """Static credentials; no profile/env chain lookup in CI."""
    monkeypatch.setattr(
        "boto3.session.Session.get_credentials",
        lambda _self: Credentials("AKIDEXAMPLE", "SECRET"),
    )


def _aws_gw_config(url: str, **extra) -> GatewayConfig:
    return GatewayConfig.model_validate({"targets": [{"type": "aws-gateway", "name": "prod", "url": url, **extra}]})


async def test_sigv4_signs_discovery_and_calls(remote_spy, monkeypatch):
    url, _srv, seen = remote_spy
    _fake_creds(monkeypatch)
    gw = _aws_gw_config(url, auth={"type": "sigv4", "region": "us-east-1"})
    tgt = AWSGatewayTarget(gw.targets[0], gw)
    try:
        # Discovery already went out signed.
        auth = seen["headers"]["authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
        assert "/us-east-1/bedrock-agentcore/aws4_request" in auth
        assert "SignedHeaders=" in auth
        assert "Signature=" in auth
        assert "x-amz-date" in seen["headers"]
        # A tool call round-trips (the signed POST body and its
        # Content-Length must be consistent for the server to parse it).
        out = await tgt.call_tool("orders___lookup", {"order_id": "42"})
        assert not out.is_error
        assert out.payload == {"order": "42", "status": "shipped"}
    finally:
        await tgt.aclose()


async def test_bearer_auth_header_sent(remote_spy):
    url, _srv, seen = remote_spy
    gw = _aws_gw_config(url, auth={"type": "bearer", "value": "jwt-token"})
    tgt = AWSGatewayTarget(gw.targets[0], gw)
    try:
        assert tgt.list_tools()
        assert seen["headers"]["authorization"] == "Bearer jwt-token"
    finally:
        await tgt.aclose()


async def test_tools_exposed_verbatim_without_local_prefix(remote_spy):
    url, _srv, _seen = remote_spy
    mcp, targets = build_gateway(_aws_gw_config(url))
    try:
        async with Client(mcp) as c:
            names = {t.name for t in await c.list_tools()}
        # Verbatim remote names -- NOT prod___orders___lookup.
        assert {"orders___lookup", "orders___cancel"} <= names
        assert not any(n.startswith("prod___") for n in names)
    finally:
        for t in targets:
            await t.aclose()


async def test_allowlist_filters_remote_tools(remote_spy):
    url, _srv, _seen = remote_spy
    gw = _aws_gw_config(url, tools=["orders___lookup"])
    tgt = AWSGatewayTarget(gw.targets[0], gw)
    try:
        assert [td.name for td in tgt.list_tools()] == ["orders___lookup"]
    finally:
        await tgt.aclose()


async def test_collision_with_local_target_rejected_at_build(remote_spy, tmp_path):
    url, _srv, _seen = remote_spy
    (tmp_path / "h.py").write_text("def handler(e, c): return {}\n")
    gw = GatewayConfig.model_validate(
        {
            "targets": [
                # A local lambda target whose prefixed name equals a remote one.
                {
                    "type": "lambda",
                    "name": "orders",
                    "lambda": {"backend": "native", "handler": "h.handler"},
                    "tools": [{"name": "lookup"}],
                },
                {"type": "aws-gateway", "name": "prod", "url": url},
            ]
        }
    )
    gw.source_dir = str(tmp_path)
    with pytest.raises(ValueError, match=r"collision.*orders___lookup"):
        build_gateway(gw)


async def test_resync_inherited_from_mcp_target(remote_spy):
    url, srv, _seen = remote_spy
    mcp, targets = build_gateway(_aws_gw_config(url))
    history = InvocationLog(10)
    try:

        @srv.tool(name="billing___invoice")
        def invoice(order_id: str) -> dict:
            return {"invoice": order_id}

        _ = invoice
        res = await sync_targets(mcp, targets, history)
        assert res["prod"] == {"added": ["billing___invoice"], "removed": [], "updated": []}
        async with Client(mcp) as c:
            assert "billing___invoice" in {t.name for t in await c.list_tools()}
    finally:
        for t in targets:
            await t.aclose()


async def test_resync_collision_reported_in_sync_response(remote_spy, tmp_path):
    url, srv, _seen = remote_spy
    (tmp_path / "h.py").write_text("def handler(e, c): return {}\n")
    gw = GatewayConfig.model_validate(
        {
            "targets": [
                {
                    "type": "lambda",
                    "name": "st",
                    "lambda": {"backend": "native", "handler": "h.handler"},
                    "tools": [{"name": "ping"}],
                },
                {"type": "aws-gateway", "name": "prod", "url": url},
            ]
        }
    )
    gw.source_dir = str(tmp_path)
    mcp, targets = build_gateway(gw)
    history = InvocationLog(10)
    try:
        # The remote gateway grows a tool whose verbatim name collides with
        # the local lambda target's prefixed one.
        @srv.tool(name="st___ping")
        def ping() -> str:
            return "remote pong"

        _ = ping
        res = await sync_targets(mcp, targets, history)
        assert res["st"] == "static"
        assert "collision" in res["prod"]["error"]
        assert "st___ping" in res["prod"]["error"]
    finally:
        for t in targets:
            await t.aclose()


def test_sigv4_without_boto3_says_install_the_extra(remote_spy, monkeypatch):
    url, _srv, _seen = remote_spy
    monkeypatch.setitem(sys.modules, "boto3", None)  # simulate missing extra
    gw = _aws_gw_config(url, auth={"type": "sigv4", "region": "us-east-1"})
    with pytest.raises(ValueError, match=r"pip install 'localcore-gateway\[aws\]'"):
        AWSGatewayTarget(gw.targets[0], gw)


# ==========================================================================
# lambda backend: aws -- invoke a deployed function (botocore Stubber)
# ==========================================================================


def _aws_invoker() -> tuple[AwsLambdaInvoker, Stubber]:
    require_boto3()  # dev group always has it; fail loudly if not
    cfg = LambdaFunctionConfig(backend="aws", aws_function="my-fn", region="us-east-1", timeout_sec=5)
    inv = AwsLambdaInvoker(cfg)
    return inv, Stubber(inv._client)


def _payload_stream(obj) -> StreamingBody:
    raw = json.dumps(obj).encode()
    return StreamingBody(io.BytesIO(raw), len(raw))


async def test_invoke_sends_agentcore_contract_and_returns_payload():
    inv, stub = _aws_invoker()
    event = {"a": 1, "b": 2}
    cc = {"bedrockAgentCoreToolName": "demo___add"}
    expected_cc = base64.b64encode(json.dumps({"custom": cc}).encode()).decode()
    stub.add_response(
        "invoke",
        {"StatusCode": 200, "Payload": _payload_stream({"sum": 3})},
        expected_params={
            "FunctionName": "my-fn",
            "Payload": json.dumps(event).encode(),
            "ClientContext": expected_cc,
            "LogType": "Tail",
        },
    )
    with stub:
        result = await inv.invoke(event, client_context=cc)
    assert not result.errored
    assert result.payload == {"sum": 3}


async def test_function_error_maps_to_error_envelope():
    inv, stub = _aws_invoker()
    envelope = {"errorMessage": "boom", "errorType": "ValueError", "stackTrace": ["l1"]}
    stub.add_response(
        "invoke",
        {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": _payload_stream(envelope)},
        expected_params={"FunctionName": "my-fn", "Payload": b"{}", "LogType": "Tail"},
    )
    with stub:
        result = await inv.invoke({})
    assert result.errored
    assert result.function_error == "Unhandled"
    assert result.payload == envelope  # AWS's envelope passes through as-is


async def test_log_result_decoded_into_logs():
    inv, stub = _aws_invoker()
    log_text = "START RequestId: 1\nhello from lambda\nEND RequestId: 1"
    stub.add_response(
        "invoke",
        {
            "StatusCode": 200,
            "Payload": _payload_stream({}),
            "LogResult": base64.b64encode(log_text.encode()).decode(),
        },
        expected_params={"FunctionName": "my-fn", "Payload": b"{}", "LogType": "Tail"},
    )
    with stub:
        result = await inv.invoke({})
    assert result.logs == ["START RequestId: 1", "hello from lambda", "END RequestId: 1"]


async def test_botocore_error_maps_to_error_envelope():
    inv, stub = _aws_invoker()
    stub.add_client_error(
        "invoke",
        service_error_code="TooManyRequestsException",
        service_message="Rate exceeded",
        http_status_code=429,
    )
    with stub:
        result = await inv.invoke({})
    assert result.errored
    assert result.function_error == "Unhandled"
    assert "Rate exceeded" in result.payload["errorMessage"]
    assert result.payload["errorType"].endswith(("ClientError", "TooManyRequestsException"))


def test_backend_aws_requires_aws_function():
    with pytest.raises(ValueError, match="aws_function is required"):
        LambdaFunctionConfig(backend="aws")


def test_backend_aws_without_boto3_says_install_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)  # simulate missing extra
    cfg = LambdaFunctionConfig(backend="aws", aws_function="my-fn")
    with pytest.raises(ValueError, match=r"pip install 'localcore-gateway\[aws\]'"):
        AwsLambdaInvoker(cfg)
