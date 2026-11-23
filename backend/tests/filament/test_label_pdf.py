"""#7157: the labels PDF and the label templates (`filament/label_pdf.py`,
`filament/label_templates.py`; plan `docs/superpowers/plans/2026-11-23-label-templates-pdf.md`)."""

from __future__ import annotations

import base64
import re
import zlib

import pytest
from pydantic import ValidationError

from scadbuddy.filament import label_pdf
from scadbuddy.filament.label_pdf import PlaceLabel, SpoolLabel, render_pdf
from scadbuddy.filament.label_templates import BY_ID, LabelTemplateChoice

MM = 72 / 25.4

SPOOL = SpoolLabel(
    number=12, material="PLA Basic", colour="Jade White", brand="Bambu Lab", qr="scadbuddy:spool:x"
)
PLACE = PlaceLabel(code="L-7", name="H2C AMS 1 slot 2", qr="https://sb.example/x/L-7")


def _streams(pdf: bytes) -> str:
    """Every content stream's text: ReportLab writes them ASCII85, then Flate."""
    out = []
    for raw in re.findall(rb"stream\r?\n(.*?)~>", pdf, re.S):
        out.append(zlib.decompress(base64.a85decode(raw.strip())).decode("latin-1"))
    return "\n".join(out)


def _boxes(pdf: bytes) -> list[tuple[float, float]]:
    return [
        (float(w), float(h)) for w, h in re.findall(rb"/MediaBox \[ 0 0 ([\d.]+) ([\d.]+) \]", pdf)
    ]


def test_a_roll_label_is_one_page_of_its_own_size() -> None:
    pdf = render_pdf([SPOOL, PLACE], BY_ID["box_62x29"], "roll")
    assert pdf.startswith(b"%PDF-")
    boxes = _boxes(pdf)
    assert len(boxes) == 2
    assert boxes[0] == pytest.approx((62 * MM, 29 * MM), abs=0.01)


def test_the_spool_label_carries_the_browser_labels_lines() -> None:
    text = _streams(render_pdf([SPOOL], BY_ID["roll_50x25"], "roll"))
    for line in ("(#12)", "(PLA Basic)", "(Jade White)", "(Bambu Lab)"):
        assert line in text


def test_the_place_label_carries_its_code_and_name_in_two_lines() -> None:
    text = _streams(render_pdf([PLACE], BY_ID["roll_50x25"], "roll"))
    assert "(L-7)" in text
    # 23.5 mm of text column: the name breaks at a space, as the browser's `line-clamp-2`.
    assert "(H2C AMS 1)" in text
    assert "(slot 2)" in text
    # A wider label keeps it on one line.
    assert "(H2C AMS 1 slot 2)" in _streams(render_pdf([PLACE], BY_ID["ams_holder_75x55"], "roll"))


def test_each_label_draws_the_qr_code_of_its_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    drawn: list[str] = []
    real = label_pdf._draw_qr

    def spy(canvas: object, payload: str, x: float, y: float) -> None:
        drawn.append(payload)
        real(canvas, payload, x, y)  # type: ignore[arg-type]

    monkeypatch.setattr(label_pdf, "_draw_qr", spy)
    render_pdf([SPOOL, PLACE], BY_ID["roll_50x25"], "roll")
    assert drawn == ["scadbuddy:spool:x", "https://sb.example/x/L-7"]


def test_a_sheet_template_lays_its_grid_on_its_page() -> None:
    pdf = render_pdf([SPOOL] * 31, BY_ID["avery_5160"], "roll")
    boxes = _boxes(pdf)
    # 30 a Letter sheet: 31 labels are two pages, whatever the layout says.
    assert len(boxes) == 2
    assert boxes[0] == pytest.approx((215.9 * MM, 279.4 * MM), abs=0.01)


def test_the_packed_sheet_is_letter_paper() -> None:
    # 50 x 25 mm on 190 x 259.4 mm with 2 mm gaps: 3 columns, 9 rows, 27 a page.
    pdf = render_pdf([SPOOL] * 28, BY_ID["roll_50x25"], "sheet")
    boxes = _boxes(pdf)
    assert len(boxes) == 2
    assert boxes[0] == pytest.approx((215.9 * MM, 279.4 * MM), abs=0.01)


def test_a_long_name_is_cut_with_an_ellipsis() -> None:
    long = SpoolLabel(
        number=3, material="PLA " + "Silk " * 20, colour=None, brand=None, qr="scadbuddy:spool:y"
    )
    text = _streams(render_pdf([long], BY_ID["roll_50x25"], "roll"))
    assert "\\205" in text or "\x85" in text  # WinAnsi's ellipsis
    assert "Silk " * 20 not in text


def test_a_place_name_of_one_long_word_runs_onto_the_second_line() -> None:
    word = "Filamentdryboxshelfnumberseventeen"
    lines = label_pdf._wrap(word, label_pdf.BOLD, 3.2, 23.5, 2)
    assert len(lines) == 2
    # The first line is as much of the word as fits, uncut; the overflow goes on line 2,
    # which alone is cut.
    assert word.startswith(lines[0])
    assert lines[0] and not lines[0].endswith(label_pdf.ELLIPSIS)
    assert lines[1].endswith(label_pdf.ELLIPSIS)
    assert word[len(lines[0]) :].startswith(lines[1][:-1])
    text = _streams(
        render_pdf([PlaceLabel("L-1", word, "https://x/x/L-1")], BY_ID["roll_50x25"], "roll")
    )
    assert f"({lines[0]})" in text


def test_text_outside_latin_1_prints_as_a_question_mark() -> None:
    odd = SpoolLabel(number=4, material="PLA", colour="翡翠", brand=None, qr="scadbuddy:spool:z")
    assert "(??)" in _streams(render_pdf([odd], BY_ID["roll_50x25"], "roll"))


def test_a_custom_choice_needs_both_sizes_in_bounds() -> None:
    custom = LabelTemplateChoice(template="custom", width_mm=80, height_mm=40).resolved()
    assert (custom.width_mm, custom.height_mm, custom.sheet) == (80, 40, None)
    for bad in (
        {"template": "custom", "width_mm": 80},
        {"template": "custom", "width_mm": 39, "height_mm": 30},
        {"template": "custom", "width_mm": 80, "height_mm": 191},
        {"template": "box_40x30", "width_mm": 80, "height_mm": 40},
        {"template": "nope"},
    ):
        with pytest.raises(ValidationError):
            LabelTemplateChoice.model_validate(bad)


def test_no_choice_is_the_old_50_by_25_roll() -> None:
    template = LabelTemplateChoice().resolved()
    assert (template.id, template.width_mm, template.height_mm) == ("roll_50x25", 50, 25)
