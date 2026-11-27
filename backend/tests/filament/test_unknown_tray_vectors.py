"""#7143 Task 3: the vectors that hold the Go port equal to Python (G13).

Python writes ``tests/fixtures/unknown-tray-vectors.json``
(``UPDATE_UNKNOWN_TRAY_VECTORS=1``); the Go tests read it. A stale file fails this test.
The resolve write stays in Python (G5), so the file has no list of links.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from scadbuddy.bambuddy.filaments import colour_distance
from scadbuddy.bambuddy.trays import place_label, tray_label

VECTORS = Path(__file__).parent.parent / "fixtures" / "unknown-tray-vectors.json"
PRINTER_NAME = "H2C"

COLOUR_PAIRS: list[tuple[str | None, str | None]] = [
    (None, None),
    (None, "#FF0000"),
    ("#FF0000", None),
    ("", "#FF0000"),
    ("#FF0000", "#FF0000"),
    ("#FF0000FF", "#FF000000"),
    ("#ff0000", "#FF0000FF"),
    ("FF0000", "#FF0000"),
    ("#FF0000", "#F00000"),
    ("#FF0000", "#E81010"),
    ("#FF0000", "#D32F2F"),
    ("#FF0000", "#00FF00"),
    ("#FF0000", "#0000FF"),
    ("#000000", "#FFFFFF"),
    ("#FFFFFF", "#000000"),
    ("#000000", "#000000"),
    ("#FFFFFF", "#FFFFFFFF"),
    ("#A7AEB2", "#A9AFB4"),
    ("#A7AEB2FF", "#F772A4FF"),
    ("#808080", "#7F7F7F"),
    ("#123456", "#123457"),
    ("not a colour", "#FF0000"),
    ("#FFF", "#FFFFFF"),
    ("#FFFF00", "#FFD700"),
]

TRAYS: list[tuple[int, int]] = [
    *[(ams, tray) for ams in range(4) for tray in range(4)],
    (128, 0),
    (129, 0),
    (255, 0),
]


def _vectors() -> dict[str, Any]:
    return {
        "colour_distance": [
            {"left": left, "right": right, "distance": colour_distance(left, right)}
            for left, right in COLOUR_PAIRS
        ],
        "labels": [
            {
                "ams_id": ams_id,
                "tray_id": tray_id,
                "tray_label": tray_label(ams_id, tray_id),
                "place_label_for_printer_H2C": place_label(PRINTER_NAME, ams_id, tray_id),
            }
            for ams_id, tray_id in TRAYS
        ],
    }


def test_the_vectors_match_what_python_does() -> None:
    expected = _vectors()
    if os.environ.get("UPDATE_UNKNOWN_TRAY_VECTORS"):
        VECTORS.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")
    assert json.loads(VECTORS.read_text()) == expected


def test_the_vectors_cover_the_required_cases() -> None:
    pairs = _vectors()["colour_distance"]
    assert len(pairs) >= 20
    assert any(p["left"] is None or p["right"] is None for p in pairs)
    assert any(p["distance"] == 0 for p in pairs)
    assert any(p["distance"] is None for p in pairs)
