"""SAM-local Lambda backend.

Drives a running ``sam local start-lambda`` endpoint via the real AWS Lambda
Invoke API (``POST /2015-03-31/functions/{name}/invocations``). Because SAM
runs the handler inside the genuine AWS Lambda Linux runtime image, this is
byte-for-byte the production runtime (true isolation + hard timeout).

Prereqs (user-managed, out of process):

    sam local start-lambda            # in the SAM project dir

``bedrockAgentCoreToolName`` is delivered via the standard Lambda
``X-Amz-Client-Context`` header (base64 JSON), exactly as real Lambda does.
Per-invoke logs surface in the ``sam local`` console (out-of-band for the
Invoke API), so this backend reports only an invoke summary.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from localcore_gateway.config import LambdaFunctionConfig
from localcore_gateway.lambda_emu.base import InvokeResult, LambdaInvoker


class SamLambdaInvoker(LambdaInvoker):
    def __init__(self, cfg: LambdaFunctionConfig) -> None:
        self._cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.sam_endpoint.rstrip("/"),
            timeout=cfg.timeout_sec + 15.0,
        )

    async def invoke(
        self,
        event: Any,
        *,
        client_context: dict[str, Any] | None = None,
    ) -> InvokeResult:
        fn = self._cfg.sam_function
        url = f"/2015-03-31/functions/{fn}/invocations"
        headers = {
            "Content-Type": "application/json",
            "X-Amz-Invocation-Type": "RequestResponse",
        }
        if client_context:
            cc = json.dumps({"custom": client_context}).encode()
            headers["X-Amz-Client-Context"] = base64.b64encode(cc).decode()

        resp = await self._client.post(url, content=json.dumps(event).encode(), headers=headers)
        function_error = resp.headers.get("X-Amz-Function-Error")
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            payload = resp.text  # non-JSON body -> pass through as text

        logs = [
            f"[sam] invoked {fn} via {self._cfg.sam_endpoint} "
            f"(HTTP {resp.status_code}); see `sam local` console for logs"
        ]
        return InvokeResult(
            payload=payload,
            function_error=function_error or ("Unhandled" if resp.status_code >= 300 else None),
            logs=logs,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
