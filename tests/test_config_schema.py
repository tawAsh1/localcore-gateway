from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from localcore_gateway.__main__ import main
from localcore_gateway.config import GatewayConfig

EXAMPLES = sorted((Path(__file__).resolve().parents[1] / "examples").glob("*.yaml"))


def _schema() -> dict:
    return GatewayConfig.model_json_schema(by_alias=True)


def test_schema_is_valid_jsonschema_and_hides_source_dir(capsys):
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    # A validator can be built from it (raises on an invalid schema)...
    jsonschema.validators.validator_for(schema).check_schema(schema)
    # ...it matches the YAML surface (aliases, not python field names)...
    lambda_target = schema["$defs"]["LambdaTargetConfig"]["properties"]
    assert "lambda" in lambda_target
    assert "lambda_" not in lambda_target
    assert "inputSchema" in schema["$defs"]["ToolSpec"]["properties"]
    # ...and the loader-internal field is kept out of it.
    assert "source_dir" not in json.dumps(schema)


def test_schema_output_writes_file(tmp_path):
    out = tmp_path / "gateway.schema.json"
    assert main(["schema", "--output", str(out)]) == 0
    assert json.loads(out.read_text())["$defs"]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_configs_validate_against_schema(path):
    """Doubles as a guard that the shipped examples stay schema-valid."""
    # Raw YAML, no ${VAR} expansion: expansion placeholders are plain strings
    # and must already satisfy the schema.
    data = yaml.safe_load(path.read_text())
    jsonschema.validate(instance=data, schema=_schema())
