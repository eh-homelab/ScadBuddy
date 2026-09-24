from __future__ import annotations

import pytest

from scadbuddy.core.config import Config, load_config
from scadbuddy.library.scad import check_source, parse_diagnostics

BROKEN = "// a keychain\nsize = 10;\ncube([size, size, size)\n"
FINE = '/* [Main] */\n// Width\nwidth = 10; // [1:100]\nname = "hi";\ncube([width, 10, 2]);\n'
ASSERTS = 'width = 10;\nassert(false, "boom");\ncube([width, 1, 1]);\n'


def test_a_parser_error_carries_its_line() -> None:
    diagnostics = parse_diagnostics(
        [
            "ERROR: Parser error: syntax error in file model.scad, line 3",
            "Can't parse file 'model.scad'!",
        ]
    )
    assert [(d.severity, d.line, d.message) for d in diagnostics] == [
        ("error", 3, "Parser error: syntax error")
    ]


def test_warnings_and_traces_are_kept_with_their_lines() -> None:
    diagnostics = parse_diagnostics(
        [
            "WARNING: Can't find include file 'lib.scad'. in file model.scad, line 1",
            "TRACE: called by 'assert' in file model.scad, line 2",
            "Geometries in cache: 12",
        ]
    )
    assert [(d.severity, d.line) for d in diagnostics] == [("warning", 1), ("trace", 2)]
    assert diagnostics[0].message == "Can't find include file 'lib.scad'."
    assert diagnostics[0].file == "model.scad"


def test_a_message_without_a_location_still_parses() -> None:
    (diagnostic,) = parse_diagnostics(["ERROR: Parser error: syntax error"])
    assert (diagnostic.line, diagnostic.file) == (None, None)


async def test_without_a_binary_the_check_says_it_never_ran() -> None:
    result = await check_source(BROKEN, config=Config(openscad="scadbuddy-no-such-openscad"))
    assert (result.checked, result.ok, result.diagnostics) == (False, True, [])


@pytest.mark.requires_openscad
async def test_a_syntax_error_is_reported_against_its_line() -> None:
    result = await check_source(BROKEN, config=load_config())
    assert result.checked is True
    assert result.ok is False
    assert [(d.severity, d.line) for d in result.errors] == [("error", 3)]


@pytest.mark.requires_openscad
async def test_source_that_parses_passes() -> None:
    result = await check_source(FINE, config=load_config())
    assert (result.checked, result.ok) == (True, True)
    assert result.errors == []


@pytest.mark.requires_openscad
async def test_a_failed_assertion_fails_the_check_despite_exit_zero() -> None:
    """OpenSCAD exits 0 on a top-level assertion failure, so the ERROR line is the signal."""
    result = await check_source(ASSERTS, config=load_config())
    assert result.ok is False
    assert result.errors[0].line == 2
