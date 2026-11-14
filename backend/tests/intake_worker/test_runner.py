"""One check in a child process (#6133, spec 2026-11-13 §7): its verdict, its timeout, its
address-space cap, its hardening, and the hand-over to the pod's sandbox container."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.intake_worker import runner, sandbox
from scadbuddy.intake_worker.runner import CheckCrashedError, run_check
from scadbuddy.intake_worker.wire import MAX_PREVIEW_BYTES
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


def test_a_verdict_that_is_not_one_fails_the_check(tmp_path: Path) -> None:
    (tmp_path / runner.VERDICT).write_text("[1, 2]")
    with pytest.raises(CheckCrashedError, match="not one"):
        runner.read_verdict(tmp_path)


def test_a_preview_past_its_cap_fails_the_check(tmp_path: Path) -> None:
    (tmp_path / runner.VERDICT).write_text('{"outcome": "ready", "facts": {}}')
    (tmp_path / runner.PREVIEW).write_bytes(b"\0" * (MAX_PREVIEW_BYTES + 1))
    with pytest.raises(CheckCrashedError, match="preview past its cap"):
        runner.read_verdict(tmp_path)


def _hardened(code: str) -> str:
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_the_worker_makes_itself_non_dumpable() -> None:
    code = (
        "from scadbuddy.intake_worker import hardening as h;"
        " before = h.dumpable(); h.forbid_dumping(); print(before, h.dumpable())"
    )
    assert _hardened(code) == "True False"


def test_an_isolated_child_reaches_no_socket() -> None:
    """In its own network namespace the child reaches nothing, the host's loopback
    included. Where the kernel refuses the namespace, the test skips (the check goes on)."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        code = (
            "import socket; from scadbuddy.intake_worker import hardening as h\n"
            "refused = h.isolate()\n"
            "if refused:\n"
            "    print('refused', refused)\n"
            "else:\n"
            "    try:\n"
            f"        socket.create_connection(('127.0.0.1', {port}), timeout=2)\n"
            "        print('reached')\n"
            "    except OSError:\n"
            "        print('isolated')"
        )
        answer = _hardened(code)
    if answer.startswith("refused"):
        pytest.skip(answer)
    assert answer == "isolated"


def test_the_childs_verdict_says_whether_it_was_isolated(tmp_path: Path) -> None:
    file = tmp_path / "upload.bin"
    file.write_bytes(box_stl())
    asyncio.run(run_check(file, "box.stl", tmp_path))
    verdict = json.loads((tmp_path / runner.VERDICT).read_text())
    assert "isolation" in verdict


def _spooled(spool: Path, data: bytes, **kwargs: Any) -> runner.Verdict:
    """One check handed over ``spool`` to a sandbox served in this loop."""
    job = spool / "check-1"
    job.mkdir(mode=0o700)
    (job / runner.UPLOAD).write_bytes(data)

    async def go() -> runner.Verdict:
        stop = asyncio.Event()
        serving = asyncio.create_task(sandbox.serve(spool, 1, stop))
        try:
            return await run_check(job / runner.UPLOAD, "box.stl", job, sandbox=True, **kwargs)
        finally:
            stop.set()
            await serving

    return asyncio.run(go())


def test_the_sandbox_container_runs_a_spooled_check(tmp_path: Path) -> None:
    verdict = _spooled(tmp_path, box_stl())
    assert verdict.outcome == "ready" and verdict.facts is not None
    job = tmp_path / "check-1"
    assert (job / runner.TAKEN).exists() and (job / runner.DONE).read_text() == "0"
    assert job.stat().st_mode & 0o777 == runner.SPOOL_DIR_MODE


def test_a_spooled_check_past_its_timeout_is_killed(tmp_path: Path) -> None:
    with pytest.raises(CheckCrashedError, match="exited with timeout"):
        _spooled(tmp_path, box_stl(), timeout=0.01)


def test_a_spooled_check_no_sandbox_took_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "SPOOL_GRACE", 0.3)
    job = tmp_path / "check-1"
    job.mkdir()
    (job / runner.UPLOAD).write_bytes(box_stl())
    with pytest.raises(CheckCrashedError, match="no answer"):
        asyncio.run(run_check(job / runner.UPLOAD, "box.stl", job, sandbox=True, timeout=0.1))
    assert (job / runner.CANCEL).exists()


@pytest.mark.parametrize("name", sandbox.SANDBOX_FORBIDDEN_ENV)
def test_the_sandbox_refuses_a_secret_or_a_proof(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "x")
    assert sandbox.main([]) == 2
