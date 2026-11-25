"""#7157: the labels PDF and the label templates (`filament/label_pdf.py`,
`filament/label_templates.py`; plan `docs/superpowers/plans/2026-11-23-label-templates-pdf.md`)."""

from __future__ import annotations

import re

import pypdfium2
import pytest
from pydantic import ValidationError

from scadbuddy.filament import label_pdf, label_png
from scadbuddy.filament.label_fonts import width_mm
from scadbuddy.filament.label_pdf import PlaceLabel, SpoolLabel, render_pdf
from scadbuddy.filament.label_templates import BY_ID, LabelTemplateChoice

MM = 72 / 25.4

SPOOL = SpoolLabel(
    number=12, material="PLA Basic", colour="Jade White", brand="Bambu Lab", qr="scadbuddy:spool:x"
)
PLACE = PlaceLabel(code="L-7", name="H2C AMS 1 slot 2", qr="https://sb.example/x/L-7")


def _text(pdf: bytes) -> str:
    """Every page's text as PDFium reads it, the pages joined by newlines."""
    return "\n".join(text for text, _ in _pages(pdf))


def _lines(pdf: bytes) -> list[str]:
    """Each line of text that PDFium reads, in page order."""
    return [line.strip() for line in _text(pdf).splitlines() if line.strip()]


def _sizes(pdf: bytes) -> list[float]:
    """The font size, in points, of each character PDFium reads."""
    return [size for _, sizes in _pages(pdf) for size in sizes]


def _pages(pdf: bytes) -> list[tuple[str, list[float]]]:
    """Each page's text and its characters' font sizes. PDFium is not thread-safe, so the
    read holds the labels' PDFium lock (`label_png._PDFIUM_LOCK`)."""
    out = []
    with label_png._PDFIUM_LOCK:
        document = pypdfium2.PdfDocument(pdf)
        try:
            for at in range(len(document)):
                page = document[at]
                textpage = page.get_textpage()
                sizes = [
                    pypdfium2.raw.FPDFText_GetFontSize(textpage, index)
                    for index in range(textpage.count_chars())
                ]
                out.append((textpage.get_text_range(), sizes))
                textpage.close()
                page.close()
        finally:
            document.close()
    return out


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
    lines = _lines(render_pdf([SPOOL], BY_ID["roll_50x25"], "roll"))
    assert lines == ["#12", "PLA Basic", "Jade White", "Bambu Lab"]


def test_the_place_label_carries_its_code_and_name_in_two_lines() -> None:
    # 23.5 mm of text column: the name breaks at a space, as the browser's `line-clamp-2`.
    assert _lines(render_pdf([PLACE], BY_ID["roll_50x25"], "roll")) == [
        "L-7",
        "H2C AMS 1",
        "slot 2",
    ]
    # A wider label keeps it on one line.
    assert _lines(render_pdf([PLACE], BY_ID["ams_holder_75x55"], "roll")) == [
        "L-7",
        "H2C AMS 1 slot 2",
    ]


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
    text = _text(render_pdf([long], BY_ID["roll_50x25"], "roll"))
    assert label_pdf.ELLIPSIS in text
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
    drawn = _lines(
        render_pdf([PlaceLabel("L-1", word, "https://x/x/L-1")], BY_ID["roll_50x25"], "roll")
    )
    assert drawn[1] == lines[0]


def test_a_column_narrower_than_one_character_leaves_no_empty_line() -> None:
    assert label_pdf._wrap("W", label_pdf.BOLD, 3.2, 0.5, 2) == ["W"]
    lines = label_pdf._wrap("Wx", label_pdf.BOLD, 3.2, 0.5, 2)
    assert lines[0] == "W" and "" not in lines


@pytest.mark.parametrize("text", ["L-1234567", "#123456", "WWWW"])
def test_the_big_text_is_measured_to_fill_the_narrowest_column(text: str) -> None:
    # The narrowest custom label, 40 mm wide, leaves 13.5 mm of text column.
    column = 40 - 2 * label_pdf.QUIET_MM - label_pdf.QR_MM - label_pdf.PAD_MM
    size = label_pdf._big_size(text, 8, column)
    drawn = label_pdf._width(text, label_pdf.BOLD, size)
    # Inside the column, and as large as fits: no guessed width per character.
    assert drawn <= column + 1e-6
    assert size == 8 or drawn == pytest.approx(column, rel=1e-3)


def test_the_pdf_draws_the_big_text_at_the_size_its_label_column_allows() -> None:
    # A 40 mm label: _draw_label hands _big_size the narrow 13.5 mm column, not the 50 mm one.
    template = LabelTemplateChoice(template="custom", width_mm=40, height_mm=30).resolved()
    long = SpoolLabel(
        number=123456, material="PLA", colour="Red", brand=None, qr="scadbuddy:spool:y"
    )
    column = 40 - 2 * label_pdf.QUIET_MM - label_pdf.QR_MM - label_pdf.PAD_MM
    want = label_pdf._big_size("#123456", 9, column)
    assert want < 9
    sizes = _sizes(render_pdf([long], template, "roll"))
    # The number is the label's first line, so its first character's size is the number's.
    assert sizes[0] == pytest.approx(want * MM, abs=0.01)
    # Not the size the 50 mm label's column would give.
    wide = label_pdf._big_size(
        "#123456", 9, 50 - 2 * label_pdf.QUIET_MM - label_pdf.QR_MM - label_pdf.PAD_MM
    )
    assert all(size != pytest.approx(wide * MM, abs=0.01) for size in sizes)


def test_a_name_outside_latin_1_prints_as_itself() -> None:
    odd = SpoolLabel(number=4, material="PLA", colour="Пётр", brand="Ζεύς", qr="scadbuddy:spool:z")
    text = _text(render_pdf([odd], BY_ID["roll_50x25"], "roll"))
    assert "Пётр" in text
    assert "Ζεύς" in text
    assert "?" not in text


def test_the_fonts_are_embedded() -> None:
    pdf = render_pdf([SPOOL, PLACE], BY_ID["roll_50x25"], "roll")
    assert b"/FontFile2" in pdf
    assert b"/BaseFont /Helvetica" not in pdf


def test_a_mixed_script_name_is_cut_by_its_measured_runs() -> None:
    odd = SpoolLabel(
        number=5, material=None, colour="Пётр 3D ✓ 😀 " * 6, brand=None, qr="scadbuddy:spool:m"
    )
    lines = _lines(render_pdf([odd], BY_ID["roll_50x25"], "roll"))
    column = 50 - 2 * label_pdf.QUIET_MM - label_pdf.QR_MM - label_pdf.PAD_MM
    line = lines[1]
    assert line.endswith(label_pdf.ELLIPSIS)
    assert line.startswith("Пётр 3D ✓")
    assert width_mm(line, "regular", 3.0) <= column


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
