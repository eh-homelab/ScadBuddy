"""A model's source cannot read or enumerate the server's filesystem (#994)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scadbuddy.core.config import Config, load_config
from scadbuddy.library.scad import check_source
from scadbuddy.render import confinement, sandbox
from scadbuddy.render.confinement import escaping_includes
from scadbuddy.render.runner import run_openscad

landlock = pytest.mark.skipif(sandbox.abi_version() < 1, reason="the kernel has no Landlock")


def test_absolute_and_climbing_targets_are_found(tmp_path: Path) -> None:
    (tmp_path / "model.scad").write_text(
        "include </etc/passwd>\n"
        "use <../other-model/model.scad>\n"
        "include <BOSL2/../../../etc/passwd>\n"
        "include <BOSL2/std.scad>\n"
        "use <helper.scad>\n"
        "cube(1);\n",
        encoding="utf-8",
    )
    found = escaping_includes(tmp_path, "model.scad")
    assert [(f.kind, f.target, f.line) for f in found] == [
        ("include", "/etc/passwd", 1),
        ("use", "../other-model/model.scad", 2),
        ("include", "BOSL2/../../../etc/passwd", 3),
    ]


def test_the_models_own_includes_are_followed(tmp_path: Path) -> None:
    (tmp_path / "parts").mkdir()
    (tmp_path / "model.scad").write_text("include <parts/a.scad>\n", encoding="utf-8")
    (tmp_path / "parts" / "a.scad").write_text("\n\nuse </proc/self/environ>\n", encoding="utf-8")
    (found,) = escaping_includes(tmp_path, "model.scad")
    assert (found.file, found.line, found.target) == ("parts/a.scad", 3, "/proc/self/environ")


def test_a_statement_in_a_comment_is_refused_too(tmp_path: Path) -> None:
    """A superset of OpenSCAD's lexer: no disagreement with it can let one through."""
    (tmp_path / "model.scad").write_text("/* include </etc/passwd> */\n", encoding="utf-8")
    assert [f.target for f in escaping_includes(tmp_path, "model.scad")] == ["/etc/passwd"]


def test_a_link_out_of_the_model_is_not_read_by_the_backend(tmp_path: Path) -> None:
    outside = tmp_path / "outside.scad"
    outside.write_text("include </etc/passwd>\n", encoding="utf-8")
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.scad").write_text("include <leak.scad>\n", encoding="utf-8")
    (model / "leak.scad").symlink_to(outside)
    assert escaping_includes(model, "model.scad") == []


@pytest.mark.parametrize("target", ["/etc/passwd", "/etc/shadow", "/nonexistent-xyz"])
async def test_the_check_refuses_without_running_openscad_or_looking(
    tmp_path: Path, target: str
) -> None:
    """The same answer whether the file exists, is readable or neither: no oracle."""
    ran = tmp_path / "ran"
    binary = tmp_path / "openscad"
    binary.write_text(f"#!/bin/sh\ntouch {ran}\n", encoding="utf-8")
    binary.chmod(0o755)
    result = await check_source(
        f"include <{target}>\ncube(1);\n", config=Config(openscad=str(binary))
    )
    assert (result.checked, result.ok) == (True, False)
    (error,) = result.errors
    assert error.line == 1
    assert error.message == (
        f"include <{target}> is outside the model's directory and its libraries; "
        "name a file in either without '..' or a leading '/'"
    )
    assert not ran.exists()


def _confined(argv: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    command = sandbox.command(argv, read=[*confinement.SYSTEM_READ, str(tmp_path)], write=[])
    return subprocess.run(command, capture_output=True, text=True, check=False)


@landlock
def test_the_sandbox_refuses_a_read_outside_it(tmp_path: Path) -> None:
    (tmp_path / "mine.txt").write_text("mine", encoding="utf-8")
    assert _confined(["/bin/cat", str(tmp_path / "mine.txt")], tmp_path).stdout == "mine"
    for path in ("/etc/passwd", "/proc/self/environ"):
        result = _confined(["/bin/cat", path], tmp_path)
        assert result.returncode != 0
        assert "Permission denied" in result.stderr


@landlock
def test_the_sandbox_refuses_to_list_a_directory_outside_it(tmp_path: Path) -> None:
    result = _confined(["/bin/ls", "/etc"], tmp_path)
    assert result.returncode != 0
    assert "Permission denied" in result.stderr


@landlock
def test_a_link_out_of_the_sandbox_leads_nowhere(tmp_path: Path) -> None:
    (tmp_path / "leak").symlink_to("/etc/passwd")
    result = _confined(["/bin/cat", str(tmp_path / "leak")], tmp_path)
    assert result.returncode != 0
    assert "Permission denied" in result.stderr


@landlock
@pytest.mark.requires_openscad
async def test_a_link_in_the_models_directory_does_not_reach_the_file(tmp_path: Path) -> None:
    """Without the sandbox OpenSCAD reads /etc/passwd through the link and reports a
    parser error on it; with it, the file cannot be opened."""
    context = tmp_path / "model"
    context.mkdir()
    (context / "leak.scad").symlink_to("/etc/passwd")
    result = await check_source(
        "include <leak.scad>\ncube(1);\n", config=load_config(), context=context
    )
    messages = [d.message for d in result.diagnostics]
    assert not any("Parser error" in m for m in messages), messages
    assert any("Can't open include file" in m for m in messages), messages


@landlock
@pytest.mark.requires_openscad
async def test_a_computed_path_does_not_reach_the_file(tmp_path: Path) -> None:
    """``surface()`` takes an expression, which no check before the run can read.
    Unconfined, OpenSCAD reads the file and says "Illegal value in '/etc/passwd'"."""
    (tmp_path / "model.scad").write_text(
        'surface(file = str("/etc/", "passwd"));\ncube(1);\n', encoding="utf-8"
    )
    output = await run_openscad(
        ["-o", str(tmp_path / "out.stl"), "model.scad"], cwd=tmp_path, config=load_config()
    )
    messages = [d.message for d in output.diagnostics]
    assert not any("Illegal value" in m for m in messages), messages
    assert any("couldn't be opened" in m for m in messages), messages


@landlock
@pytest.mark.requires_openscad
async def test_a_model_still_reads_its_own_files_under_the_sandbox(tmp_path: Path) -> None:
    context = tmp_path / "model"
    context.mkdir()
    (context / "helper.scad").write_text("width = 7;\n", encoding="utf-8")
    result = await check_source(
        'include <helper.scad>\nassert(width == 7, "helper not read");\ncube(width);\n',
        config=load_config(),
        context=context,
    )
    assert result.ok, result.log_tail
