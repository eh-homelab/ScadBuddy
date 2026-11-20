"""#6725: the print's prime tower as the run and the analyzers read it, from the shared
vectors in `test-fixtures/prime-tower/tower-choice.json`. The print dialog's copy
(`frontend/src/lib/primeTower.ts` `analysisTower`) reads the same file, so the two cannot
drift. Each case's `process_overrides` is the request body as JSON gives it; the request
model stores it as text (`process_overrides.normalised_overrides`) before the run reads it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.api.accepted_fixes import analysis_request
from scadbuddy.bambuddy.prime_tower import tower_asked
from scadbuddy.bambuddy.print_run import PrintRunRequest

VECTORS = Path(__file__).resolve().parents[3] / "test-fixtures/prime-tower/tower-choice.json"

BODY: dict[str, Any] = {
    "printer_id": 1,
    "copies": 1,
    "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
    "choices": {"nozzles": [{"size": "0.4"}], "tier": "fine"},
}


def _vectors() -> list[dict[str, Any]]:
    vectors: list[dict[str, Any]] = json.loads(VECTORS.read_text(encoding="utf-8"))
    return vectors


def _request(vector: dict[str, Any]) -> PrintRunRequest:
    body = dict(BODY)
    if vector["choice"] is not None:
        body["prime_tower"] = vector["choice"]
    if "process_overrides" in vector:
        body["process_overrides"] = vector["process_overrides"]
    return PrintRunRequest.model_validate(body)


def test_the_shared_file_has_the_cases_it_must() -> None:
    values = [
        v["process_overrides"].get("enable_prime_tower")
        for v in _vectors()
        if "process_overrides" in v
    ]
    # JSON text tells False from 0 and 0 from 0.0, also inside a list.
    present = {json.dumps(value) for value in values}
    wanted: tuple[Any, ...] = (
        "0",
        "1",
        True,
        False,
        0,
        1,
        0.0,
        ["0"],
        [False],
        [0],
        ["1", "0"],
        [],
    )
    for value in wanted:
        assert json.dumps(value) in present, value


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: str(v["case"]))
def test_the_analysis_takes_the_runs_tower(vector: dict[str, Any]) -> None:
    tower = analysis_request(_request(vector)).prime_tower
    want = vector["want"]
    got = None if tower is None else tower.model_dump(exclude_none=True)
    assert got == want


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: str(v["case"]))
def test_the_run_asks_for_the_tower_the_vector_says(vector: dict[str, Any]) -> None:
    request = _request(vector)
    want = vector["want"]
    assert tower_asked(request.prime_tower, request.overrides()) == (
        None if want is None else want["enabled"]
    )
