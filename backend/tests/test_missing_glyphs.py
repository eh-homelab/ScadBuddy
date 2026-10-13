"""#2358 — characters a font has no glyph for, which OpenSCAD's ``text()`` leaves out
without a word: read off fontconfig's charset for the face a render would get."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from scadbuddy.library.fonts import font_coverage, missing_glyphs, parse_charset
from scadbuddy.render.jobs import missing_glyph_warnings
from scadbuddy.render.schema import CustomizerSchema, Parameter

#: Basic Latin, Latin-1 and the letters a lobster needs: no emoji, no CJK.
LATIN = ("Lobster Two", parse_charset("20-7e a0-ff 152-153"))


def test_a_charset_is_read_as_ranges_and_single_points() -> None:
    assert parse_charset("20-7e a0-17f 2c6\n") == [(0x20, 0x7E), (0xA0, 0x17F), (0x2C6, 0x2C6)]
    assert parse_charset("") == []
    assert parse_charset("zz 41") == [(0x41, 0x41)]


def test_the_characters_the_font_lacks_are_named_once_in_order() -> None:
    text = 'Zoë 😀 "q" \\ <b> 漢 😀'
    assert missing_glyphs("Lobster Two:style=Bold", text, LATIN) == ["😀", "漢"]


def test_blanks_joiners_and_variation_selectors_need_no_glyph() -> None:
    assert missing_glyphs("Lobster Two", "a b\tc\n\u200d\ufe0f\u00a0", LATIN) == []


def test_nothing_is_said_for_the_default_font_another_family_or_no_fontconfig() -> None:
    assert missing_glyphs("", "😀", LATIN) == []
    assert missing_glyphs(":style=Bold", "😀", LATIN) == []
    # fontconfig gave another family: the family is not installed, which #1286 reports.
    assert missing_glyphs("Pacifico", "😀", LATIN) == []
    assert missing_glyphs("Lobster Two", "😀", None) == []
    # Compared as fontconfig compares names.
    assert missing_glyphs("lobstertwo", "😀", LATIN) == ["😀"]


def test_the_coverage_asks_for_a_face_a_render_can_draw(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout="Lobster Two,Lobster Two Bold\n20-7e a0-ff", stderr=""
        )

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert font_coverage("Lobster Two:style=Bold", {}) == (
        "Lobster Two",
        [(0x20, 0x7E), (0xA0, 0xFF)],
    )
    assert seen == [
        [
            "fc-match",
            "-f",
            "%{family}\n%{charset}",
            "Lobster Two:style=Bold:outline=true:scalable=true",
        ]
    ]


def _schema(*parameters: Parameter) -> CustomizerSchema:
    return CustomizerSchema(parameters=list(parameters))


def test_a_render_warns_of_each_font_that_leaves_characters_out() -> None:
    schema = _schema(
        Parameter(name="word", type="string", initial="Zoë"),
        Parameter(name="font", type="font", initial="Lobster Two:style=Bold"),
        Parameter(name="plain", type="font", initial=""),
    )
    asked: list[str] = []

    def coverage(font: str) -> tuple[str, list[tuple[int, int]]]:
        asked.append(font)
        return LATIN

    warnings = missing_glyph_warnings(schema, {"word": "Zoë 😀 漢"}, coverage)
    assert warnings == [
        "font 'Lobster Two:style=Bold' has no glyph for 😀 漢; the text leaves them out"
    ]
    # The default font names no family: fontconfig is not asked about it.
    assert asked == ["Lobster Two:style=Bold"]
    assert missing_glyph_warnings(schema, {"word": "Zoë"}, coverage) == []


def test_a_model_with_no_text_asks_nothing() -> None:
    schema = _schema(Parameter(name="font", type="font", initial="Lobster Two"))
    assert missing_glyph_warnings(schema, {}, lambda font: pytest.fail("asked")) == []


REAL_FC_MATCH = shutil.which("fc-match")


@pytest.mark.skipif(not REAL_FC_MATCH, reason="fontconfig is not installed")
def test_against_the_real_fontconfig_an_unknown_script_is_missing() -> None:
    families = subprocess.run(
        ["fc-list", ":outline=true:scalable=true:charset=41", "family"],
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    family = next((line.split(",")[0] for line in families if line.strip()), None)
    if family is None:
        pytest.skip("no outline font with a Latin A installed")
    # A private-use code point no font ships a glyph for.
    assert missing_glyphs(family, "A\U000f0000", font_coverage(family, {})) == ["\U000f0000"]
