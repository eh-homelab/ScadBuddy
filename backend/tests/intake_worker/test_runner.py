"""One check in a child process (#6133, spec 2026-11-13 §7): its verdict, its timeout, its
address-space cap, its hardening, what the worker reads back, and the sandbox's start.
The sandbox's RPC is `test_sandbox.py`."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.intake_worker import confine, runner, sandbox
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


@pytest.mark.parametrize(
    "verdict",
    [
        {"outcome": "ready"},
        {"outcome": "ready", "facts": {"format": "stl"}},
        {"outcome": "ready", "facts": [1]},
        {"outcome": "refused"},
    ],
)
def test_a_verdict_with_bad_facts_or_no_reason_fails_the_check(
    tmp_path: Path, verdict: dict[str, Any]
) -> None:
    (tmp_path / VERDICT).write_text(json.dumps(verdict))
    (tmp_path / PREVIEW).write_bytes(_png())
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
    with pytest.raises(CheckCrashedError, match="expected user's own"):
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
    ("filename", "timeout", "space", "request_id"),
    [
        ("", 1.0, 1, "ab" * 8),
        ("a/b.stl", 1.0, 1, "ab" * 8),
        ("a\x00.stl", 1.0, 1, "ab" * 8),
        ("x" * 256, 1.0, 1, "ab" * 8),
        ("box.stl", float("nan"), 1, "ab" * 8),
        ("box.stl", float("inf"), 1, "ab" * 8),
        ("box.stl", 0.0, 1, "ab" * 8),
        ("box.stl", runner.CHILD_TIMEOUT + 1, 1, "ab" * 8),
        ("box.stl", 1.0, 0, "ab" * 8),
        ("box.stl", 1.0, runner.CHILD_ADDRESS_SPACE + 1, "ab" * 8),
        ("box.stl", 1.0, 1, "ab" * 7 + "a"),
        ("box.stl", 1.0, 1, "ab" * 32 + "a"),
        ("box.stl", 1.0, 1, "AB" * 8),
        ("box.stl", 1.0, 1, "ab" * 8 + "\n"),
    ],
)
def test_a_request_out_of_bounds_is_never_handed_over(
    filename: str, timeout: float, space: int, request_id: str
) -> None:
    with pytest.raises(ValueError):
        runner.check_request(filename, timeout, space, request_id)


# -- the child's hardening ----------------------------------------------------------------


def _hardened(code: str, *args: str) -> str:
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, *args], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _confined(hide: Path, code: str, *args: str, cwd: Path | None = None) -> str:
    """``code`` run through the launcher with ``--strict``, as the sandbox runs a parse."""
    out = subprocess.run(
        confine.command(str(hide), [sys.executable, "-I", "-c", code, *args], strict=True),
        capture_output=True,
        text=True,
        timeout=60,
        cwd=cwd,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


#: Why a check cannot be confined on this machine (a container's default seccomp profile
#: refuses ``unshare(CLONE_NEWUSER)``), or None. Where it is refused, the sandbox refuses
#: to start, so the tests that need it skip, except where it is required.
_REFUSED = sandbox.probe_isolation(Path(tempfile.gettempdir()))
#: Set by CI's confinement step: a refusal there fails these tests rather than skipping
#: them, so a run always proves the sandbox.
_REQUIRED = os.environ.get("SCADBUDDY_REQUIRE_CONFINEMENT") == "1"


def needs_confinement(test: Callable[..., Any]) -> Callable[..., Any]:
    if _REFUSED is None:
        return test
    if _REQUIRED:

        @functools.wraps(test)
        def refused(*args: Any, **kwargs: Any) -> None:
            pytest.fail(f"confinement refused where it is required: {_REFUSED}")

        return refused
    skip: Callable[..., Any] = pytest.mark.skip(reason=f"confinement refused: {_REFUSED}")(test)
    return skip


def test_confinement_holds_where_it_is_required() -> None:
    if _REQUIRED:
        assert _REFUSED is None, _REFUSED


def test_the_worker_makes_itself_non_dumpable() -> None:
    code = (
        "from scadbuddy.intake_worker import hardening as h;"
        " before = h.dumpable(); h.forbid_dumping(); print(before, h.dumpable())"
    )
    assert _hardened(code) == "True False"


@needs_confinement
def test_a_confined_child_reaches_no_socket(tmp_path: Path) -> None:
    """In its own network namespace the child reaches nothing, the host's loopback
    included."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        code = (
            "import socket\n"
            "try:\n"
            f"    socket.create_connection(('127.0.0.1', {port}), timeout=2)\n"
            "    print('reached')\n"
            "except OSError:\n"
            "    print('isolated')"
        )
        assert _confined(tmp_path, code) == "isolated"


@needs_confinement
def test_a_confined_child_reaches_only_its_own_box(tmp_path: Path) -> None:
    """With the tmpfs over the spool, a child in one job's box sees no other job's
    directory, by path or by ``..``, and still reads its own box."""
    spool = tmp_path / "spool"
    mine, other = spool / "jobs" / "check-a" / "box", spool / "jobs" / "check-b"
    mine.mkdir(parents=True)
    other.mkdir()
    (mine / UPLOAD).write_text("mine")
    (other / "secret").write_text("another job's")
    code = (
        "import sys\n"
        "seen = [open('upload.bin').read()]\n"
        "for path in (sys.argv[1], '../../check-b/secret'):\n"
        "    try:\n"
        "        seen.append(open(path).read())\n"
        "    except OSError:\n"
        "        seen.append('hidden')\n"
        "print(','.join(seen))"
    )
    assert _confined(spool, code, str(other / "secret"), cwd=mine) == "mine,hidden,hidden"


_PROBE_HEAD = (
    "import ctypes, os, sys\n"
    "libc = ctypes.CDLL(None, use_errno=True)\n"
    "libc.mount.argtypes = [ctypes.c_char_p] * 3 + [ctypes.c_ulong, ctypes.c_char_p]\n"
    "X86 = os.uname().machine == 'x86_64'\n"
    "def errno(rc):\n"
    "    return 'ok' if rc == 0 else os.strerror(ctypes.get_errno())\n"
)


@needs_confinement
def test_a_confined_child_holds_no_capability_and_cannot_mount(tmp_path: Path) -> None:
    """The parser's exec as a uid that is not 0 in its user namespace clears every
    capability, so it cannot unmount the tmpfs that hides the spool (or mount, or make a
    namespace of its own); the seccomp filter refuses those calls besides. Read with
    ``capget`` and ``prctl``: the parser has no ``/proc``."""
    target = tmp_path / "target"
    target.mkdir()
    code = _PROBE_HEAD + (
        "head = (ctypes.c_uint32 * 2)(0x20080522, 0)\n"
        "data = (ctypes.c_uint32 * 6)()\n"
        "assert libc.capget(head, data) == 0\n"
        "print(list(data), libc.prctl(39, 0, 0, 0, 0), libc.prctl(21, 0, 0, 0, 0))\n"
        "print(errno(libc.umount2(sys.argv[1].encode(), 2)))\n"
        "print(errno(libc.mount(b'tmpfs', sys.argv[2].encode(), b'tmpfs', 0, None)))\n"
        "print(errno(libc.unshare(0x10000000)))\n"
        "print(errno(libc.syscall(56 if X86 else 220, 0x10000000 | 17, 0, 0, 0, 0)))"
    )
    lines = _confined(tmp_path, code, str(tmp_path), str(target)).splitlines()
    assert lines[0] == "[0, 0, 0, 0, 0, 0] 1 2"
    assert lines[1:] == ["Operation not permitted"] * 4


@needs_confinement
def test_a_confined_child_sees_no_other_process(tmp_path: Path) -> None:
    """PID 1 of its own PID namespace, with no ``/proc``; ``process_vm_readv``,
    ``process_vm_writev``, ``kcmp`` and the pidfd calls are refused even so."""
    code = _PROBE_HEAD + (
        "print(os.getpid())\n"
        "try:\n"
        "    print(os.listdir('/proc'))\n"
        "except OSError as error:\n"
        "    print(error.strerror)\n"
        "parent = os.getppid()\n"
        "calls = [(310 if X86 else 270, 1), (311 if X86 else 271, 1), (312 if X86 else 272, 1),"
        " (434, 1), (438, 1), (424, 1), (112 if X86 else 157, 0), (109 if X86 else 154, 0)]\n"
        "print([errno(libc.syscall(nr, arg, 0, 0, 0, 0, 0)) for nr, arg in calls])"
    )
    lines = _confined(tmp_path, code).splitlines()
    assert lines[0] == "1"
    assert lines[1] in ("[]", "Permission denied")
    # The six cross-process calls, then setsid and setpgid: the parser cannot leave the
    # process group a timeout kills.
    assert lines[2] == str(["Operation not permitted"] * 8)


def _state(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def _descendants(pid: int) -> list[int]:
    found: list[int] = []
    todo = [pid]
    while todo:
        parent = todo.pop()
        for children in Path(f"/proc/{parent}/task").glob("*/children"):
            with contextlib.suppress(OSError):
                for child in children.read_text().split():
                    found.append(int(child))
                    todo.append(int(child))
    return found


@needs_confinement
def test_killing_the_launcher_ends_every_process_of_the_job(tmp_path: Path) -> None:
    """The parser dies with its launcher (``PR_SET_PDEATHSIG``), and with PID 1 of its
    namespace gone the kernel kills the rest: no process of the job outlives it."""
    code = (
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    time.sleep(60)\n"
        "    raise SystemExit\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)"
    )
    launcher = subprocess.Popen(
        confine.command(str(tmp_path), [sys.executable, "-I", "-c", code], strict=True),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert launcher.stdout is not None
        assert launcher.stdout.readline().strip() == "ready"
        job = _descendants(launcher.pid)
        assert len(job) == 2  # the parser and its child
        launcher.kill()
        launcher.wait(timeout=10)
        deadline = time.monotonic() + 10
        while any(_state(pid) not in (None, "Z", "X") for pid in job):
            assert time.monotonic() < deadline, [(pid, _state(pid)) for pid in job]
            time.sleep(0.05)
    finally:
        launcher.kill()
        launcher.wait(timeout=10)


@needs_confinement
def test_the_sandbox_kills_a_job_process_left_alive_before_it_claims(tmp_path: Path) -> None:
    launcher = subprocess.Popen(
        confine.command(
            str(tmp_path), [sys.executable, "-I", "-c", "import time; time.sleep(60)"], strict=True
        )
    )
    try:
        deadline = time.monotonic() + 10
        while len(found := sandbox.job_processes()) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        # The launcher (non-dumpable, so unreadable) and the parser (another PID namespace).
        assert sorted(found) == sorted([launcher.pid, *_descendants(launcher.pid)])
        asyncio.run(sandbox.clear_job_processes())
        assert sandbox.job_processes() == []
        assert all(_state(pid) in (None, "Z", "X") for pid in found)
    finally:
        launcher.kill()
        launcher.wait(timeout=10)


def test_the_sandbox_stops_when_a_job_process_will_not_die(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "JOB_EXIT_GRACE", 0.2)
    monkeypatch.setattr(sandbox, "job_processes", lambda: [2**22 + 12345])
    with pytest.raises(sandbox.JobOutlivedError):
        asyncio.run(sandbox.clear_job_processes())


@needs_confinement
def test_the_launcher_answers_the_parsers_exit(tmp_path: Path) -> None:
    out = subprocess.run(
        confine.command(
            str(tmp_path), [sys.executable, "-I", "-c", "raise SystemExit(7)"], strict=True
        ),
        timeout=60,
    )
    assert out.returncode == 7


def test_the_launcher_refuses_root_with_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(SystemExit) as stopped:
        confine.main(["--strict", "/nowhere", "--", "/bin/true"])
    assert stopped.value.code == 126


def test_the_childs_verdict_says_whether_it_was_isolated(tmp_path: Path) -> None:
    job = _job(tmp_path, box_stl())
    asyncio.run(run_check(job / UPLOAD, "box.stl", job))
    verdict = json.loads((job / VERDICT).read_text())
    assert "isolation" in verdict


# -- the sandbox's start (its RPC: test_sandbox.py) ---------------------------------------


def test_the_sandbox_refuses_to_start_when_a_check_cannot_be_confined(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """It fails closed: no check is served by a sandbox whose checks would run unconfined."""
    for name in sandbox.SANDBOX_FORBIDDEN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sandbox, "probe_isolation", lambda spool: "unshare: refused")
    monkeypatch.setattr(sandbox, "forbid_dumping", lambda: None)
    served: list[object] = []
    monkeypatch.setattr(sandbox, "_main", lambda *args: served.append(args))
    assert sandbox.main([]) == 3
    assert served == []
    assert "refuses to start" in capsys.readouterr().out


@pytest.mark.parametrize("name", sandbox.SANDBOX_FORBIDDEN_ENV)
def test_the_sandbox_refuses_a_secret_or_a_proof(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "x")
    assert sandbox.main([]) == 2
