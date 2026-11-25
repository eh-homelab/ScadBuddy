"""The labels PDF (#7157, plan `docs/superpowers/plans/2026-11-23-label-templates-pdf.md`):
the browser's labels (`frontend/src/components/filament/Label.tsx`) drawn with ReportLab
(R-7157-3), at a template's size. The QR code is 19.8 mm with 2.6 mm of quiet zone on the
left, centred top to bottom; the text column beside it is centred too. A roll template
prints one label a page, a sheet template on its grid, and the `sheet` layout of a roll
template packs the labels on Letter paper (R-7157-7). The text is DejaVu Sans with
a Noto fallback chain, embedded (`label_fonts.py`; #7216, R-7216-1 to R-7216-3): each line is
drawn as runs of one font each, measured by `label_fonts.width_mm`."""

from __future__ import annotations

import io
import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas

from scadbuddy.filament.label_fonts import Weight, runs, width_mm
from scadbuddy.filament.label_output import ALL_FIELDS, DEFAULT_FIELDS, LabelField
from scadbuddy.filament.label_templates import PAGE_MM, LabelSheet, LabelTemplate

Layout = Literal["roll", "sheet"]

#: The browser label's QR box and quiet zone (`labelSheet.ts` `QR_MM`, `QUIET_MM`).
QR_MM: Final = 19.8
QUIET_MM: Final = 2.6
#: The text column's right padding.
PAD_MM: Final = 1.5
#: The packed sheet (R-7157-7): US Letter, 10 mm margins, 2 mm gaps, 190 mm of labels across
#: as the browser's sheet (`LabelsPage.tsx` `printCss`).
PACKED_PAGE: Final = "letter"
PACKED_WIDTH_MM: Final = 190.0
PACKED_MARGIN_MM: Final = 10.0
PACKED_GAP_MM: Final = 2.0
#: Line height over font size, as Tailwind's `leading-tight`.
LEADING: Final = 1.25
ELLIPSIS: Final = "…"

BOLD: Final[Weight] = "bold"
PLAIN: Final[Weight] = "regular"

#: The line sizes, in mm, of the spool label's fields (#7216, R-7216-10). The big number and a
#: place's code have a line height of their size (`BIG_LEADING`); every other line is
#: `LEADING` times its size, and the first line under the big text has `GAP_MM` above it.
#: The browser label (`frontend/src/components/filament/labelSheet.ts` `fitLines`) must use
#: these same values, with `PAD_MM`, so it keeps the same lines as the PDF.
NUMBER_MM: Final = 9.0
CODE_MM: Final = 8.0
PLACE_NAME_MM: Final = 3.2
BIG_LEADING: Final = 1.0
GAP_MM: Final = 1.0
FIELD_MM: Final[Mapping[LabelField, float]] = {
    "material": 3.2,
    "colour": 3.0,
    "hex": 2.6,
    "brand": 2.6,
    "location": 2.6,
    "temps": 2.6,
    "weight": 2.6,
    "note": 2.6,
    "added": 2.6,
}
#: The fields that print in bold; the rest are regular.
BOLD_FIELDS: Final[frozenset[LabelField]] = frozenset({"material"})


@dataclass(frozen=True)
class SpoolLabel:
    number: int
    material: str | None
    colour: str | None
    brand: str | None
    #: What the QR code holds: `scadbuddy:spool:<id>`.
    qr: str
    #: The effective colour as `#RRGGBB` (#7216, R-7216-9).
    hex: str | None = None
    #: The name of the spool's place.
    location: str | None = None
    #: The nozzle temperatures, the range or one value (`labels.temps_text`).
    temps: str | None = None
    #: The label weight, `1000 g`.
    weight: str | None = None
    #: The note's first line.
    note: str | None = None
    #: The day the row was made, in UTC, ISO.
    added: str | None = None


@dataclass(frozen=True)
class PlaceLabel:
    code: str
    name: str
    #: What the QR code holds: `<base>/x/L-<n>`.
    qr: str


Label = SpoolLabel | PlaceLabel


@dataclass(frozen=True)
class _Line:
    text: str
    font: Weight
    size_mm: float
    #: Space above the line, in mm.
    gap_mm: float = 0.0
    #: The line's height over its size: the big number and code are `leading-none`.
    leading: float = LEADING
    #: The size the fit counts, when it is not ``size_mm``: the big text's full size, which
    #: does not depend on how wide its glyphs are in a font (R-7216-10).
    fit_size_mm: float | None = None
    #: The big number or code: a fit never leaves it off.
    big: bool = False

    @property
    def fit_height_mm(self) -> float:
        """The space the line takes in the fit: its gap and its line box."""
        size = self.size_mm if self.fit_size_mm is None else self.fit_size_mm
        return self.gap_mm + size * self.leading


def fit_lines(lines: Sequence[_Line], available: float) -> list[_Line]:
    """``lines`` less the last ones while they are taller than ``available`` mm (R-7216-10).
    The big number or code always stays. Only the line heights count, never a glyph's width,
    so the browser label keeps the same lines in another font."""
    kept = list(lines)
    while kept and not kept[-1].big and sum(line.fit_height_mm for line in kept) > available + 1e-9:
        kept.pop()
    return kept


def render_pdf(
    labels: Sequence[Label],
    template: LabelTemplate,
    layout: Layout,
    fields: Sequence[LabelField] = DEFAULT_FIELDS,
) -> bytes:
    """The PDF of ``labels`` at ``template``'s size, in their order. A spool label prints
    ``fields`` (in ``ALL_FIELDS`` order); a place label always prints its code, name and QR
    code."""
    buffer = io.BytesIO()
    width, height = template.width_mm, template.height_mm
    sheet = sheet_of(template, layout)
    page = PAGE_MM[sheet.page] if sheet else (width, height)
    canvas = Canvas(
        buffer,
        pagesize=(page[0] * mm, page[1] * mm),
        invariant=True,
        # types-reportlab types it as a number; ReportLab takes a font name.
        initialFontName=_initial_font(),  # type: ignore[arg-type]
    )
    canvas.setTitle("ScadBuddy labels")
    canvas.setAuthor("ScadBuddy")
    per_page = sheet.cols * sheet.rows if sheet else 1
    for page_at in range(page_count(template, layout, len(labels))):
        on_page = labels[page_at * per_page : (page_at + 1) * per_page]
        for at, label in enumerate(on_page):
            x, top = (0.0, 0.0) if sheet is None else _cell(sheet, at, width, height)
            # ReportLab's origin is the page's bottom left.
            _draw_label(canvas, label, x, page[1] - top - height, width, height, fields)
        # Each page is closed here: `save` closes the last page only when something is drawn
        # on it, and a spool label with no chosen value draws nothing (#7216).
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def sheet_of(template: LabelTemplate, layout: Layout) -> LabelSheet | None:
    """The sheet the labels are laid on: the template's own, the packed Letter sheet for the
    `sheet` layout of a one-label template, else ``None`` (one label a page)."""
    if template.sheet:
        return template.sheet
    return _packed(template.width_mm, template.height_mm) if layout == "sheet" else None


def page_count(template: LabelTemplate, layout: Layout, count: int) -> int:
    """How many pages ``render_pdf`` draws for ``count`` labels."""
    sheet = sheet_of(template, layout)
    return math.ceil(count / (sheet.cols * sheet.rows)) if sheet else count


def _packed(width: float, height: float) -> LabelSheet:
    across = PACKED_WIDTH_MM + PACKED_GAP_MM
    down = PAGE_MM[PACKED_PAGE][1] - 2 * PACKED_MARGIN_MM + PACKED_GAP_MM
    return LabelSheet(
        page=PACKED_PAGE,
        cols=max(1, math.floor(across / (width + PACKED_GAP_MM))),
        rows=max(1, math.floor(down / (height + PACKED_GAP_MM))),
        top_mm=PACKED_MARGIN_MM,
        left_mm=PACKED_MARGIN_MM,
        col_gap_mm=PACKED_GAP_MM,
        row_gap_mm=PACKED_GAP_MM,
    )


def _cell(sheet: LabelSheet, index: int, width: float, height: float) -> tuple[float, float]:
    """A cell's left and top, in mm from the page's top left."""
    col, row = index % sheet.cols, index // sheet.cols
    return (
        sheet.left_mm + col * (width + sheet.col_gap_mm),
        sheet.top_mm + row * (height + sheet.row_gap_mm),
    )


def _draw_label(
    canvas: Canvas,
    label: Label,
    x: float,
    y: float,
    width: float,
    height: float,
    fields: Sequence[LabelField],
) -> None:
    """One label with its bottom left at (``x``, ``y``) mm. A spool label with no `qr`
    field has no QR code, and its text column starts at the quiet zone (R-7216-10)."""
    if isinstance(label, PlaceLabel) or "qr" in fields:
        _draw_qr(canvas, label.qr, x + QUIET_MM, y + (height - QR_MM) / 2)
        text_x = x + 2 * QUIET_MM + QR_MM
        column = width - 2 * QUIET_MM - QR_MM - PAD_MM
    else:
        text_x = x + QUIET_MM
        column = width - QUIET_MM - PAD_MM
    lines = fit_lines(
        _spool_lines(label, column, fields)
        if isinstance(label, SpoolLabel)
        else _place_lines(label, column),
        height - 2 * PAD_MM,
    )
    block = sum(line.gap_mm + line.size_mm * line.leading for line in lines)
    top = y + (height + block) / 2
    for line in lines:
        top -= line.gap_mm
        box = line.size_mm * line.leading
        # The baseline sits about a fifth of the size above the line box's foot.
        baseline = top - box + (box - line.size_mm) / 2 + 0.2 * line.size_mm
        at = text_x
        for name, part in runs(line.text, line.font):
            canvas.setFont(name, line.size_mm * mm)
            canvas.drawString(at * mm, baseline * mm, part)
            at += width_mm(part, line.font, line.size_mm)
        top -= box


def _draw_qr(canvas: Canvas, payload: str, x: float, y: float) -> None:
    """The QR code of ``payload`` at level M with no border, ``QR_MM`` square, at (x, y) mm."""
    widget = QrCodeWidget(payload, barLevel="M", barBorder=0)
    left, bottom, right, top = widget.getBounds()
    side = QR_MM * mm
    drawing = Drawing(
        side,
        side,
        transform=[side / (right - left), 0, 0, side / (top - bottom), 0, 0],
        initialFontName=_initial_font(),
    )
    drawing.add(widget)
    renderPDF.draw(drawing, canvas, x * mm, y * mm)


def _initial_font() -> str:
    """DejaVu's registered name, for the font a page and a QR drawing start in. ReportLab's
    default is Helvetica, which the PDF would then name but not embed. A missing DejaVu
    raises `LabelFontMissingError` here, before any page is drawn."""
    return runs(" ", PLAIN)[0][0]


def _big_size(text: str, cap: float, column: float) -> float:
    """The number's or code's size: ``cap`` mm, or the size at which its measured width in
    bold fills the column, whichever is smaller. A width scales with the size."""
    at_cap = _width(text, BOLD, cap)
    return cap if at_cap <= column else cap * column / at_cap


def _big_line(text: str, cap: float, column: float) -> _Line:
    size = _big_size(text, cap, column)
    return _Line(text, BOLD, size, leading=BIG_LEADING, fit_size_mm=cap, big=True)


def _spool_lines(label: SpoolLabel, column: float, fields: Sequence[LabelField]) -> list[_Line]:
    """A line for each chosen field with a value, in ``ALL_FIELDS`` order: the big number
    first when it is chosen, and `GAP_MM` under it."""
    lines: list[_Line] = []
    if "number" in fields:
        lines.append(_big_line(f"#{label.number}", NUMBER_MM, column))
    for field in ALL_FIELDS:
        if field not in fields or field not in FIELD_MM:
            continue
        text = getattr(label, field)
        if text:
            font = BOLD if field in BOLD_FIELDS else PLAIN
            size = FIELD_MM[field]
            cut = _fit(text, font, size, column)
            under_big = len(lines) == 1 and lines[0].big
            lines.append(_Line(cut, font, size, gap_mm=GAP_MM if under_big else 0.0))
    return lines


def _place_lines(label: PlaceLabel, column: float) -> list[_Line]:
    lines = [_big_line(label.code, CODE_MM, column)]
    for at, text in enumerate(_wrap(label.name, BOLD, PLACE_NAME_MM, column, 2)):
        lines.append(_Line(text, BOLD, PLACE_NAME_MM, gap_mm=GAP_MM if at == 0 else 0.0))
    return lines


def _width(text: str, font: Weight, size: float) -> float:
    return width_mm(text, font, size)


def _is_mark(ch: str) -> bool:
    return unicodedata.combining(ch) != 0 or unicodedata.category(ch) in {"Mn", "Mc", "Me"}


def _cut_back(text: str, end: int) -> int:
    """``end``, moved back to before the base character when a cut there would fall before
    a mark, so a cut never parts a base from the marks after it (#7216). A mark is a
    character with a combining class or a category Mn, Mc or Me: a spacing vowel sign such
    as U+093F has class 0 and an advance of its own."""
    while 0 < end < len(text) and _is_mark(text[end]):
        end -= 1
    return end


def _fit(text: str, font: Weight, size: float, column: float) -> str:
    """``text``, cut with an ellipsis to fit ``column`` mm."""
    if _width(text, font, size) <= column:
        return text
    end = len(text)
    while end and _width(text[:end] + ELLIPSIS, font, size) > column:
        end = _cut_back(text, end - 1)
    return text[:end].rstrip() + ELLIPSIS


def _wrap(text: str, font: Weight, size: float, column: float, most: int) -> list[str]:
    """``text`` in at most ``most`` lines of ``column`` mm, broken at spaces, the last cut.
    A word wider than the column is broken where it overflows, and the rest goes on the
    next line, so only the last line is ever cut."""
    lines: list[str] = []
    words = text.split()
    while words and len(lines) < most - 1:
        line = words.pop(0)
        if _width(line, font, size) > column:
            end = len(line)
            while _width(line[:end], font, size) > column:
                back = _cut_back(line, end - 1)
                if back < 1:
                    break
                end = back
            head, rest = line[:end], line[end:]
            # One character (with its marks) wider than the column stays whole: nothing
            # runs on.
            if rest:
                words.insert(0, rest)
            lines.append(head)
            continue
        while words and _width(f"{line} {words[0]}", font, size) <= column:
            line = f"{line} {words.pop(0)}"
        lines.append(_fit(line, font, size, column))
    if words:
        lines.append(_fit(" ".join(words), font, size, column))
    return lines
