"""The labels as PNG (#7216, plan `docs/superpowers/plans/2026-11-25-label-outputs.md`
R-7216-5 to R-7216-7): the labels PDF rasterised by PDFium (pypdfium2), one greyscale PNG a
page that carries its dpi. Several PNGs go in one stored ZIP whose bytes do not depend on
the date.

"PDFium is inherently not thread-safe" (pypdfium2's README, "Known limitations",
https://github.com/pypdfium2-team/pypdfium2#incompatibility-with-threading), so every PDFium
call holds one lock for the process."""

from __future__ import annotations

import io
import threading
import zipfile
from collections.abc import Sequence
from typing import Final

import pypdfium2
from PIL import Image

from scadbuddy.filament.label_pdf import Label, Layout, SpoolLabel, page_count, sheet_of
from scadbuddy.filament.label_templates import PAGE_MM, LabelTemplate

#: The most pixels one PNG answer holds, all pages together (R-7216-7).
MAX_PNG_PIXELS: Final = 400_000_000
#: A PDF point is 1/72 inch.
_POINTS_PER_INCH: Final = 72
_MM_PER_INCH: Final = 25.4
#: Every ZIP entry's date: the first a ZIP can hold.
_ZIP_DATE: Final = (1980, 1, 1, 0, 0, 0)

_PDFIUM_LOCK: Final = threading.Lock()


class PngTooLargeError(ValueError):
    """The PNGs would hold more than `MAX_PNG_PIXELS`."""


def _px(length_mm: float, dpi: int) -> int:
    return round(length_mm / _MM_PER_INCH * dpi)


def png_pixels(template: LabelTemplate, layout: Layout, count: int, dpi: int) -> int:
    """The pixels of the PNGs of ``count`` labels at ``dpi``, every page counted. It reads
    no PDF, so the route checks it before it draws."""
    sheet = sheet_of(template, layout)
    width, height = PAGE_MM[sheet.page] if sheet else (template.width_mm, template.height_mm)
    return page_count(template, layout, count) * _px(width, dpi) * _px(height, dpi)


def check_png_size(template: LabelTemplate, layout: Layout, count: int, dpi: int) -> None:
    """Raise `PngTooLargeError` when the PNGs would hold more than `MAX_PNG_PIXELS`."""
    pixels = png_pixels(template, layout, count, dpi)
    if pixels > MAX_PNG_PIXELS:
        raise PngTooLargeError(
            f"The PNGs would hold {pixels / 1e6:.0f} megapixels, more than the limit of "
            f"{MAX_PNG_PIXELS / 1e6:.0f}. Choose PDF, a lower dpi or fewer labels."
        )


def render_pngs(pdf: bytes, dpi: int) -> list[bytes]:
    """One greyscale PNG of each page of ``pdf`` at ``dpi``, which each PNG carries."""
    pngs: list[bytes] = []
    with _PDFIUM_LOCK:
        document = pypdfium2.PdfDocument(pdf)
        try:
            for at in range(len(document)):
                page = document[at]
                scale = dpi / _POINTS_PER_INCH
                try:
                    image: Image.Image = page.render(scale=scale).to_pil().convert("L")
                    # pypdfium2 rounds the size up (`math.ceil` in `PdfPage.render`), so a
                    # partial last pixel is cut: the size is the rounded one `png_pixels`
                    # counts.
                    size = (round(page.get_width() * scale), round(page.get_height() * scale))
                finally:
                    page.close()
                if image.size != size:
                    image = image.crop((0, 0, *size))
                out = io.BytesIO()
                image.save(out, "PNG", dpi=(dpi, dpi))
                pngs.append(out.getvalue())
        finally:
            document.close()
    return pngs


def png_names(labels: Sequence[Label], sheet: bool, pages: int) -> list[str]:
    """The ZIP's entry names: a roll's by the label (`spool-<number>.png`, `L-<n>.png`), a
    sheet's by the page (`sheet-<n>.png`)."""
    if sheet:
        return [f"sheet-{at}.png" for at in range(1, pages + 1)]
    return [
        f"spool-{label.number}.png" if isinstance(label, SpoolLabel) else f"{label.code}.png"
        for label in labels
    ]


def zip_pngs(names: Sequence[str], pngs: Sequence[bytes]) -> bytes:
    """A stored ZIP of ``pngs`` under ``names``, every entry dated 1980-01-01."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for name, png in zip(names, pngs, strict=True):
            archive.writestr(zipfile.ZipInfo(name, date_time=_ZIP_DATE), png)
    return out.getvalue()
