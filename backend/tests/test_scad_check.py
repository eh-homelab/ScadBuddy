from __future__ import annotations

import asyncio
import json
import shutil
import time
from collections.abc import Sequence
from os import PathLike
from pathlib import Path

import pytest

from scadbuddy.api.deps import build_state
from scadbuddy.core.config import Config, load_config
from scadbuddy.core.settings import Settings
from scadbuddy.library import scad
from scadbuddy.library.scad import check_source, parse_diagnostics
from scadbuddy.render.runner import ProcessOutput, RenderTimeoutError

StrPath = str | PathLike[str]

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


async def test_the_checks_cap_is_its_own_knob() -> None:
    """Not the render one: the queue caps itself with worker tasks, so there is no
    semaphore to share, and the pod's budget is the two added together."""
    state = build_state(Settings(check_concurrency=3, frontend_dir=Path("/nonexistent")))

    for _ in range(3):
        await asyncio.wait_for(state.checks.acquire(), timeout=0.1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(state.checks.acquire(), timeout=0.05)


async def test_an_unusable_param_export_is_a_diagnostic_and_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`build_schema` subscripts the export directly, so a missing key is a KeyError."""

    async def fake_run(args: Sequence[str], *, cwd: Path, config: Config) -> ProcessOutput:
        Path(args[1]).write_text(
            json.dumps({"parameters": [{"type": "number", "initial": 1}]}), encoding="utf-8"
        )
        return ProcessOutput(returncode=0, log_tail=[], duration_s=0.01)

    monkeypatch.setattr(scad, "run_openscad", fake_run)
    monkeypatch.setattr("scadbuddy.library.scad.shutil.which", lambda _: "/usr/bin/openscad")

    result = await check_source(FINE, config=Config(openscad="openscad"))
    assert result.ok is False
    assert "could not be derived" in result.errors[0].message
    assert result.parameters is None


async def test_a_timeout_says_so_instead_of_blaming_the_syntax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(args: Sequence[str], *, cwd: Path, config: Config) -> ProcessOutput:
        raise RenderTimeoutError("openscad timed out after 120s", ["Compiling design..."])

    monkeypatch.setattr(scad, "run_openscad", fake_run)
    monkeypatch.setattr("scadbuddy.library.scad.shutil.which", lambda _: "/usr/bin/openscad")

    result = await check_source(FINE, config=Config(openscad="openscad", render_timeout=120))
    assert (result.ok, result.timed_out) == (False, True)
    assert "timed out" in result.errors[0].message


@pytest.mark.requires_openscad
async def test_a_sibling_include_resolves_when_the_model_directory_comes_along(
    tmp_path: Path,
) -> None:
    """Checked in its own directory, an edit to a model that includes a sibling file
    reads clean; checked in an empty one, OpenSCAD warns about a file that is there."""
    model_dir = tmp_path / "widget"
    model_dir.mkdir()
    (model_dir / "helper.scad").write_text("helper_depth = 4;\n", encoding="utf-8")
    source = (
        'include <helper.scad>\n/* [Main] */\n// Label\nlabel = "hi";\n'
        "cube([10, 2, helper_depth]);\n"
    )

    with_context = await check_source(source, config=load_config(), context=model_dir)
    assert (with_context.ok, with_context.diagnostics) == (True, [])

    without_context = await check_source(source, config=load_config())
    assert any("include" in d.message for d in without_context.diagnostics)

    # Measured against OpenSCAD 2026.09.23: the .param export carries only the MAIN
    # file's literal-initialised top-level variables, so the derived schema is the same
    # either way — the include changes the diagnostics, never the parameter set.
    assert with_context.parameters == without_context.parameters == 1


@pytest.mark.requires_openscad
async def test_the_model_directory_sidecars_are_left_behind(tmp_path: Path) -> None:
    """Copying a thumbnail into every check would be pure waste."""
    model_dir = tmp_path / "widget"
    model_dir.mkdir()
    (model_dir / "thumbnail.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 4096)
    (model_dir / "helper.scad").write_text("helper_depth = 4;\n", encoding="utf-8")

    staged = tmp_path / "sandbox"
    staged.mkdir()
    scad._stage("cube(1);\n", staged, model_dir)

    assert sorted(path.name for path in staged.iterdir()) == ["helper.scad", "model.scad"]


async def test_staging_the_check_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model directory is copied per check, and a check runs on every keystroke
    pause: doing that copy on the loop stalls every other request in the pod for its
    duration — renders, the health probe, the other users' checks."""
    context = tmp_path / "model"
    context.mkdir()
    (context / "helper.scad").write_text("helper = 1;\n", encoding="utf-8")

    real_copy = shutil.copy2

    def slow_copy(src: StrPath, dst: StrPath) -> StrPath:
        time.sleep(0.3)
        return real_copy(src, dst)

    async def fake_run(args: Sequence[str], *, cwd: Path, config: Config) -> ProcessOutput:
        Path(args[1]).write_text(json.dumps({"parameters": []}), encoding="utf-8")
        return ProcessOutput(returncode=0, log_tail=[], duration_s=0.0)

    monkeypatch.setattr("scadbuddy.library.scad.shutil.copy2", slow_copy)
    monkeypatch.setattr(scad, "run_openscad", fake_run)
    monkeypatch.setattr("scadbuddy.library.scad.shutil.which", lambda _: "/usr/bin/openscad")

    gaps: list[float] = []

    async def heartbeat() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(heartbeat())
    try:
        await check_source(FINE, config=Config(openscad="openscad"), context=context)
    finally:
        ticker.cancel()

    # The copy takes 300ms; anything close to that in one gap means the loop sat idle
    # through it. A generous ceiling keeps this about blocking, not about scheduler jitter.
    assert gaps, "the heartbeat never ran at all"
    assert max(gaps) < 0.15
