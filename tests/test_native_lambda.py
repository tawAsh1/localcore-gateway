from __future__ import annotations

import textwrap

from localcore_gateway.config import LambdaFunctionConfig
from localcore_gateway.lambda_emu.native import NativeLambdaInvoker

HANDLER = textwrap.dedent(
    """
    def handler(event, context):
        tool = context.client_context.custom.get("bedrockAgentCoreToolName")
        print(f"hello from {tool}")
        if tool == "boom":
            raise RuntimeError("nope")
        return {"tool": tool, "event": event,
                "fn": context.function_name,
                "mem": context.memory_limit_in_mb}
    """
)


def _cfg(tmp_path) -> LambdaFunctionConfig:
    (tmp_path / "h.py").write_text(HANDLER)
    return LambdaFunctionConfig(
        backend="native",
        handler="h.handler",
        code_root=str(tmp_path),
        function_name="t-fn",
        memory_mb=256,
    )


async def test_event_context_and_client_context(tmp_path):
    inv = NativeLambdaInvoker(_cfg(tmp_path), code_root=str(tmp_path))
    res = await inv.invoke({"x": 1}, client_context={"bedrockAgentCoreToolName": "echo"})
    assert not res.errored
    assert res.payload == {
        "tool": "echo",
        "event": {"x": 1},
        "fn": "t-fn",
        "mem": "256",
    }


async def test_cloudwatch_log_framing(tmp_path):
    inv = NativeLambdaInvoker(_cfg(tmp_path), code_root=str(tmp_path))
    res = await inv.invoke({}, client_context={"bedrockAgentCoreToolName": "x"})
    text = "\n".join(res.logs)
    assert res.logs[0].startswith("START RequestId: ")
    assert "hello from x" in text
    assert any(ln.startswith("END RequestId: ") for ln in res.logs)
    assert any(ln.startswith("REPORT RequestId: ") for ln in res.logs)


async def test_error_envelope(tmp_path):
    inv = NativeLambdaInvoker(_cfg(tmp_path), code_root=str(tmp_path))
    res = await inv.invoke({}, client_context={"bedrockAgentCoreToolName": "boom"})
    assert res.errored
    assert res.function_error == "Unhandled"
    assert res.payload["errorType"] == "RuntimeError"
    assert res.payload["errorMessage"] == "nope"
    assert isinstance(res.payload["stackTrace"], list)


async def test_soft_timeout(tmp_path):
    (tmp_path / "slow.py").write_text("import time\ndef handler(e, c):\n    time.sleep(5)\n    return 1\n")
    cfg = LambdaFunctionConfig(
        backend="native",
        handler="slow.handler",
        code_root=str(tmp_path),
        timeout_sec=0.3,
    )
    inv = NativeLambdaInvoker(cfg, code_root=str(tmp_path))
    res = await inv.invoke({}, client_context={})
    assert res.errored
    assert res.payload["errorType"] == "TimeoutError"
