"""One check in a child process (#6133, spec 2026-11-13 §7): its verdict, its timeout and
its address-space cap."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scadbuddy.intake_worker import runner
from scadbuddy.intake_worker.runner import CheckCrashedError, run_check
from tests.intake_worker.test_check import box_stl


def test_a_ready_file_comes_back_with_its_facts_and_preview(tmp_path: Path) -> None:
    file = tmp_path / "upload.bin"
    file.write_bytes(box_stl())
    verdict = asyncio.run(run_check(file, "box.stl", tmp_path))
    assert verdict.outcome == "ready"
    assert verdict.facts is not None and verdict.facts.triangles == 12
    assert verdict.preview is not None and verdict.preview.read_bytes().startswith(b"\x89PNG")


def test_a_refused_file_comes_back_with_its_reason(tmp_path: Path) -> None:
    file = tmp_path / "upload.bin"
    file.write_bytes(b"not a model at all")
    verdict = asyncio.run(run_check(file, "box.stl", tmp_path))
    assert (verdict.outcome, verdict.preview) == ("refused", None)
    assert verdict.reason == "The file is not a 3MF or an STL."


def test_a_check_past_its_timeout_is_killed(tmp_path: Path) -> None:
    file = tmp_path / "upload.bin"
    file.write_bytes(box_stl())
    with pytest.raises(CheckCrashedError, match="timed out"):
        asyncio.run(run_check(file, "box.stl", tmp_path, timeout=0.01))


def test_a_check_past_its_address_space_fails_its_child(tmp_path: Path) -> None:
    """The cap is set before numpy loads: a child capped at 64 MiB cannot even start the
    read, so the check fails rather than the worker."""
    file = tmp_path / "upload.bin"
    file.write_bytes(box_stl())
    with pytest.raises(CheckCrashedError, match="exited with"):
        asyncio.run(run_check(file, "box.stl", tmp_path, address_space=64 << 20))


def test_the_child_gets_none_of_the_workers_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCADBUDDY_SERVICE_TOKEN_FILE", "/var/run/secrets/token")
    monkeypatch.setenv("SCADBUDDY_API_INTERNAL_URL", "http://scadbuddy:8080")
    env = runner.child_env(tmp_path)
    assert not [name for name in env if name.startswith("SCADBUDDY_")]
    assert env["MPLCONFIGDIR"] == str(tmp_path)
