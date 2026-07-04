from __future__ import annotations

import logging

import pytest
from fastmcp import Client

from localcore_gateway.config import GatewayConfig
from localcore_gateway.gateway import build_gateway
from localcore_gateway.history import InvocationLog


def _cfg(contract_checks: str) -> GatewayConfig:
    # A mock target is the vehicle: its canned response deliberately violates
    # its own declared output schema, and its input schema requires an int.
    return GatewayConfig.model_validate(
        {
            "server": {"contract_checks": contract_checks},
            "targets": [
                {
                    "type": "mock",
                    "name": "m",
                    "tools": [
                        {
                            "name": "bad",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"a": {"type": "integer"}},
                                "required": ["a"],
                            },
                            "outputSchema": {
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                            },
                            "response": {"ok": "not-a-bool"},
                        },
                        {"name": "good", "response": "fine"},
                    ],
                }
            ],
        }
    )


async def _call(cfg, name, args, history=None):
    mcp, targets = build_gateway(cfg, history=history)
    try:
        async with Client(mcp) as c:
            return await c.call_tool(name, args, raise_on_error=False)
    finally:
        for t in targets:
            await t.aclose()


@pytest.fixture
def raw_results(monkeypatch):
    """Disable the python MCP SDK's CLIENT-side output validation.

    The SDK's ClientSession independently validates non-error results against
    the advertised outputSchema (that's the CONSUMER's stack, out of the
    gateway's hands -- a python-SDK agent behaves the same against the real
    gateway). Off here so pass-through tests observe what the gateway sent.
    """

    async def _noop(_self, _name, _result):
        return None

    from mcp.client.session import ClientSession

    monkeypatch.setattr(ClientSession, "_validate_tool_result", _noop)


@pytest.mark.usefixtures("raw_results")
async def test_off_passes_violating_payload_untouched():
    # Also pins the SDK bypass: without it, the MCP SDK's wire layer would
    # hard-error ("Output validation error") on the advertised outputSchema.
    res = await _call(_cfg("off"), "m___bad", {"a": 1})
    assert not res.is_error
    assert res.structured_content == {"ok": "not-a-bool"}


@pytest.mark.usefixtures("raw_results")
async def test_off_passes_bad_arguments_through():
    # Pins that fastmcp does NOT validate arguments by default
    # (strict_input_validation=False): the bad args reach the target.
    res = await _call(_cfg("off"), "m___bad", {"a": "not-an-int"})
    assert not res.is_error
    assert res.structured_content == {"ok": "not-a-bool"}  # canned response came back


@pytest.mark.usefixtures("raw_results")
async def test_warn_passes_payload_and_flags(caplog):
    history = InvocationLog(10)
    with caplog.at_level(logging.WARNING, logger="lcgw"):
        res = await _call(_cfg("warn"), "m___bad", {"a": 1}, history=history)
    assert not res.is_error
    assert res.structured_content == {"ok": "not-a-bool"}  # still passes through
    assert any("contract violation" in r.getMessage() for r in caplog.records)
    records, _ = history.since(0)
    assert "'not-a-bool' is not of type 'boolean'" in records[0]["contract_violation"]
    # `lcgw tail` marks violating lines.
    from localcore_gateway.__main__ import _format_invocation

    assert "[contract:" in _format_invocation(records[0])


@pytest.mark.usefixtures("raw_results")
async def test_warn_flags_bad_arguments(caplog):
    history = InvocationLog(10)
    with caplog.at_level(logging.WARNING, logger="lcgw"):
        res = await _call(_cfg("warn"), "m___bad", {}, history=history)
    assert not res.is_error  # payload still passes through
    records, _ = history.since(0)
    assert records[0]["contract_violation"].startswith("arguments")


async def test_error_returns_contract_violation_envelope_for_output():
    history = InvocationLog(10)
    res = await _call(_cfg("error"), "m___bad", {"a": 1}, history=history)
    assert res.is_error
    text = "".join(getattr(b, "text", "") for b in res.content)
    assert "ContractViolation" in text
    assert "'not-a-bool' is not of type 'boolean'" in text
    records, _ = history.since(0)
    assert records[0]["is_error"]
    assert records[0]["contract_violation"] is not None


async def test_error_rejects_bad_arguments_before_dispatch():
    res = await _call(_cfg("error"), "m___bad", {"a": "not-an-int"})
    assert res.is_error
    text = "".join(getattr(b, "text", "") for b in res.content)
    assert "ContractViolation" in text
    assert "arguments" in text


async def test_valid_calls_unaffected_in_error_mode():
    res = await _call(_cfg("error"), "m___good", {})
    assert not res.is_error


def test_contract_checks_value_validated():
    with pytest.raises(ValueError, match="contract_checks"):
        GatewayConfig.model_validate({"server": {"contract_checks": "strict"}})
