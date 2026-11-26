"""The labels PDF's fonts (#7216, plan `docs/superpowers/plans/2026-11-25-label-outputs.md`
R-7216-1 to R-7216-4).

The text is DejaVu Sans, embedded as a subset (R-7216-1). A character DejaVu lacks takes
the first font of a fixed Noto chain that has it (R-7216-2). A character no font holds
prints as U+FFFD from DejaVu. A font file is loaded and registered with ReportLab only when
a character needs it, once per process. A missing DejaVu file is an error
(`LabelFontMissingError`, R-7216-4); a missing Noto file is skipped and logged once.

There is no shaping, no right-to-left order and no CJK font (R-7216-3): Arabic prints
unjoined, Hebrew and Arabic print in stored order, and CJK prints as U+FFFD. Follow-up
issue #7707. ReportLab 5.1 shapes text only with the optional `uharfbuzz` and reorders it
only with the optional `rlbidi` (`reportlab/pdfbase/ttfonts.py` L67,
`reportlab/pdfgen/textobject.py` L22, read in the backend's locked 5.1.0)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Final, Literal

from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

log = logging.getLogger(__name__)

Weight = Literal["regular", "bold"]

#: DejaVu Sans from `fonts-dejavu-core`, which the Dockerfile's `base` stage installs through
#: `fonts-dejavu` (measured 2026-11-25 with `ls` in the pinned base image and `dpkg -S` in an
#: app image).
DEJAVU: Final[dict[Weight, Path]] = {
    "regular": Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    "bold": Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
}
#: `fonts-noto-core`'s folder (the Dockerfile's `base` stage). The image holds no CJK font.
FALLBACK_DIR: Final = Path("/usr/share/fonts/truetype/noto")
#: The fallback chain, in order (R-7216-2). Each family is `<family>-Regular.ttf` and
#: `<family>-Bold.ttf` in `FALLBACK_DIR`.
FALLBACKS: Final[tuple[str, ...]] = (
    "NotoSans",
    "NotoSansArmenian",
    "NotoSansGeorgian",
    "NotoSansHebrew",
    "NotoSansArabic",
    "NotoSansDevanagari",
    "NotoSansBengali",
    "NotoSansTamil",
    "NotoSansSinhala",
    "NotoSansThai",
    "NotoSansLao",
    "NotoSansKhmer",
    "NotoSansMyanmar",
    "NotoSansEthiopic",
)
REPLACEMENT: Final = "\N{REPLACEMENT CHARACTER}"

_FILE_WEIGHT: Final[dict[Weight, str]] = {"regular": "Regular", "bold": "Bold"}


class LabelFontMissingError(RuntimeError):
    """A DejaVu Sans file the labels PDF needs is absent."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"The labels font {path} is missing: install fonts-dejavu-core (the app image has it)."
        )
        self.path = path


_lock = threading.Lock()
#: Loaded fonts by (family, weight), family `None` for DejaVu. A fallback whose file is
#: missing is kept as `None`, so it is looked for and logged once.
_fonts: dict[tuple[str | None, Weight], TTFont | None] = {}
#: The registered font name of each character already placed, by weight.
_placed: dict[tuple[str, Weight], tuple[str, str]] = {}


def reset_for_tests() -> None:
    """Forget every loaded font and placed character."""
    with _lock:
        _fonts.clear()
        _placed.clear()


def _name(family: str | None, weight: Weight) -> str:
    return f"SBLabel-{weight}" if family is None else f"SBLabel-{family}-{weight}"


def _font(family: str | None, weight: Weight) -> TTFont | None:
    key = (family, weight)
    if key in _fonts:
        return _fonts[key]
    with _lock:
        if key in _fonts:
            return _fonts[key]
        if family is None:
            path = DEJAVU[weight]
            if not path.is_file():
                raise LabelFontMissingError(path)
        else:
            path = FALLBACK_DIR / f"{family}-{_FILE_WEIGHT[weight]}.ttf"
            if not path.is_file():
                log.warning("labels: the fallback font %s is missing; it is skipped", path)
                _fonts[key] = None
                return None
        font = TTFont(_name(family, weight), str(path))
        pdfmetrics.registerFont(font)
        _fonts[key] = font
        return font


def _covers(font: TTFont, char: str) -> bool:
    return ord(char) in font.face.charToGlyph


def _place(char: str, weight: Weight) -> tuple[str, str]:
    """The registered font name for ``char``, and the character it prints."""
    key = (char, weight)
    placed = _placed.get(key)
    if placed is not None:
        return placed
    dejavu = _font(None, weight)
    assert dejavu is not None
    placed = (_name(None, weight), char if _covers(dejavu, char) else REPLACEMENT)
    if placed[1] == REPLACEMENT and char != REPLACEMENT:
        for family in FALLBACKS:
            font = _font(family, weight)
            if font is not None and _covers(font, char):
                placed = (_name(family, weight), char)
                break
    _placed[key] = placed
    return placed


def runs(text: str, weight: Weight) -> list[tuple[str, str]]:
    """``text`` as (registered font name, text) runs, adjacent characters of one font
    joined. Raises `LabelFontMissingError` when DejaVu's file is absent."""
    out: list[tuple[str, str]] = []
    for char in text:
        name, printed = _place(char, weight)
        if out and out[-1][0] == name:
            out[-1] = (name, out[-1][1] + printed)
        else:
            out.append((name, printed))
    return out


def width_mm(text: str, weight: Weight, size_mm: float) -> float:
    """``text``'s width at ``size_mm``, in mm: the sum of its runs' widths."""
    return sum(
        pdfmetrics.stringWidth(part, name, size_mm * mm) / mm for name, part in runs(text, weight)
    )
