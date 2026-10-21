"""The committed API documentation must match the code.

If this fails, run `uv run heatbeat-openapi` and commit docs/openapi.json.
"""

from pathlib import Path

from heatbeat.api.main import openapi_json


def test_docs_openapi_json_is_up_to_date():
    committed = Path("docs/openapi.json").read_text(encoding="utf-8")

    assert committed == openapi_json(), "docs/openapi.json is outdated: run `uv run heatbeat-openapi`"
