"""#7216 R-7216-19: `label_pdf.py`'s fit constants and `fit_lines` against the shared vectors,
`test-fixtures/filament/label-fit-vectors.json`, which the browser label's copy
(`frontend/src/components/filament/labelSheet.ts`, `labelFitVectors.test.ts`) is tested
against too, so the PDF and the browser keep the same lines (R-7216-10)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.filament import label_pdf
from scadbuddy.filament.label_output import LabelField
from scadbuddy.filament.label_pdf import SpoolLabel

VECTORS = Path(__file__).resolve().parents[3] / "test-fixtures/filament/label-fit-vectors.json"
_DATA: dict[str, Any] = json.loads(VECTORS.read_text())

#: One text a field prints, each different, so a kept line names its field.
TEXTS: dict[str, str] = {
    "material": "PLA Basic",
    "colour": "Jade White",
    "hex": "#00AE42",
    "brand": "Bambu Lab",
    "location": "Shelf A",
    "temps": "190\N{EN DASH}230 °C",
    "weight": "1000 g",
    "note": "Dry first",
    "added": "2026-11-20",
}


def test_the_constants_are_the_shared_ones() -> None:
    constants = _DATA["constants"]
    for name in (
        "PAD_MM",
        "QR_MM",
        "QUIET_MM",
        "NUMBER_MM",
        "CODE_MM",
        "PLACE_NAME_MM",
        "LEADING",
        "BIG_LEADING",
        "GAP_MM",
    ):
        assert getattr(label_pdf, name) == constants[name], name
    assert dict(label_pdf.FIELD_MM) == constants["FIELD_MM"]
    assert sorted(label_pdf.BOLD_FIELDS) == sorted(constants["BOLD_FIELDS"])


@pytest.mark.parametrize("case", _DATA["cases"], ids=lambda case: case["name"])
def test_the_fit_keeps_the_shared_lines(case: dict[str, Any]) -> None:
    values: dict[str, str | None] = {"material": None, "colour": None, "brand": None}
    values |= {field: TEXTS[field] for field in case["values"]}
    label = SpoolLabel(number=12, qr="scadbuddy:spool:x", **values)
    fields: list[LabelField] = case["fields"]
    # A wide column, so no text is cut: the fit counts line heights only.
    lines = label_pdf.fit_lines(
        label_pdf._spool_lines(label, 100.0, fields), case["height_mm"] - 2 * label_pdf.PAD_MM
    )
    by_text = {text: field for field, text in TEXTS.items()} | {"#12": "number"}
    assert [by_text[line.text] for line in lines] == case["kept"]
