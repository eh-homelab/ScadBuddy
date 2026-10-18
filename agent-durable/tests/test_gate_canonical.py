"""The shared response-hash vectors (#3724): the agent's ``src/gate/canonical.ts`` wrote
them, and this port must compute every canonical form and hash again."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scadbuddy_durable.gate.canonical import canonical_json, response_hash

DOC: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "agent"
        / "test"
        / "fixtures"
        / "response-hash-vectors.json"
    ).read_text()
)


@pytest.mark.parametrize("vector", DOC["vectors"], ids=lambda v: v["name"])
def test_vector(vector: dict[str, Any]) -> None:
    assert canonical_json(vector["value"]) == vector["canonical"]
    assert response_hash(vector["value"]) == vector["sha256"]


@pytest.mark.parametrize("number", [1, -0.0, 1e21, 1.5])
def test_a_number_is_refused(number: float) -> None:
    # canonical.ts refuses one too: the two languages print some numbers differently.
    with pytest.raises(TypeError):
        canonical_json({"kind": "answer", "n": number})


def test_true_and_false_are_not_numbers() -> None:
    assert canonical_json([True, False]) == "[true,false]"
