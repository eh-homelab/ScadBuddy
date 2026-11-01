"""The shared list of a turn's statuses (``agent/test/fixtures/turn-statuses.json``): the
same file pins the agent's durable sweep (``durableSweep.ts`` ``TURN_STATUSES``), so the
gate store's parked statuses and the agent's cannot drift."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scadbuddy_durable.gate.store import _WAITING

DOC: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[2] / "agent" / "test" / "fixtures" / "turn-statuses.json"
    ).read_text()
)


def test_the_parked_statuses_are_the_sweeps() -> None:
    assert list(_WAITING) == DOC["waiting"]
    assert set(_WAITING) < set(DOC["turn"])
