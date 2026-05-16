from __future__ import annotations

import json

from localcore_gateway.__main__ import main


def test_tools_surfaces_output_schema_only_when_declared(tmp_path, capsys):
    (tmp_path / "h.py").write_text("def handler(e, c): return {}\n")
    (tmp_path / "config.yaml").write_text(
        "targets:\n"
        "  - type: lambda\n"
        "    name: t\n"
        "    lambda: { backend: native, handler: h.handler }\n"
        "    tools:\n"
        "      - name: withschema\n"
        "        outputSchema:"
        " { type: object, properties: { ok: { type: boolean } } }\n"
        "      - name: noschema\n"
    )
    rc = main(["tools", "-c", str(tmp_path / "config.yaml")])
    assert rc == 0
    by = {e["name"]: e for e in json.loads(capsys.readouterr().out)}
    assert by["t___withschema"]["outputSchema"] == {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
    }
    assert "outputSchema" not in by["t___noschema"]  # omitted when absent
