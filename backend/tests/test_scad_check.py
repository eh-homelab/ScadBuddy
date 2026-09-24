from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from scadbuddy.api.deps import build_state
from scadbuddy.core.config import Config, load_config
from scadbuddy.core.settings import Settings
from scadbuddy.library import scad
from scadbuddy.library.scad import check_source, parse_diagnostics
from scadbuddy.render.runner import ProcessOutput

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
async def test_source_that_parses_passes_and_reports_its_parameters() -> None:
    result = await check_source(FINE, config=load_config())
    assert (result.checked, result.ok) == (True, True)
    assert result.errors == []
    # `width` and `name`; the check derives the schema rather than only parsing.
    assert result.parameters == 2


@pytest.mark.requires_openscad
async def test_a_failed_assertion_fails_the_check_despite_exit_zero() -> None:
    """OpenSCAD exits 0 on a top-level assertion failure, so the ERROR line is the signal."""
    result = await check_source(ASSERTS, config=load_config())
    assert result.ok is False
    assert result.errors[0].line == 2


@pytest.mark.requires_openscad
async def test_a_failed_check_reports_no_parameter_count() -> None:
    result = await check_source(BROKEN, config=load_config())
    assert result.parameters is None


async def test_the_check_runs_no_more_openscads_at_once_than_its_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An editor checking on every keystroke pause must not outrun the render cap."""
    live = 0
    peak = 0

    async def fake_run(args: Sequence[str], *, cwd: Path, config: Config) -> ProcessOutput:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        Path(args[1]).write_text(json.dumps({"parameters": []}), encoding="utf-8")
        live -= 1
        return ProcessOutput(returncode=0, log_tail=[], duration_s=0.01)

    monkeypatch.setattr(scad, "run_openscad", fake_run)
    monkeypatch.setattr("scadbuddy.library.scad.shutil.which", lambda _: "/usr/bin/openscad")

    limit = asyncio.Semaphore(2)
    config = Config(openscad="openscad")
    await asyncio.gather(*(check_source(FINE, config=config, limit=limit) for _ in range(6)))

    assert peak == 2


def test_the_checks_cap_is_the_render_concurrency() -> None:
    """The cap the routes hand to the check comes from the same knob renders obey."""
    state = build_state(Settings(render_concurrency=3, frontend_dir=Path("/nonexistent")))
    assert state.checks._value == 3
