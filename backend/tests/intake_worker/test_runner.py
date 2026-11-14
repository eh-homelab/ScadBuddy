"""One check in a child process (#6133, spec 2026-11-13 §7): its verdict, its timeout, its
address-space cap, its hardening, what the worker reads back, and the hand-over to the
pod's sandbox container."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.intake_worker import runner, sandbox
from scadbuddy.intake_worker.child import PREVIEW, UPLOAD, VERDICT
from scadbuddy.intake_worker.runner import CheckCrashedError, run_check
from scadbuddy.intake_worker.wire import MAX_PREVIEW_BYTES
from tests.intake_worker.test_check import box_stl

OWN = os.geteuid()


def _job(tmp_path: Path, data: bytes, name: str = "check-1") -> Path:
    """A check's scratch folder in a work directory of its own, holding ``data``."""
    job = tmp_path / "work" / name
    job.mkdir(parents=True, mode=0o700)
    (job / UPLOAD).write_bytes(data)
    return job


def _png(width: int = 4, height: int = 4) -> bytes:
    head = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        runner.PNG_MAGIC
        + struct.pack(">I", 13)
        + head
        + struct.pack(">I", zlib.crc32(head))
        + b"\x00" * 16
    )


def _read(folder: Path, name: str, cap: int = 1 << 20, owner: int | None = OWN) -> bytes:
    fd = runner.open_dir(folder)
    try:
        return runner.read_output(fd, name, cap, owner)
    finally:
        os.close(fd)


def test_a_ready_file_comes_back_with_its_facts_and_preview(tmp_path: Path) -> None:
    job = _job(tmp_path, box_stl())
    verdict = asyncio.run(run_check(job / UPLOAD, "box.stl", job))
    assert verdict.outcome == "ready"
    assert verdict.facts is not None and verdict.facts.triangles == 12
    assert verdict.preview is not None and verdict.preview.startswith(runner.PNG_MAGIC)


def test_a_refused_file_comes_back_with_its_reason(tmp_path: Path) -> None:
    job = _job(tmp_path, b"not a model at all")
    verdict = asyncio.run(run_check(job / UPLOAD, "box.stl", job))
    assert (verdict.outcome, verdict.preview) == ("refused", None)
    assert verdict.reason == "The file is not a 3MF or an STL."


def test_a_check_past_its_timeout_is_killed(tmp_path: Path) -> None:
    job = _job(tmp_path, box_stl())
    with pytest.raises(CheckCrashedError, match="timed out"):
        asyncio.run(run_check(job / UPLOAD, "box.stl", job, timeout=0.01))


def test_a_check_past_its_address_space_fails_its_child(tmp_path: Path) -> None:
    """The cap is set before numpy loads: a child capped at 64 MiB cannot even start the
    read, so the check fails rather than the worker."""
    job = _job(tmp_path, box_stl())
    with pytest.raises(CheckCrashedError, match="exited with"):
        asyncio.run(run_check(job / UPLOAD, "box.stl", job, address_space=64 << 20))


def test_the_child_gets_none_of_the_workers_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCADBUDDY_SERVICE_TOKEN_FILE", "/var/run/secrets/token")
    monkeypatch.setenv("SCADBUDDY_API_INTERNAL_URL", "http://scadbuddy:8080")
    env = runner.child_env(tmp_path)
    assert not [name for name in env if name.startswith("SCADBUDDY_")]
    assert env["MPLCONFIGDIR"] == str(tmp_path)


# -- what the worker reads back ---------------------------------------------------------


def test_a_verdict_that_is_not_one_fails_the_check(tmp_path: Path) -> None:
    (tmp_path / VERDICT).write_text("[1, 2]")
    fd = runner.open_dir(tmp_path)
    try:
        with pytest.raises(CheckCrashedError, match="not one"):
            runner.read_verdict(fd, OWN)
    finally:
        os.close(fd)


def test_an_output_past_its_cap_fails_the_check(tmp_path: Path) -> None:
    (tmp_path / PREVIEW).write_bytes(b"\0" * (MAX_PREVIEW_BYTES + 1))
    with pytest.raises(CheckCrashedError, match="past its cap"):
        _read(tmp_path, PREVIEW, MAX_PREVIEW_BYTES)


def test_a_symlinked_output_is_never_followed(tmp_path: Path) -> None:
    """A hostile parse cannot make the worker read (and upload) one of its own files,
    such as its proof, by leaving a symlink where the preview goes."""
    secret = tmp_path / "token"
    secret.write_text("the worker's proof")
    out = tmp_path / "out"
    out.mkdir()
    (out / PREVIEW).symlink_to(secret)
    with pytest.raises(CheckCrashedError, match="not a plain file"):
        _read(out, PREVIEW)


def test_a_hardlinked_output_is_refused(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("the worker's proof")
    out = tmp_path / "out"
    out.mkdir()
    os.link(secret, out / PREVIEW)
    with pytest.raises(CheckCrashedError, match="one link"):
        _read(out, PREVIEW)


def test_an_output_of_another_owner_is_refused(tmp_path: Path) -> None:
    (tmp_path / PREVIEW).write_bytes(_png())
    with pytest.raises(CheckCrashedError, match="sandbox's own"):
        _read(tmp_path, PREVIEW, owner=OWN + 1)


def test_an_output_that_is_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / PREVIEW)
    with pytest.raises(CheckCrashedError, match="not a plain file"):
        _read(tmp_path, PREVIEW)


@pytest.mark.parametrize(
    "data",
    [
        b"GIF89a" + b"\0" * 64,
        runner.PNG_MAGIC + b"\0" * 64,
        _png()[:20],
        _png(width=0),
        _png(width=runner.MAX_PREVIEW_SIDE + 1),
        _png()[:29] + b"\0\0\0\0" + _png()[33:],  # a CRC that does not match
    ],
)
def test_a_preview_that_is_not_a_png_is_refused(data: bytes) -> None:
    with pytest.raises(CheckCrashedError, match="not a PNG"):
        runner.check_png(data)
    runner.check_png(_png())


@pytest.mark.parametrize(
    ("filename", "timeout", "space"),
    [
        ("", 1.0, 1),
        ("a/b.stl", 1.0, 1),
        ("a\x00.stl", 1.0, 1),
        ("x" * 256, 1.0, 1),
        ("box.stl", float("nan"), 1),
        ("box.stl", float("inf"), 1),
        ("box.stl", 0.0, 1),
        ("box.stl", runner.CHILD_TIMEOUT + 1, 1),
        ("box.stl", 1.0, 0),
        ("box.stl", 1.0, runner.CHILD_ADDRESS_SPACE + 1),
    ],
)
def test_a_request_out_of_bounds_is_never_handed_over(
    filename: str, timeout: float, space: int
) -> None:
    with pytest.raises(ValueError):
        runner.check_request(filename, timeout, space)


# -- the child's hardening ----------------------------------------------------------------


def _hardened(code: str, *args: str) -> str:
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, *args], capture_output=True, text=True, timeout=60
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


def test_an_isolated_child_reaches_only_its_own_box(tmp_path: Path) -> None:
    """With the tmpfs over the spool, a child in one job's box sees no other job's
    directory, by path or by ``..``, and still reads its own box."""
    spool = tmp_path / "spool"
    mine, other = spool / "check-a" / "box", spool / "check-b"
    mine.mkdir(parents=True)
    other.mkdir()
    (mine / UPLOAD).write_text("mine")
    (other / "secret").write_text("another job's")
    code = (
        "import os, sys; from scadbuddy.intake_worker import hardening as h\n"
        "os.chdir(sys.argv[1])\n"
        "refused = h.isolate(sys.argv[2])\n"
        "if refused:\n"
        "    print('refused', refused); raise SystemExit\n"
        "seen = [open('upload.bin').read()]\n"
        "for path in (sys.argv[3], '../../check-b/secret'):\n"
        "    try:\n"
        "        seen.append(open(path).read())\n"
        "    except OSError:\n"
        "        seen.append('hidden')\n"
        "print(','.join(seen))"
    )
    answer = _hardened(code, str(mine), str(spool), str(other / "secret"))
    if answer.startswith("refused"):
        pytest.skip(answer)
    assert answer == "mine,hidden,hidden"


def test_the_childs_verdict_says_whether_it_was_isolated(tmp_path: Path) -> None:
    job = _job(tmp_path, box_stl())
    asyncio.run(run_check(job / UPLOAD, "box.stl", job))
    verdict = json.loads((job / VERDICT).read_text())
    assert "isolation" in verdict


# -- the sandbox container ---------------------------------------------------------------


def _spooled(job: Path, **kwargs: Any) -> runner.Verdict:
    """One check handed over its spool to a sandbox served in this loop."""
    spool = job.parent

    async def go() -> runner.Verdict:
        stop = asyncio.Event()
        serving = asyncio.create_task(sandbox.serve(spool, 1, stop))
        try:
            return await run_check(job / UPLOAD, "box.stl", job, sandbox_uid=OWN, **kwargs)
        finally:
            stop.set()
            await serving

    return asyncio.run(go())


def test_the_sandbox_container_runs_a_spooled_check(tmp_path: Path) -> None:
    job = _job(tmp_path, box_stl())
    verdict = _spooled(job)
    assert verdict.outcome == "ready" and verdict.facts is not None
    assert verdict.preview is not None and verdict.preview.startswith(runner.PNG_MAGIC)
    assert (job / runner.TAKEN).exists() and (job / runner.DONE).read_text() == "0"
    assert job.stat().st_mode & 0o777 == runner.SPOOL_DIR_MODE
    # The sandbox's box is gone; what came back is in the worker's own out/.
    assert not (job / runner.BOX).exists()
    assert sorted(p.name for p in (job / runner.OUT).iterdir()) == [PREVIEW, VERDICT]


def test_a_spooled_out_dir_swapped_for_a_symlink_is_refused(tmp_path: Path) -> None:
    """The worker reads ``out/`` through a descriptor it opened when it made it; a
    directory swapped in later by name is never the one it reads."""
    job = _job(tmp_path, box_stl())

    async def go() -> runner.Verdict:
        stop = asyncio.Event()

        async def swap_then_serve() -> None:
            while not (job / runner.READY).exists():
                await asyncio.sleep(0.05)
            decoy = tmp_path / "decoy"
            decoy.mkdir()
            (decoy / VERDICT).write_text('{"outcome": "refused", "reason": "decoy"}')
            (job / runner.OUT).rename(tmp_path / "moved")
            (job / runner.OUT).symlink_to(decoy)
            await sandbox.serve(job.parent, 1, stop)

        serving = asyncio.create_task(swap_then_serve())
        try:
            return await run_check(job / UPLOAD, "box.stl", job, sandbox_uid=OWN)
        finally:
            stop.set()
            await serving

    # The sandbox finds no out/ of its own to fill (it never follows the symlink), and
    # the worker's descriptor still names the moved directory, which holds no verdict.
    with pytest.raises(CheckCrashedError):
        asyncio.run(go())


def test_a_spooled_check_past_its_timeout_is_killed(tmp_path: Path) -> None:
    with pytest.raises(CheckCrashedError, match="exited with timeout"):
        _spooled(_job(tmp_path, box_stl()), timeout=0.01)


def test_a_spooled_check_no_sandbox_took_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "SPOOL_GRACE", 0.3)
    job = _job(tmp_path, box_stl())
    with pytest.raises(CheckCrashedError, match="no answer"):
        asyncio.run(run_check(job / UPLOAD, "box.stl", job, sandbox_uid=OWN, timeout=0.1))
    assert (job / runner.CANCEL).exists()


def test_a_spooled_answer_of_another_owner_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CheckCrashedError, match="sandbox's own"):
        job = _job(tmp_path, box_stl())
        spool = job.parent

        async def go() -> runner.Verdict:
            stop = asyncio.Event()
            serving = asyncio.create_task(sandbox.serve(spool, 1, stop))
            try:
                return await run_check(job / UPLOAD, "box.stl", job, sandbox_uid=OWN + 1)
            finally:
                stop.set()
                await serving

        asyncio.run(go())


def test_the_sandbox_answers_a_bad_request_without_running_it(tmp_path: Path) -> None:
    job = _job(tmp_path, box_stl())
    (job / runner.REQUEST).write_text(
        json.dumps({"filename": "box.stl", "timeout": "Infinity", "address_space": 1})
    )
    (job / runner.OUT).mkdir()
    (job / runner.READY).touch()

    async def go() -> None:
        stop = asyncio.Event()
        serving = asyncio.create_task(sandbox.serve(job.parent, 1, stop))
        while not (job / runner.DONE).exists():
            await asyncio.sleep(0.05)
        stop.set()
        await serving

    asyncio.run(go())
    assert (job / runner.DONE).read_text() == "bad-request"
    assert not (job / runner.BOX).exists()


@pytest.mark.parametrize("name", sandbox.SANDBOX_FORBIDDEN_ENV)
def test_the_sandbox_refuses_a_secret_or_a_proof(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "x")
    assert sandbox.main([]) == 2
