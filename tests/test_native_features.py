from __future__ import annotations

import json
import os
import sys

import pytest
from pydantic import ValidationError

from localcore_gateway.config import GatewayConfig, LambdaFunctionConfig, load_config
from localcore_gateway.lambda_emu.native import NativeLambdaInvoker


async def test_p0_multiple_code_roots(tmp_path):
    """Handler in root B can import a helper that lives in root A."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "shared.py").write_text("PREFIX = 'A:'\n")
    (b / "hb.py").write_text("import shared\ndef handler(e, c): return {'msg': shared.PREFIX + e['x']}\n")
    cfg = LambdaFunctionConfig(backend="native", handler="hb.handler")
    inv = NativeLambdaInvoker(cfg, code_roots=[str(a), str(b)])
    res = await inv.invoke({"x": "hi"}, client_context={})
    await inv.aclose()
    assert res.payload == {"msg": "A:hi"}


async def test_p1_gateway_syspath_never_mutated(tmp_path):
    """Subprocess isolation: the gateway process sys.path is untouched."""
    (tmp_path / "h.py").write_text("def handler(e, c): return 1\n")
    before = list(sys.path)
    cfg = LambdaFunctionConfig(backend="native", handler="h.handler")
    inv = NativeLambdaInvoker(cfg, code_roots=[str(tmp_path)])
    assert sys.path == before  # construction does not touch sys.path
    await inv.invoke({}, client_context={})
    assert sys.path == before  # nor does invoke (it runs in a child process)
    await inv.aclose()
    assert sys.path == before


async def test_monorepo_same_module_name_isolated(tmp_path):
    """Two targets whose handler is the SAME module name stay independent."""
    svc_a = tmp_path / "services" / "a"
    svc_b = tmp_path / "services" / "b"
    svc_a.mkdir(parents=True)
    svc_b.mkdir(parents=True)
    # Both define a top-level `handler` module (the Lambda default name).
    (svc_a / "handler.py").write_text("def handler(e, c): return {'svc': 'A'}\n")
    (svc_b / "handler.py").write_text("def handler(e, c): return {'svc': 'B'}\n")
    cfg = LambdaFunctionConfig(backend="native", handler="handler.handler")
    a = NativeLambdaInvoker(cfg, code_roots=[str(svc_a)])
    b = NativeLambdaInvoker(cfg, code_roots=[str(svc_b)])
    ra = await a.invoke({}, client_context={})
    rb = await b.invoke({}, client_context={})
    await a.aclose()
    await b.aclose()
    assert ra.payload == {"svc": "A"}
    assert rb.payload == {"svc": "B"}  # NOT shadowed by A's `handler`


async def test_p2_hot_reload_watches_whole_code_root(tmp_path):
    (tmp_path / "helper.py").write_text("VALUE = 1\n")
    (tmp_path / "hp.py").write_text("import helper\ndef handler(e, c): return {'v': helper.VALUE}\n")
    cfg = LambdaFunctionConfig(backend="native", handler="hp.handler")
    inv = NativeLambdaInvoker(cfg, code_roots=[str(tmp_path)])
    r1 = await inv.invoke({}, client_context={})
    assert r1.payload == {"v": 1}

    # Edit a *helper* (not the handler file); bump mtime deterministically.
    (tmp_path / "helper.py").write_text("VALUE = 2\n")
    future = (tmp_path / "hp.py").stat().st_mtime + 100
    os.utime(tmp_path / "helper.py", (future, future))

    r2 = await inv.invoke({}, client_context={})
    await inv.aclose()
    assert r2.payload == {"v": 2}  # deep cold-start picked up the helper edit


async def test_p3_env_file_with_inline_override(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("# comment\nexport FROM_FILE='file-val'\nSHARED=from_file\n")
    (tmp_path / "he.py").write_text(
        "import os\n"
        "def handler(e, c):\n"
        "    return {'f': os.environ.get('FROM_FILE'),\n"
        "            's': os.environ.get('SHARED')}\n"
    )
    cfg = LambdaFunctionConfig(backend="native", handler="he.handler", env={"SHARED": "inline_wins"})
    inv = NativeLambdaInvoker(cfg, code_roots=[str(tmp_path)], env_file=str(envf))
    res = await inv.invoke({}, client_context={})
    await inv.aclose()
    assert res.payload == {"f": "file-val", "s": "inline_wins"}
    assert "FROM_FILE" not in os.environ  # env restored after invoke


def test_p4_tool_schema_file_merge(tmp_path):
    (tmp_path / "h.py").write_text("def handler(e, c): return {}\n")
    (tmp_path / "schema.json").write_text(
        json.dumps(
            [
                {"name": "fromfile", "description": "f", "inputSchema": {"type": "object"}},
                {"name": "dup", "description": "file-version"},
            ]
        )
    )
    (tmp_path / "config.yaml").write_text(
        "targets:\n"
        "  - type: lambda\n"
        "    name: t\n"
        "    lambda: { backend: native, handler: h.handler }\n"
        "    tool_schema_file: schema.json\n"
        "    tools:\n"
        "      - { name: dup, description: inline-wins }\n"
        "      - { name: inlineonly }\n"
    )
    cfg = load_config(tmp_path / "config.yaml")
    tools = {t.name: t.description for t in cfg.effective_tools(cfg.targets[0])}
    assert set(tools) == {"fromfile", "dup", "inlineonly"}
    assert tools["dup"] == "inline-wins"  # inline overrides file on name clash


def test_p4_requires_tools_or_schema_file():
    with pytest.raises(ValidationError, match=r"tools.*tool_schema_file"):
        GatewayConfig.model_validate(
            {
                "targets": [
                    {
                        "type": "lambda",
                        "name": "t",
                        "lambda": {"backend": "native", "handler": "h.handler"},
                    }
                ]
            }
        )
