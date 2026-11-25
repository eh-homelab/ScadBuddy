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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas

from scadbuddy.filament.label_fonts import Weight, runs, width_mm
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


@dataclass(frozen=True)
class SpoolLabel:
    number: int
    material: str | None
    colour: str | None
    brand: str | None
    #: What the QR code holds: `scadbuddy:spool:<id>`.
    qr: str


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


def render_pdf(labels: Sequence[Label], template: LabelTemplate, layout: Layout) -> bytes:
    """The PDF of ``labels`` at ``template``'s size, in their order."""
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
        if page_at:
            canvas.showPage()
        on_page = labels[page_at * per_page : (page_at + 1) * per_page]
        for at, label in enumerate(on_page):
            x, top = (0.0, 0.0) if sheet is None else _cell(sheet, at, width, height)
            # ReportLab's origin is the page's bottom left.
            _draw_label(canvas, label, x, page[1] - top - height, width, height)
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
    canvas: Canvas, label: Label, x: float, y: float, width: float, height: float
) -> None:
    """One label with its bottom left at (``x``, ``y``) mm."""
    _draw_qr(canvas, label.qr, x + QUIET_MM, y + (height - QR_MM) / 2)
    text_x = x + 2 * QUIET_MM + QR_MM
    column = width - 2 * QUIET_MM - QR_MM - PAD_MM
    lines = (
        _spool_lines(label, column)
        if isinstance(label, SpoolLabel)
        else _place_lines(label, column)
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


def _spool_lines(label: SpoolLabel, column: float) -> list[_Line]:
    number = f"#{label.number}"
    lines = [_Line(number, BOLD, _big_size(number, 9, column), leading=1.0)]
    rows: tuple[tuple[str | None, Weight, float], ...] = (
        (label.material, BOLD, 3.2),
        (label.colour, PLAIN, 3.0),
        (label.brand, PLAIN, 2.6),
    )
    for text, font, size in rows:
        if text:
            cut = _fit(text, font, size, column)
            lines.append(_Line(cut, font, size, gap_mm=1.0 if len(lines) == 1 else 0.0))
    return lines


def _place_lines(label: PlaceLabel, column: float) -> list[_Line]:
    code = label.code
    lines = [_Line(code, BOLD, _big_size(code, 8, column), leading=1.0)]
    for at, text in enumerate(_wrap(label.name, BOLD, 3.2, column, 2)):
        lines.append(_Line(text, BOLD, 3.2, gap_mm=1.0 if at == 0 else 0.0))
    return lines


def _width(text: str, font: Weight, size: float) -> float:
    return width_mm(text, font, size)


def _fit(text: str, font: Weight, size: float, column: float) -> str:
    """``text``, cut with an ellipsis to fit ``column`` mm."""
    if _width(text, font, size) <= column:
        return text
    while text and _width(text + ELLIPSIS, font, size) > column:
        text = text[:-1]
    return text.rstrip() + ELLIPSIS


def _wrap(text: str, font: Weight, size: float, column: float, most: int) -> list[str]:
    """``text`` in at most ``most`` lines of ``column`` mm, broken at spaces, the last cut.
    A word wider than the column is broken where it overflows, and the rest goes on the
    next line, so only the last line is ever cut."""
    lines: list[str] = []
    words = text.split()
    while words and len(lines) < most - 1:
        line = words.pop(0)
        if _width(line, font, size) > column:
            head = line
            while len(head) > 1 and _width(head, font, size) > column:
                head = head[:-1]
            rest = line[len(head) :]
            # One character wider than the column stays whole: nothing runs on.
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
