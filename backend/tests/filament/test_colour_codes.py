"""Bambu Studio's colour table, read by code (#6267)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from scadbuddy.filament.colour_codes import ColourCodes, ColourTableMissingError

FIXTURE = Path(__file__).parents[1] / "fixtures" / "bambu_filaments_color_codes.json"


def test_a_code_gives_its_rgba_and_name() -> None:
    found = ColourCodes(FIXTURE).lookup("10101")
    assert found is not None
    assert found.name == "Black"
    assert found.rgba == ["#000000FF"]
    assert found.fila_type == "PLA Basic"
    assert found.fila_id == "GFA00"


def test_a_translucent_keeps_alpha_80() -> None:
    found = ColourCodes(FIXTURE).lookup("13100")
    assert found is not None
    assert found.rgba == ["#F4EE2A80"]


def test_a_multi_colour_code_keeps_every_colour_in_order() -> None:
    found = ColourCodes(FIXTURE).lookup("14500")
    assert found is not None
    assert found.rgba == ["#FF0000FF", "#0000FFFF"]


def test_an_unknown_code_is_none() -> None:
    assert ColourCodes(FIXTURE).lookup("GFA17") is None


def test_a_malformed_entry_is_skipped_and_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rows["data"].append(
        {
            "fila_color_code": "99999",
            "fila_color": ["#ZZZZZZ"],
            "fila_color_name": {"en": "Broken"},
            "fila_type": "PLA Basic",
            "fila_id": "GFA00",
        }
    )
    rows["data"].append({"fila_color_code": "99998"})
    table = tmp_path / "table.json"
    table.write_text(json.dumps(rows), encoding="utf-8")
    codes = ColourCodes(table)
    with caplog.at_level(logging.WARNING):
        assert codes.lookup("10101") is not None
        assert codes.lookup("99999") is None
        assert codes.lookup("99998") is None
    skipped = [r for r in caplog.records if r.getMessage() == "filament.colour_code.skipped"]
    assert sorted(r.__dict__["code"] for r in skipped) == ["99998", "99999"]


def test_a_missing_table_names_its_path(tmp_path: Path) -> None:
    table = tmp_path / "nope.json"
    with pytest.raises(ColourTableMissingError, match=r"nope\.json"):
        ColourCodes(table).lookup("10101")
