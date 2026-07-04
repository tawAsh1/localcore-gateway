"""${VAR} environment expansion in load_config."""

from __future__ import annotations

import pytest

from localcore_gateway.config import load_config

MCP_TARGET = "targets:\n  - type: mcp\n    name: mytools\n    url: http://127.0.0.1:9000/mcp\n"


def test_expands_env_var_in_auth_value(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN", "s3cret")
    (tmp_path / "config.yaml").write_text(MCP_TARGET + '    auth: { type: bearer, value: "${TOKEN}" }\n')
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.targets[0].auth.value == "s3cret"


def test_expands_in_nested_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HDR", "abc")
    monkeypatch.setenv("KEY", "k123")
    (tmp_path / "config.yaml").write_text(
        "targets:\n"
        "  - type: mcp\n"
        "    name: m\n"
        "    url: http://127.0.0.1:9000/mcp\n"
        '    headers: { X-Extra: "v-${HDR}" }\n'
        "  - type: openapi\n"
        "    name: api\n"
        "    spec: { openapi: 3.0.0, info: { title: t, version: '1' }, paths: {} }\n"
        "    base_url: http://api.example\n"
        '    auth: { type: apikey, value: "${KEY}" }\n'
    )
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.targets[0].headers == {"X-Extra": "v-abc"}
    assert cfg.targets[1].auth.value == "k123"


def test_unset_env_var_is_a_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv("NO_SUCH_TOKEN", raising=False)
    (tmp_path / "config.yaml").write_text(MCP_TARGET + '    auth: { type: bearer, value: "${NO_SUCH_TOKEN}" }\n')
    with pytest.raises(ValueError, match=r"\$\{NO_SUCH_TOKEN\}.*not set"):
        load_config(tmp_path / "config.yaml")


def test_dollar_dollar_escapes_to_literal(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN", "s3cret")
    (tmp_path / "config.yaml").write_text(MCP_TARGET + '    auth: { type: bearer, value: "$${TOKEN}" }\n')
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.targets[0].auth.value == "${TOKEN}"


def test_bare_dollar_and_non_ref_dollars_untouched(tmp_path):
    (tmp_path / "config.yaml").write_text(MCP_TARGET + '    auth: { type: bearer, value: "$HOME costs $5 ${}" }\n')
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.targets[0].auth.value == "$HOME costs $5 ${}"


def test_referenced_files_are_not_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("URLVAR", "http://expanded.example")
    (tmp_path / "openapi.yaml").write_text(
        "openapi: 3.0.0\ninfo: { title: t, version: '1' }\nservers: [{ url: \"${URLVAR}\" }]\npaths: {}\n"
    )
    (tmp_path / "config.yaml").write_text("targets:\n  - type: openapi\n    name: api\n    spec_file: openapi.yaml\n")
    cfg = load_config(tmp_path / "config.yaml")
    # Expansion is a load_config concern; spec_file content passes through verbatim.
    assert cfg.openapi_base_url(cfg.targets[0]) == "${URLVAR}"
