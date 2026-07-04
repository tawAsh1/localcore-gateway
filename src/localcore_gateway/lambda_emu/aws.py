"""Real-AWS Lambda backend: invoke a **deployed** function.

The gateway still runs locally; the handler runs in real AWS Lambda (hybrid
debugging -- nothing is emulated). Same AgentCore contract as native/sam:
event = tool arguments, ``bedrockAgentCoreToolName`` delivered via the
standard Lambda ClientContext (base64 JSON, ``{"custom": {...}}`` -- the same
wire format sam sends in ``X-Amz-Client-Context``). ``LogType="Tail"`` pulls
the invocation's last 4 KB of CloudWatch logs into the usual logs channel.

Requires the ``aws`` extra (boto3) and AWS credentials (``aws_profile`` or
the default chain). Retries are disabled (``max_attempts: 0``): a tool invoke
is side-effecting, so botocore's silent retry-on-timeout could double-invoke.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from localcore_gateway.aws_deps import require_boto3
from localcore_gateway.config import LambdaFunctionConfig
from localcore_gateway.lambda_emu.base import InvokeResult, LambdaInvoker


class AwsLambdaInvoker(LambdaInvoker):
    def __init__(self, cfg: LambdaFunctionConfig) -> None:
        boto3 = require_boto3()
        from botocore.config import Config

        self._cfg = cfg
        session = boto3.Session(profile_name=cfg.aws_profile, region_name=cfg.region)
        # timeout_sec caps the HTTP read; retries off (see module docstring).
        self._client = session.client(
            "lambda",
            config=Config(read_timeout=cfg.timeout_sec, retries={"max_attempts": 0}),
        )

    async def invoke(
        self,
        event: Any,
        *,
        client_context: dict[str, Any] | None = None,
    ) -> InvokeResult:
        kwargs: dict[str, Any] = {
            "FunctionName": self._cfg.aws_function,
            "Payload": json.dumps(event).encode(),
            "LogType": "Tail",
        }
        if client_context:
            cc = json.dumps({"custom": client_context}).encode()
            kwargs["ClientContext"] = base64.b64encode(cc).decode()

        try:
            # boto3 is blocking; keep the gateway's event loop free.
            resp = await asyncio.to_thread(self._client.invoke, **kwargs)
        except Exception as exc:  # noqa: BLE001  # throttle/denied/timeout -> error envelope
            return InvokeResult(
                payload={"errorMessage": str(exc), "errorType": type(exc).__name__},
                function_error="Unhandled",
                logs=[],
            )

        raw = resp["Payload"].read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw.decode(errors="replace")  # non-JSON body -> pass through as text

        logs: list[str] = []
        if resp.get("LogResult"):
            logs = base64.b64decode(resp["LogResult"]).decode(errors="replace").splitlines()

        # FunctionError payloads already ARE the Lambda error envelope
        # (errorMessage / errorType / stackTrace) -- pass through, same as
        # the native worker produces.
        return InvokeResult(
            payload=payload,
            function_error=resp.get("FunctionError"),
            logs=logs,
        )
