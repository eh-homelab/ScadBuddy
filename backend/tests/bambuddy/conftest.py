from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig

RECORDINGS = Path(__file__).parent / "recordings"
BASE_URL = "https://bambuddy.test"


def recording(name: str) -> Any:
    """A response body recorded from the live Bambuddy; see recordings/README.md."""
    return json.loads((RECORDINGS / name).read_text(encoding="utf-8"))


@pytest.fixture
def config() -> BambuddyConfig:
    # No poll delay: await_slice is driven through respx, not a real slicer.
    return BambuddyConfig(
        base_url=BASE_URL, api_key="s3cret", slice_timeout=1.0, slice_poll_interval=0.0
    )


@pytest.fixture
async def bambuddy(config: BambuddyConfig) -> Any:
    async with BambuddyClient(config) as client:
        yield client


#: The pruned OpenAPI recordings, searched in order for a schema.
RECORDED_SPECS = ("scadbuddy-routes.json", "rfid-routes.json", "sync-routes.json")


def recorded_schema(name: str) -> dict[str, Any]:
    for recorded in RECORDED_SPECS:
        spec = json.loads((RECORDINGS / "openapi" / recorded).read_text(encoding="utf-8"))
        schemas: dict[str, Any] = spec["components"]["schemas"]
        if name in schemas:
            found: dict[str, Any] = schemas[name]
            return found
    raise KeyError(name)


def shaped(schema: str, **values: Any) -> dict[str, Any]:
    """A response body built inline for a call the live instance was never asked to make
    (a POST; see recordings/README.md). Only the field names are checked against
    Bambuddy's recorded schema, so a misspelt field fails here rather than in
    production; required fields and JSON types are not checked."""
    unknown = set(values) - set(recorded_schema(schema)["properties"])
    assert not unknown, f"{schema} has no field {sorted(unknown)}"
    return values


def assert_recorded_body(recorded: str, schema: str, body: Any, *, many: bool = False) -> None:
    """``body`` (a list of them with ``many``) is valid against ``schema`` as the recording
    ``recordings/openapi/<recorded>`` holds it: types, required fields and nested refs."""
    spec = json.loads((RECORDINGS / "openapi" / recorded).read_text(encoding="utf-8"))
    ref = {"$ref": f"#/components/schemas/{schema}"}
    root = {"type": "array", "items": ref} if many else ref
    jsonschema.validate(body, {**root, "components": spec["components"]})
