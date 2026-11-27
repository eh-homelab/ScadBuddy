"""#7157: the labels PDF and the label templates (`filament/label_pdf.py`,
`filament/label_templates.py`; plan `docs/superpowers/plans/2026-11-23-label-templates-pdf.md`)."""

from __future__ import annotations

import re

import pypdfium2
import pytest
from pydantic import ValidationError
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth

from scadbuddy.filament import label_pdf, label_png
from scadbuddy.filament.label_fonts import width_mm
from scadbuddy.filament.label_output import ALL_FIELDS, LabelOutput
from scadbuddy.filament.label_pdf import CodeLabel, SpoolLabel, render_pdf
from scadbuddy.filament.label_templates import BY_ID, LabelTemplateChoice
from scadbuddy.filament.labels import temps_text
from tests.filament.test_label_fonts import needs_font

MM = 72 / 25.4

SPOOL = SpoolLabel(
    number=12, material="PLA Basic", colour="Jade White", brand="Bambu Lab", qr="scadbuddy:spool:x"
)
PLACE = CodeLabel(code="L-7", name="H2C AMS 1 slot 2", qr="https://sb.example/x/L-7")
FULL = SpoolLabel(
    number=12,
    material="PLA Basic",
    colour="Jade White",
    brand="Bambu Lab",
    qr="scadbuddy:spool:x",
    hex="#00AE42",
    location="Shelf A",
    temps="190\N{EN DASH}230 °C",
    weight="1000 g",
    note="Dry first",
    added="2026-11-20",
)


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


def _glyphs(pdf: bytes) -> list[tuple[str, float, float]]:
    """The first page's characters as PDFium reads them, each with its box's left and right
    edge in points, under the labels' PDFium lock."""
    out = []
    with label_png._PDFIUM_LOCK:
        document = pypdfium2.PdfDocument(pdf)
        try:
            page = document[0]
            textpage = page.get_textpage()
            for index in range(textpage.count_chars()):
                left, _, right, _ = textpage.get_charbox(index)
                char = chr(pypdfium2.raw.FPDFText_GetUnicode(textpage, index))
                out.append((char, left, right))
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
        render_pdf([CodeLabel("L-1", word, "https://x/x/L-1")], BY_ID["roll_50x25"], "roll")
    )
    assert drawn[1] == lines[0]


#: U+065A has an advance of its own in DejaVu Sans (1.5 mm at 3 mm), where U+0308 has none
#: (measured 2026-11-25 with `label_fonts.width_mm`). Only a mark with an advance can make a
#: cut fall between it and its base.
WIDE_MARK = "\N{ARABIC VOWEL SIGN SMALL V ABOVE}"


def test_a_cut_before_a_combining_mark_moves_before_its_base() -> None:
    # "Zoë" with a combining diaeresis: a cut at the mark keeps "Zo", never a bare "e".
    assert label_pdf._cut_back("Zoe\N{COMBINING DIAERESIS}…", 3) == 2
    assert label_pdf._cut_back("Zoe\N{COMBINING DIAERESIS}\N{COMBINING ACUTE ACCENT}", 4) == 2
    assert label_pdf._cut_back("Zoe", 2) == 2


@needs_font("NotoSansDevanagari-Regular.ttf")
def test_a_cut_keeps_a_spacing_vowel_sign_with_its_base() -> None:
    # U+093F is category Mc, combining class 0 and has an advance, so only the category
    # tells `_cut_back` that it belongs to the consonant before it.
    sign = "\N{DEVANAGARI VOWEL SIGN I}"
    ka, kha, ga, gha = "क", "ख", "ग", "घ"
    ellipsis = label_pdf.ELLIPSIS
    assert label_pdf._cut_back(f"{ka}{kha}{sign}{ga}", 2) == 1
    column = label_pdf._width(f"{ka}{kha}{ellipsis}", label_pdf.PLAIN, 3.0) + 0.05
    assert label_pdf._width(f"{ka}{kha}{sign}{ellipsis}", label_pdf.PLAIN, 3.0) > column
    fitted = label_pdf._fit(f"{ka}{kha}{sign}{ga}{gha}", label_pdf.PLAIN, 3.0, column)
    assert fitted == f"{ka}{ellipsis}"


def test_the_ellipsis_cut_keeps_a_combining_mark_with_its_base() -> None:
    name = f"Zoe{WIDE_MARK} Smith"
    # Room for "Zoe…" but not for "Zoe" with its mark and the ellipsis.
    column = label_pdf._width(f"Zoe{label_pdf.ELLIPSIS}", label_pdf.PLAIN, 3.0) + 0.1
    assert label_pdf._width(f"Zoe{WIDE_MARK}{label_pdf.ELLIPSIS}", label_pdf.PLAIN, 3.0) > column
    assert label_pdf._fit(name, label_pdf.PLAIN, 3.0, column) == f"Zo{label_pdf.ELLIPSIS}"


def test_a_long_word_breaks_before_a_base_and_its_combining_mark() -> None:
    word = f"Zoe{WIDE_MARK}lia"
    column = label_pdf._width("Zoe", label_pdf.BOLD, 3.2) + 0.1
    lines = label_pdf._wrap(word, label_pdf.BOLD, 3.2, column, 3)
    assert lines[0] == "Zo"
    assert lines[1].startswith(f"e{WIDE_MARK}")


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


def _measured(parts: list[tuple[str, str]], size: float) -> float:
    """The width in mm of ``parts``, each (registered font name, text), in its own font."""
    return sum(stringWidth(text, font, size * mm) for font, text in parts) / mm


@needs_font("NotoSansThai-Regular.ttf")
def test_a_line_in_two_fonts_is_cut_inside_the_second_run() -> None:
    thai = "ไทย" * 8
    odd = SpoolLabel(
        number=6, material=None, colour=f"Mint {thai}", brand=None, qr="scadbuddy:spool:t"
    )
    # Drawn first: it registers the fonts that `_measured` names.
    pdf = render_pdf([odd], BY_ID["roll_50x25"], "roll")
    column = 50 - 2 * label_pdf.QUIET_MM - label_pdf.QR_MM - label_pdf.PAD_MM
    dejavu, noto = "SBLabel-regular", "SBLabel-NotoSansThai-regular"
    # The longest head of the Thai run that fits with "Mint " before it and the ellipsis
    # after it, each measured in its own font.
    kept = max(
        end
        for end in range(len(thai) + 1)
        if _measured([(dejavu, "Mint "), (noto, thai[:end]), (dejavu, label_pdf.ELLIPSIS)], 3.0)
        <= column
    )
    assert 0 < kept < len(thai)
    want = f"Mint {thai[:kept]}{label_pdf.ELLIPSIS}"
    assert _lines(pdf)[1] == want
    # Each run is drawn where the one before it ends, and the line ends inside the column.
    drawn = [glyph for glyph in _glyphs(pdf) if glyph[0] in want and glyph[0] != " "]
    line = drawn[[char for char, _, _ in drawn].index("M") :][: len(want) - 1]
    assert "".join(char for char, _, _ in line) == want.replace(" ", "")
    first_thai = len("Mint")
    assert line[first_thai][1] >= line[first_thai - 1][2] - 0.01
    assert line[-1][1] >= line[-2][2] - 0.01
    text_x = 2 * label_pdf.QUIET_MM + label_pdf.QR_MM
    assert line[-1][2] <= (text_x + column) * MM + 0.01


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


# -- #7216: the fields a spool label prints (R-7216-9, R-7216-10) -------------------------


def test_the_default_fields_print_what_labels_printed_before() -> None:
    text = _text(render_pdf([FULL], BY_ID["ams_holder_75x55"], "roll"))
    assert all(s in text for s in ("#12", "PLA Basic", "Jade White", "Bambu Lab"))
    assert not any(s in text for s in ("#00AE42", "Shelf A", "1000 g"))


def test_only_the_chosen_fields_print_in_their_order() -> None:
    text = _text(render_pdf([FULL], BY_ID["ams_holder_75x55"], "roll", ["weight", "number", "hex"]))
    assert text.index("#12") < text.index("#00AE42") < text.index("1000 g")
    assert "PLA Basic" not in text


def test_every_field_prints_on_a_label_tall_enough() -> None:
    lines = _lines(render_pdf([FULL], BY_ID["ams_holder_75x55"], "roll", list(ALL_FIELDS)))
    assert lines == [
        "#12",
        "PLA Basic",
        "Jade White",
        "#00AE42",
        "Bambu Lab",
        "Shelf A",
        "190\N{EN DASH}230 °C",
        "1000 g",
        "Dry first",
        "2026-11-20",
    ]


def test_lines_that_do_not_fit_are_left_off_from_the_bottom() -> None:
    pdf = render_pdf([FULL], BY_ID["roll_50x25"], "roll", list(ALL_FIELDS))
    text = _text(pdf)
    assert "#12" in text and "2026-11-20" not in text
    # 22 mm of block on a 25 mm label: the number (9) and its gap (1), the material (4),
    # the colour (3.75) and one small line (3.25) make 21 mm; one more line is 24.25 mm.
    assert _lines(pdf) == ["#12", "PLA Basic", "Jade White", "#00AE42"]


def test_the_lines_kept_come_from_line_heights_not_widths() -> None:
    # A number that shrinks to fit a narrow column keeps as many lines as a short one: the
    # fit counts the number at its full size, so the browser and the PDF agree.
    template = LabelTemplateChoice(template="custom", width_mm=40, height_mm=25).resolved()
    short = _lines(render_pdf([FULL], template, "roll", list(ALL_FIELDS)))
    long = SpoolLabel(**(FULL.__dict__ | {"number": 123456}))
    assert len(_lines(render_pdf([long], template, "roll", list(ALL_FIELDS)))) == len(short) == 4


def test_the_number_line_stays_when_nothing_else_fits() -> None:
    lines = label_pdf.fit_lines(
        [
            label_pdf._Line("#1", label_pdf.BOLD, 9, leading=1.0, big=True),
            label_pdf._Line("x", label_pdf.PLAIN, 3.0, gap_mm=1.0),
        ],
        available=5,
    )
    assert [line.text for line in lines] == ["#1"]


def test_a_number_not_chosen_does_not_print() -> None:
    lines = _lines(render_pdf([FULL], BY_ID["roll_50x25"], "roll", ["material", "brand"]))
    assert lines == ["PLA Basic", "Bambu Lab"]


def test_without_the_qr_the_text_starts_at_the_quiet_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    drawn: list[str] = []
    real = label_pdf._draw_qr
    monkeypatch.setattr(label_pdf, "_draw_qr", lambda *a: drawn.append(a[1]))
    pdf = render_pdf([FULL], BY_ID["roll_50x25"], "roll", ["number", "material"])
    assert drawn == []
    # The number's first glyph starts at the quiet zone. Its box starts at the glyph's ink,
    # so the same "#" at the same size beside a QR code gives the side bearing.
    alone = next(left for char, left, _ in _glyphs(pdf) if char == "#")
    monkeypatch.setattr(label_pdf, "_draw_qr", real)
    with_qr = render_pdf([FULL], BY_ID["roll_50x25"], "roll", ["number", "material", "qr"])
    beside = next(left for char, left, _ in _glyphs(with_qr) if char == "#")
    bearing = beside - (2 * label_pdf.QUIET_MM + label_pdf.QR_MM) * MM
    assert 0 <= bearing < 1 * MM
    assert alone == pytest.approx(label_pdf.QUIET_MM * MM + bearing, abs=0.01)


def test_without_the_qr_the_column_takes_the_qrs_width_too() -> None:
    long = SpoolLabel(**(FULL.__dict__ | {"material": "PLA Basic Silk Matte Galaxy Glow"}))
    narrow = _lines(render_pdf([long], BY_ID["roll_50x25"], "roll", ["number", "material", "qr"]))
    wide = _lines(render_pdf([long], BY_ID["roll_50x25"], "roll", ["number", "material"]))
    assert narrow[1].endswith(label_pdf.ELLIPSIS)
    assert len(wide[1]) > len(narrow[1])


def test_a_spool_with_no_chosen_value_still_gets_its_page() -> None:
    bare = SpoolLabel(number=3, material=None, colour=None, brand=None, qr="q")
    assert len(_boxes(render_pdf([bare, bare], BY_ID["roll_50x25"], "roll", ["note"]))) == 2


def test_place_labels_ignore_the_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    drawn: list[str] = []
    real = label_pdf._draw_qr

    def spy(canvas: object, payload: str, x: float, y: float) -> None:
        drawn.append(payload)
        real(canvas, payload, x, y)  # type: ignore[arg-type]

    monkeypatch.setattr(label_pdf, "_draw_qr", spy)
    assert "L-7" in _text(render_pdf([PLACE], BY_ID["roll_50x25"], "roll", ["note"]))
    assert drawn == [PLACE.qr]


def test_label_output_puts_fields_in_order_and_refuses_none() -> None:
    assert LabelOutput.model_validate({"fields": ["qr", "number"]}).fields == ["number", "qr"]
    for bad in ([], ["number", "number"]):
        with pytest.raises(ValidationError):
            LabelOutput.model_validate({"fields": bad})


@pytest.mark.parametrize(
    ("low", "high", "want"),
    [
        (190, 230, "190\N{EN DASH}230 °C"),
        (None, 220, "220 °C"),
        (220, None, "220 °C"),
        (220, 220, "220 °C"),
        (None, None, None),
    ],
)
def test_temps_text(low: int | None, high: int | None, want: str | None) -> None:
    assert temps_text(low, high) == want
