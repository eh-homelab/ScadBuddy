"""#7216: the labels PDF's fonts (`filament/label_fonts.py`; plan
`docs/superpowers/plans/2026-11-25-label-outputs.md` R-7216-1 to R-7216-4)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from scadbuddy.filament import label_fonts
from scadbuddy.filament.label_fonts import (
    FALLBACK_DIR,
    REPLACEMENT,
    LabelFontMissingError,
    runs,
    width_mm,
)

#: CI's `backend-pytest` sets it in the `test` image, which holds the Noto fonts
#: (`fonts-noto-core`), so a test that needs one fails there when it is gone.
FONTS_REQUIRED = os.environ.get("SCADBUDDY_TEST_FONTS_REQUIRED") == "1"


def needs_font(name: str) -> pytest.MarkDecorator:
    """Skip outside the image when the Noto file ``name`` is absent; never where
    `SCADBUDDY_TEST_FONTS_REQUIRED=1`."""
    return pytest.mark.skipif(
        not (FALLBACK_DIR / name).exists() and not FONTS_REQUIRED,
        reason=f"{FALLBACK_DIR / name} is absent: the image's Noto fonts (fonts-noto-core)",
    )


@pytest.fixture(autouse=True)
def _fresh_fonts() -> Iterator[None]:
    label_fonts.reset_for_tests()
    yield
    label_fonts.reset_for_tests()


def test_dejavu_holds_cyrillic_greek_and_latin_in_one_run() -> None:
    assert [font for font, _ in runs("Пётр Ωμέγα Zoë", "regular")] == ["SBLabel-regular"]


def test_a_character_no_font_holds_is_the_replacement_character() -> None:
    # The image holds no CJK font (R-7216-3). DejaVu Sans 2.37 holds U+1F600, so an emoji
    # is no such character (measured 2026-11-25: ReportLab's `charToGlyph` of DejaVuSans.ttf).
    assert "".join(text for _, text in runs("A翡", "bold")) == f"A{REPLACEMENT}"


def test_the_replacement_character_prints_in_dejavu() -> None:
    assert runs("翡", "regular") == [("SBLabel-regular", REPLACEMENT)]


@needs_font("NotoSansThai-Regular.ttf")
def test_thai_takes_the_noto_thai_font() -> None:
    assert runs("ไทย", "regular") == [("SBLabel-NotoSansThai-regular", "ไทย")]


@needs_font("NotoSansThai-Bold.ttf")
def test_a_mixed_line_splits_into_runs_by_font() -> None:
    assert runs("A ไทย B", "bold") == [
        ("SBLabel-bold", "A "),
        ("SBLabel-NotoSansThai-bold", "ไทย"),
        ("SBLabel-bold", " B"),
    ]


def test_a_missing_fallback_file_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(label_fonts, "FALLBACK_DIR", tmp_path)
    label_fonts.reset_for_tests()
    assert runs("ไทย", "regular") == [("SBLabel-regular", REPLACEMENT * 3)]
    runs("ไทย", "regular")
    missing = [r for r in caplog.records if "NotoSansThai-Regular.ttf" in r.getMessage()]
    assert len(missing) == 1


def test_the_width_is_the_sum_of_the_runs_widths() -> None:
    whole = width_mm("AB", "regular", 3.0)
    assert whole == pytest.approx(width_mm("A", "regular", 3.0) + width_mm("B", "regular", 3.0))
    assert width_mm("AB", "regular", 6.0) == pytest.approx(2 * whole)


def test_a_missing_dejavu_names_its_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(label_fonts.DEJAVU, "regular", tmp_path / "none.ttf")
    label_fonts.reset_for_tests()
    with pytest.raises(LabelFontMissingError, match=r"none\.ttf"):
        runs("a", "regular")
