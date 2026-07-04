from __future__ import annotations

import yaml

from localcore_gateway.__main__ import main
from localcore_gateway.config import GatewayConfig
from localcore_gateway.preflight import preflight

_TOOL = {"name": "add", "description": "adds", "inputSchema": {"type": "object"}}


def _cfg(**overrides) -> GatewayConfig:
    raw = {
        "server": {"name": "my-gateway"},
        "targets": [
            {
                "type": "lambda",
                "name": "demo",
                "lambda": {"backend": "native", "handler": "h.handler"},
                "tools": [_TOOL],
            }
        ],
    }
    raw.update(overrides)
    return GatewayConfig.model_validate(raw)


def _messages(cfg, severity):
    return [f.message for f in preflight(cfg) if f.severity == severity]


def test_clean_config_has_no_findings():
    assert preflight(_cfg()) == []


# --- ERROR: hard API validation ---


def test_bad_gateway_name_is_error():
    for name in ("has_underscore", "double--hyphen", "-leading", "x" * 49):
        cfg = _cfg(server={"name": name})
        assert any("CreateGateway" in m for m in _messages(cfg, "ERROR")), name


def test_underscore_target_name_is_error():
    # Passes local config validation today; AWS would reject it at deploy.
    cfg = _cfg(
        targets=[
            {
                "type": "lambda",
                "name": "my_target",
                "lambda": {"backend": "native", "handler": "h.handler"},
                "tools": [_TOOL],
            }
        ]
    )
    msgs = _messages(cfg, "ERROR")
    assert any("CreateGatewayTarget" in m and "underscores" in m for m in msgs)


def test_empty_tool_description_is_error():
    cfg = _cfg(
        targets=[
            {
                "type": "lambda",
                "name": "demo",
                "lambda": {"backend": "native", "handler": "h.handler"},
                "tools": [{"name": "nodesc"}],
            }
        ]
    )
    assert any("description is required" in m for m in _messages(cfg, "ERROR"))


# --- WARN: default service quotas ---


def test_too_many_targets_is_warn():
    targets = [
        {
            "type": "lambda",
            "name": f"t{i}",
            "lambda": {"backend": "native", "handler": "h.handler"},
            "tools": [_TOOL],
        }
        for i in range(101)
    ]
    assert any("targets per gateway" in m for m in _messages(_cfg(targets=targets), "WARN"))


def test_too_many_tools_is_warn():
    tools = [{**_TOOL, "name": f"tool{i}"} for i in range(1001)]
    cfg = _cfg(
        targets=[
            {"type": "lambda", "name": "demo", "lambda": {"backend": "native", "handler": "h.handler"}, "tools": tools}
        ]
    )
    assert any("tools per target" in m for m in _messages(cfg, "WARN"))


def test_long_tool_name_is_warn():
    tools = [{**_TOOL, "name": "x" * 257}]
    cfg = _cfg(
        targets=[
            {"type": "lambda", "name": "demo", "lambda": {"backend": "native", "handler": "h.handler"}, "tools": tools}
        ]
    )
    assert any("tool name limit" in m for m in _messages(cfg, "WARN"))


def test_oversized_inline_schema_is_warn():
    big = {**_TOOL, "inputSchema": {"type": "object", "description": "x" * 1_000_001}}
    cfg = _cfg(
        targets=[
            {"type": "lambda", "name": "demo", "lambda": {"backend": "native", "handler": "h.handler"}, "tools": [big]}
        ]
    )
    assert any("inline schema size" in m for m in _messages(cfg, "WARN"))


def test_excessive_timeout_is_warn():
    cfg = _cfg(
        targets=[
            {
                "type": "lambda",
                "name": "demo",
                "lambda": {"backend": "native", "handler": "h.handler", "timeout_sec": 901},
                "tools": [_TOOL],
            }
        ]
    )
    assert any("invocation timeout" in m for m in _messages(cfg, "WARN"))
    cfg = _cfg(targets=[{"type": "mcp", "name": "up", "url": "http://h/mcp", "timeout_sec": 1200}])
    assert any("invocation timeout" in m for m in _messages(cfg, "WARN"))


# --- NOTICE: local-only constructs ---


def test_local_only_targets_are_notices():
    cfg = _cfg(
        targets=[
            {"type": "mock", "name": "m", "tools": [{**_TOOL, "response": 1}]},
            {"type": "aws-gateway", "name": "prod", "url": "http://h/mcp"},
            {"type": "mcp", "name": "loc", "command": "python3"},
        ]
    )
    notices = _messages(cfg, "NOTICE")
    assert len(notices) == 3
    assert all("local-only, no AWS analog" in m for m in notices)


def test_url_mode_mcp_target_is_not_a_notice():
    cfg = _cfg(targets=[{"type": "mcp", "name": "up", "url": "http://h/mcp"}])
    assert _messages(cfg, "NOTICE") == []


def test_preflight_constructs_no_targets():
    # An unreachable MCP upstream must not matter: preflight is config-only
    # (target construction would fire eager discovery against the URL).
    cfg = _cfg(targets=[{"type": "mcp", "name": "up", "url": "http://127.0.0.1:9/mcp"}])
    assert preflight(cfg) == []


# --- lcgw preflight CLI ---


def _write(tmp_path, raw) -> str:
    p = tmp_path / "gw.yaml"
    p.write_text(yaml.safe_dump(raw))
    return str(p)


def test_cli_clean_config_exits_zero(tmp_path, capsys):
    path = _write(
        tmp_path,
        {
            "targets": [
                {
                    "type": "lambda",
                    "name": "demo",
                    "lambda": {"backend": "native", "handler": "h.handler"},
                    "tools": [_TOOL],
                }
            ]
        },
    )
    assert main(["preflight", "-c", path]) == 0
    assert "no findings" in capsys.readouterr().out


def test_cli_error_exits_one(tmp_path, capsys):
    path = _write(
        tmp_path,
        {
            "targets": [
                {
                    "type": "lambda",
                    "name": "bad_name",
                    "lambda": {"backend": "native", "handler": "h.handler"},
                    "tools": [_TOOL],
                }
            ]
        },
    )
    assert main(["preflight", "-c", path]) == 1
    out = capsys.readouterr().out
    assert "ERROR  targets[bad_name]:" in out
    assert "1 error(s), 0 warning(s), 0 notice(s)" in out


def test_cli_warn_exit_depends_on_strict(tmp_path, capsys):
    path = _write(
        tmp_path,
        {
            "targets": [
                {
                    "type": "lambda",
                    "name": "demo",
                    "lambda": {"backend": "native", "handler": "h.handler", "timeout_sec": 901},
                    "tools": [_TOOL],
                }
            ]
        },
    )
    assert main(["preflight", "-c", path]) == 0
    capsys.readouterr()
    assert main(["preflight", "-c", path, "--strict"]) == 1
    assert "WARN" in capsys.readouterr().out
