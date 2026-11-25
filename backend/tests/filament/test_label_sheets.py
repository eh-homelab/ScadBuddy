"""#7216: saved label sheet grids (`filament/label_sheets.py`, R-7216-11 and R-7216-12; plan
`docs/superpowers/plans/2026-11-25-label-outputs.md`)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from scadbuddy.filament.label_sheets import LabelSheetIn, SheetLayout, name_key
from scadbuddy.filament.label_templates import LabelTemplateChoice

AVERY_5163 = dict(
    page="letter",
    width_mm=101.6,
    height_mm=50.8,
    cols=2,
    rows=5,
    top_mm=12.7,
    left_mm=4.0,
    col_gap_mm=4.8,
)


def test_a_grid_that_fits_its_page_is_a_layout() -> None:
    assert SheetLayout(**AVERY_5163).cols == 2  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change, words",
    [
        ({"cols": 3}, "wide"),
        ({"rows": 6}, "tall"),
        ({"height_mm": 24}, None),
        ({"cols": 21}, None),
        ({"rows": 41}, None),
        ({"top_mm": -1}, None),
        ({"page": "a5"}, None),
    ],
)
def test_a_bad_grid_is_refused(change: dict[str, object], words: str | None) -> None:
    with pytest.raises(ValidationError, match=words):
        SheetLayout(**{**AVERY_5163, **change})  # type: ignore[arg-type]


def test_names_match_without_case_or_composition() -> None:
    assert name_key("Café Sheet") == name_key("CAFÉ sheet")
    # "e" and a combining acute accent is the same name as the composed "é".
    assert name_key("Café") == name_key("café")


def test_a_name_is_trimmed_and_composed() -> None:
    sheet = LabelSheetIn(name="  Café  ", layout=SheetLayout(**AVERY_5163))  # type: ignore[arg-type]
    assert sheet.name == "Café"
    with pytest.raises(ValidationError):
        LabelSheetIn(name="   ", layout=SheetLayout(**AVERY_5163))  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LabelSheetIn(name="x" * 101, layout=SheetLayout(**AVERY_5163))  # type: ignore[arg-type]


def test_an_old_template_choice_still_reads() -> None:
    choice = LabelTemplateChoice.model_validate(
        {"template": "custom", "width_mm": 60, "height_mm": 30}
    )
    assert choice.sheet_id is None


def test_a_saved_choice_needs_its_sheet_id_and_only_it_has_one() -> None:
    with pytest.raises(ValidationError):
        LabelTemplateChoice(template="saved")
    with pytest.raises(ValidationError):
        LabelTemplateChoice(template="roll_50x25", sheet_id=uuid4())


def test_a_saved_choice_does_not_resolve_without_its_row() -> None:
    with pytest.raises(ValueError):
        LabelTemplateChoice(template="saved", sheet_id=uuid4()).resolved()
