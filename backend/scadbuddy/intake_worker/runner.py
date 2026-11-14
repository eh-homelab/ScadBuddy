"""Runs one check in a child process (`child.py`) started through its launcher
(`confine.py`: its own namespaces with the spool hidden, no capabilities, no new privileges,
a seccomp filter): its own process group, an address-space cap it sets on itself, and a
timeout, past which (or on cancellation) the whole group is killed, as
`workflows/template_process.py` runs template Python (spec 2026-11-13 §7).

Two ways to reach the child:

- the sandbox container (``SCADBUDDY_INTAKE_SANDBOX=true``, the deployment's): the worker
  only hands the job over a spool directory it shares with a second container of the pod
  (`sandbox.py`), which runs as another user with no service-account token, no proof and a
  read-only root, and starts the child there with ``--strict``: a launcher step refused is
  a failed check, never an unconfined parse. The worker never runs the parse.
- local (``SCADBUDDY_INTAKE_SANDBOX=unsafe-local``, development and tests only): the worker
  starts the child itself, in the job's directory, and a launcher step refused is reported
  in the verdict's ``isolation``.

The spool protocol. The worker builds the job inside the check's scratch folder, which is
its own and mode 0700, so no other user reaches it: a ``job/`` directory holding
``upload.bin``, ``request.json`` (checked against `check_request`) and an empty ``out/``.
Then it hands the job over with one ``rename`` into the spool's ``jobs/`` (mode 0750, the
worker's own: the sandbox can list it but not add to it), so the sandbox sees a job whole
or not at all. It holds descriptors of the job's directory and of ``out/`` from before the
hand-over and reaches every name through them, never by path. The sandbox claims the job by
making ``taken`` (``O_EXCL``), copies the upload into a ``box/`` of its own (mode 0700),
runs the child there, copies the child's ``verdict.json`` and ``preview.png`` into ``out/``
as files it makes itself, removes its box and writes ``done`` (the child's exit, or
``timeout``). The worker removes the job once it has read the answer.
The worker waits for ``done`` up to the child's timeout plus `SPOOL_GRACE`; past it, or on
cancellation, it writes ``cancel``, which the sandbox answers by killing the child's group.

Whatever comes back is read as untrusted (`read_output`): opened with ``O_NOFOLLOW`` through
the descriptor of a directory the reader made, so a symlink is refused; ``fstat`` must show a
regular file with one link (no hardlink to another file), owned by the user that wrote it
(the sandbox's uid, ``SCADBUDDY_INTAKE_SANDBOX_UID``, or this process's own in local mode),
and at most its cap; the verdict is then validated, and the preview must be a PNG with a
valid ``IHDR`` (`check_png`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import signal
import stat
import struct
import sys
import zlib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from prometheus_client import Counter
from pydantic import ValidationError

from scadbuddy.intake_worker import confine
from scadbuddy.intake_worker.child import PREVIEW, UPLOAD, VERDICT
from scadbuddy.intake_worker.wire import MAX_PREVIEW_BYTES, FileFacts

logger = logging.getLogger(__name__)

#: The child's address space: the largest measured fixture's model inflates to 470 MiB
#: (spec §7.1), and the read keeps its meshes besides.
CHILD_ADDRESS_SPACE: Final = 4 << 30
CHILD_TIMEOUT: Final = 300.0
#: The largest verdict a child may write.
MAX_VERDICT_BYTES: Final = 1 << 20
#: The largest ``done`` and ``request.json`` read.
MAX_MARKER_BYTES: Final = 64
MAX_REQUEST_BYTES: Final = 4096
#: The longest file name a request carries.
MAX_FILENAME: Final = 255
#: The largest preview side: the check renders 512 (`check.PREVIEW_SIZE`).
MAX_PREVIEW_SIDE: Final = 4096
#: The variables the child gets: none of the worker's (its proof's path among them).
_ENV_KEEP: Final = ("PATH", "LANG", "LC_ALL", "TMPDIR")
#: How long past the child's timeout the worker waits for the sandbox's ``done``.
SPOOL_GRACE: Final = 30.0
SPOOL_POLL: Final = 0.2
#: The spool's modes: the two containers share a group (the pod's ``fsGroup``).
SPOOL_DIR_MODE: Final = 0o770
SPOOL_FILE_MODE: Final = 0o640
BOX_MODE: Final = 0o700
REQUEST: Final = "request.json"
#: The spool's hand-over directory, and a job's name inside its scratch folder until then.
JOBS: Final = "jobs"
JOBS_MODE: Final = 0o750
JOB: Final = "job"
TAKEN: Final = "taken"
DONE: Final = "done"
CANCEL: Final = "cancel"
OUT: Final = "out"
BOX: Final = "box"
PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"

_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

#: Whether a refused namespace isolation was logged yet (once per process).
_isolation_logged = False

#: Checks the sandbox container never answered: it is down, or refused to start
#: (`sandbox.main` exits when a check cannot be confined).
SANDBOX_UNANSWERED: Final = Counter(
    "scadbuddy_intake_sandbox_unanswered",
    "Checks handed to the intake sandbox container that it never answered in time.",
)


class CheckCrashedError(RuntimeError):
    """The child timed out, crashed or wrote no verdict: the check could not finish."""


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str | None = None
    facts: FileFacts | None = None
    preview: bytes | None = None


def _kill_group(process: asyncio.subprocess.Process) -> None:
    # Under start_new_session the group id is the child's pid (template_process.py).
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def child_env(scratch: Path) -> dict[str, str]:
    """The child's environment: `_ENV_KEEP` of the worker's, nothing else."""
    env = {name: os.environ[name] for name in _ENV_KEEP if name in os.environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "MPLCONFIGDIR": str(scratch)})
    return env


def open_dir(name: str | Path, *, dir_fd: int | None = None) -> int:
    """A descriptor of the directory ``name``, never through a symlink."""
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def read_output(dir_fd: int, name: str, cap: int, owner: int | None) -> bytes:
    """``name`` in the directory ``dir_fd``, read as untrusted (the module docstring):
    `CheckCrashedError` unless it is a regular file with one link, owned by ``owner``
    (None: any), of at most ``cap`` bytes."""
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd
        )
    except FileNotFoundError:
        raise CheckCrashedError(f"the check wrote no {name}") from None
    except OSError as error:
        # ELOOP: a symlink; anything else (a FIFO that will not open) is no file either.
        raise CheckCrashedError(f"the check's {name} is not a plain file ({error.errno})") from None
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode) or found.st_nlink != 1:
            raise CheckCrashedError(f"the check's {name} is not a plain file of one link")
        if owner is not None and found.st_uid != owner:
            raise CheckCrashedError(f"the check's {name} is not the sandbox's own")
        if found.st_size > cap:
            raise CheckCrashedError(f"the check wrote a {name} past its cap")
        chunks: list[bytes] = []
        taken = 0
        while taken <= cap:
            chunk = os.read(fd, min(1 << 20, cap + 1 - taken))
            if not chunk:
                break
            chunks.append(chunk)
            taken += len(chunk)
        if taken > cap:
            raise CheckCrashedError(f"the check wrote a {name} past its cap")
        return b"".join(chunks)
    finally:
        os.close(fd)


def check_png(data: bytes) -> None:
    """`CheckCrashedError` unless ``data`` starts as a PNG: the signature, then an
    ``IHDR`` chunk of 13 bytes with sane values and a matching CRC
    ([PNG §11.2.2](https://www.w3.org/TR/png-3/#11IHDR))."""
    if len(data) < 33 or data[:8] != PNG_MAGIC or data[8:16] != b"\x00\x00\x00\x0dIHDR":
        raise CheckCrashedError("the check's preview is not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    depth, colour, compression, filtering, interlace = data[24:29]
    (crc,) = struct.unpack(">I", data[29:33])
    if not (0 < width <= MAX_PREVIEW_SIDE and 0 < height <= MAX_PREVIEW_SIDE):
        raise CheckCrashedError("the check's preview is not a PNG")
    if depth not in (1, 2, 4, 8, 16) or colour not in (0, 2, 3, 4, 6):
        raise CheckCrashedError("the check's preview is not a PNG")
    if compression or filtering or interlace not in (0, 1) or zlib.crc32(data[12:29]) != crc:
        raise CheckCrashedError("the check's preview is not a PNG")


def check_request(filename: str, timeout: float, address_space: int) -> dict[str, Any]:
    """A job's ``request.json``, its bounds checked: `ValueError` for a name that is empty,
    too long or holds a NUL or a separator, a timeout that is not finite and in
    (0, `CHILD_TIMEOUT`], or an address space not in (0, `CHILD_ADDRESS_SPACE`]."""
    if not 0 < len(filename) <= MAX_FILENAME or "\x00" in filename or "/" in filename:
        raise ValueError("a check's file name is 1 to 255 characters, no NUL or slash")
    if not (math.isfinite(timeout) and 0 < timeout <= CHILD_TIMEOUT):
        raise ValueError("a check's timeout is finite and in (0, CHILD_TIMEOUT]")
    if isinstance(address_space, bool) or not 0 < address_space <= CHILD_ADDRESS_SPACE:
        raise ValueError("a check's address space is in (0, CHILD_ADDRESS_SPACE]")
    return {"filename": filename, "timeout": timeout, "address_space": address_space}


async def run_child(
    box: str | Path | int,
    filename: str,
    *,
    hide: str,
    strict: bool,
    timeout: float = CHILD_TIMEOUT,
    address_space: int = CHILD_ADDRESS_SPACE,
    cancelled: asyncio.Event | None = None,
) -> int:
    """The child run in ``box`` (its working directory, holding ``upload.bin``: a path, or
    a descriptor of it, which the launcher inherits and ``fchdir``s to) through its
    launcher, with an empty tmpfs over ``hide`` in its own mount namespace (`confine.py`;
    with ``strict``, a step refused exits 126 with no parse); its exit code.
    `CheckCrashedError` past ``timeout``; the group is killed then, on cancellation and
    when ``cancelled`` is set."""
    # -I: the box (its working directory, which holds the customer's file) and the
    # environment never reach its import path.
    parser = [sys.executable, "-I", "-m", "scadbuddy.intake_worker.child"]
    box_fd = box if isinstance(box, int) else None
    argv = confine.command(
        hide, [*parser, filename, str(address_space)], strict=strict, box_fd=box_fd
    )
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        # Where the confined child sees its box (`confine.isolate` binds it there).
        env=child_env(Path(hide) / BOX if box_fd is not None else Path(str(box))),
        cwd=None if isinstance(box, int) else box,
        start_new_session=True,
        # Only its standard streams (all /dev/null) and the box's descriptor, which the
        # launcher closes before the parser starts: no worker socket (Temporal, the CRUD
        # service) is inherited.
        close_fds=True,
        pass_fds=(box_fd,) if box_fd is not None else (),
    )
    waits: list[asyncio.Future[Any]] = [asyncio.ensure_future(process.wait())]
    if cancelled is not None:
        waits.append(asyncio.ensure_future(cancelled.wait()))
    try:
        done, _ = await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        _kill_group(process)
        await process.wait()
        raise
    finally:
        for waiting in waits[1:]:
            waiting.cancel()
    _kill_group(process)
    if waits[0] not in done:
        await process.wait()
        if cancelled is not None and cancelled.is_set():
            raise CheckCrashedError("the check was cancelled")
        raise CheckCrashedError(f"the check timed out after {timeout:g}s")
    return process.returncode if process.returncode is not None else -1


def read_verdict(dir_fd: int, owner: int | None) -> Verdict:
    """The verdict in the directory ``dir_fd``, read as untrusted (`read_output`), then
    validated; a ready one's preview must be a PNG (`check_png`)."""
    global _isolation_logged
    raw = read_output(dir_fd, VERDICT, MAX_VERDICT_BYTES, owner)
    try:
        verdict = json.loads(raw)
        outcome = verdict["outcome"]
        isolation = verdict.get("isolation")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CheckCrashedError("the check wrote a verdict that is not one") from None
    if isolation is not None and not _isolation_logged:
        _isolation_logged = True
        logger.warning("the check runs without its own namespaces: %s", str(isolation)[:200])
    try:
        if outcome == "ready":
            facts = FileFacts.model_validate(verdict["facts"])
        elif outcome == "refused":
            return Verdict("refused", reason=str(verdict["reason"])[:500])
        else:
            raise CheckCrashedError("the check wrote a verdict that is not one")
    except (ValidationError, KeyError, TypeError):
        raise CheckCrashedError("the check wrote a verdict that is not one") from None
    preview = read_output(dir_fd, PREVIEW, MAX_PREVIEW_BYTES, owner)
    check_png(preview)
    return Verdict("ready", facts=facts, preview=preview)


def _make(dir_fd: int, name: str, data: bytes, mode: int) -> None:
    fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    try:
        os.write(fd, data)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _jobs_dir(work: Path) -> int:
    """The spool's ``jobs/``, made here (mode 0750), by descriptor; `CheckCrashedError`
    when a directory of that name is not this process's own."""
    with suppress(FileExistsError):
        os.mkdir(work / JOBS, JOBS_MODE)
    fd = open_dir(work / JOBS)
    if os.fstat(fd).st_uid != os.geteuid():
        os.close(fd)
        raise CheckCrashedError("the spool's jobs/ is not this worker's own")
    os.fchmod(fd, JOBS_MODE)
    return fd


def _remove_answered(jobs_fd: int, keep: str) -> None:
    """Every earlier job the sandbox has answered, removed: one that timed out could not be
    removed while the sandbox still held its box, which it removes before it writes
    ``done``. The worker runs one check at a time, so every other job is an earlier one;
    one not answered yet is the sandbox's still."""
    for name in os.listdir(jobs_fd):
        if name == keep:
            continue
        with suppress(OSError):
            job_fd = open_dir(name, dir_fd=jobs_fd)
            try:
                os.stat(DONE, dir_fd=job_fd, follow_symlinks=False)
            finally:
                os.close(job_fd)
            shutil.rmtree(name, dir_fd=jobs_fd)


async def _run_spooled(
    scratch: Path,
    filename: str,
    *,
    timeout: float,
    address_space: int,
    sandbox_uid: int,
) -> Verdict:
    """The job handed to the sandbox container (the module docstring); its verdict."""
    request = check_request(filename, timeout, address_space)
    scratch_fd = open_dir(scratch)
    jobs_fd: int | None = None
    job_fd: int | None = None
    out_fd: int | None = None
    handed = False
    try:
        jobs_fd = _jobs_dir(scratch.parent)
        _remove_answered(jobs_fd, scratch.name)
        os.mkdir(JOB, 0o700, dir_fd=scratch_fd)
        job_fd = open_dir(JOB, dir_fd=scratch_fd)
        os.rename(UPLOAD, UPLOAD, src_dir_fd=scratch_fd, dst_dir_fd=job_fd)
        os.chmod(UPLOAD, SPOOL_FILE_MODE, dir_fd=job_fd)
        os.mkdir(OUT, SPOOL_DIR_MODE, dir_fd=job_fd)
        out_fd = open_dir(OUT, dir_fd=job_fd)
        os.fchmod(out_fd, SPOOL_DIR_MODE)
        _make(job_fd, REQUEST, json.dumps(request).encode(), SPOOL_FILE_MODE)
        os.fchmod(job_fd, SPOOL_DIR_MODE)
        # The hand-over: one rename out of the worker's own scratch folder.
        os.rename(JOB, scratch.name, src_dir_fd=scratch_fd, dst_dir_fd=jobs_fd)
        handed = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout + SPOOL_GRACE
        try:
            while True:
                try:
                    os.stat(DONE, dir_fd=job_fd, follow_symlinks=False)
                    break
                except FileNotFoundError:
                    pass
                if loop.time() > deadline:
                    SANDBOX_UNANSWERED.inc()
                    raise CheckCrashedError("the sandbox gave no answer in time")
                await asyncio.sleep(SPOOL_POLL)
        except BaseException:
            with suppress(OSError):
                _make(job_fd, CANCEL, b"", SPOOL_FILE_MODE)
            raise
        answer = read_output(job_fd, DONE, MAX_MARKER_BYTES, sandbox_uid).decode(errors="replace")
        if answer.strip() != "0":
            raise CheckCrashedError(f"the check exited with {answer.strip()[:32]}")
        return read_verdict(out_fd, sandbox_uid)
    finally:
        for fd in (out_fd, job_fd):
            if fd is not None:
                os.close(fd)
        if handed and jobs_fd is not None:
            # A box the sandbox still holds is its own to remove.
            with suppress(OSError):
                shutil.rmtree(scratch.name, dir_fd=jobs_fd)
        if jobs_fd is not None:
            os.close(jobs_fd)
        os.close(scratch_fd)


async def run_check(
    file: Path,
    filename: str,
    scratch: Path,
    *,
    timeout: float = CHILD_TIMEOUT,
    address_space: int = CHILD_ADDRESS_SPACE,
    sandbox_uid: int | None = None,
) -> Verdict:
    """The child's verdict on ``file`` (``scratch``'s ``upload.bin``); `CheckCrashedError`
    when it gave none. With ``sandbox_uid`` the sandbox container, running as that uid,
    runs it (the module docstring); else this process does, in ``scratch``, with the
    tmpfs over ``scratch``'s parent (the work directory of every check)."""
    if file.parent != scratch or file.name != UPLOAD:
        raise ValueError("a check's file is its scratch folder's upload.bin")
    if sandbox_uid is not None:
        return await _run_spooled(
            scratch, filename, timeout=timeout, address_space=address_space, sandbox_uid=sandbox_uid
        )
    code = await run_child(
        scratch,
        filename,
        hide=str(scratch.parent),
        strict=False,
        timeout=timeout,
        address_space=address_space,
    )
    if code != 0:
        raise CheckCrashedError(f"the check exited with {code}")
    scratch_fd = open_dir(scratch)
    try:
        return read_verdict(scratch_fd, os.geteuid())
    finally:
        os.close(scratch_fd)
