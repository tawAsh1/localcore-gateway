from __future__ import annotations

import base64
import json

import httpx

from localcore_gateway.config import LambdaFunctionConfig
from localcore_gateway.lambda_emu.sam import SamLambdaInvoker

ENDPOINT = "http://sam.local:3001"


def _invoker(handler) -> SamLambdaInvoker:
    cfg = LambdaFunctionConfig(
        backend="sam",
        sam_function="DemoFn",
        sam_endpoint=ENDPOINT,
        timeout_sec=5,
    )
    inv = SamLambdaInvoker(cfg)
    inv._client = httpx.AsyncClient(base_url=ENDPOINT, transport=httpx.MockTransport(handler))
    return inv


async def test_translation_success_and_client_context():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["inv_type"] = request.headers.get("X-Amz-Invocation-Type")
        seen["body"] = json.loads(request.content)
        cc = request.headers["X-Amz-Client-Context"]
        seen["cc"] = json.loads(base64.b64decode(cc))
        return httpx.Response(200, json={"ok": True})

    inv = _invoker(handler)
    res = await inv.invoke({"x": 1}, client_context={"bedrockAgentCoreToolName": "echo"})
    await inv.aclose()

    assert seen["method"] == "POST"
    assert seen["path"] == "/2015-03-31/functions/DemoFn/invocations"
    assert seen["inv_type"] == "RequestResponse"
    assert seen["body"] == {"x": 1}
    assert seen["cc"] == {"custom": {"bedrockAgentCoreToolName": "echo"}}
    assert res.payload == {"ok": True}
    assert not res.errored


async def test_function_error_header_maps_to_errored():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Amz-Function-Error": "Unhandled"},
            json={"errorMessage": "boom"},
        )

    inv = _invoker(handler)
    res = await inv.invoke({}, client_context=None)
    await inv.aclose()

    assert res.errored
    assert res.function_error == "Unhandled"
    assert res.payload == {"errorMessage": "boom"}


async def test_non_json_5xx_falls_back_to_text_and_errors():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    inv = _invoker(handler)
    res = await inv.invoke({}, client_context=None)
    await inv.aclose()

    assert res.payload == "bad gateway"
    assert res.errored
    assert res.function_error == "Unhandled"


async def test_no_client_context_omits_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "X-Amz-Client-Context" not in request.headers
        return httpx.Response(200, json={"ok": True})

    inv = _invoker(handler)
    res = await inv.invoke({"a": 1}, client_context=None)
    await inv.aclose()
    assert not res.errored
