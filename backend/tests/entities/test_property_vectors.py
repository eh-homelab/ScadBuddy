"""The property value rules that Go's ``scadbuddy-core`` ports (#6949 K1, R-K1-33): Go
checks an item's properties in its own transaction, so it cannot call Python. Both
languages run the vectors of
``tools/go/core/internal/inventory/store/testdata/property_values.json`` (Go:
``TestPropertyValuesMatchPython``), so a port that drifts fails one of them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema.exceptions import best_match

from scadbuddy.entities import properties
from scadbuddy.entities.properties import _shape_problem, _validator, completeness, has_value

VECTORS = (
    Path(properties.__file__).resolve().parents[3]
    / "tools/go/core/internal/inventory/store/testdata/property_values.json"
)


def _fails(schema: dict[str, Any], value: Any) -> str | None:
    error = best_match(_validator(json.dumps(schema, sort_keys=True)).iter_errors(value))
    return None if error is None else str(error.validator)


def test_property_values_match_the_go_vectors() -> None:
    vectors = json.loads(VECTORS.read_text())
    for row in vectors["shape"]:
        assert _shape_problem(row["value"]) == row["problem"], row
    for row in vectors["has_value"]:
        assert has_value(row["value"]) == row["has"], row
    for row in vectors["fit"]:
        assert _fails(row["schema"], row["value"]) == row["fails"], row
    for row in vectors["completeness"]:
        found = completeness(row["schema"], row["values"])
        assert sorted(found.missing) == row["missing"], row
        assert [key for key, _ in found.invalid] == row["invalid"], row
