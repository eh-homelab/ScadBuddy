"""``python -m scadbuddy.intake_worker.sandbox``: the intake pod's sandbox container (#6133,
spec 2026-11-13 §7). It runs every check's child (`runner.run_child`), so the customer's
file is parsed in a container that holds no service-account token, no proof, no database
secret and no KEK, as another user than the worker, on a read-only root. It shares only the
spool (``SCADBUDDY_INTAKE_WORK_DIR``, an emptyDir) with the worker; the protocol is
`runner`'s docstring. It refuses to start with a database secret, the KEK or a proof
(`check_sandbox_environment`), and makes itself non-dumpable (`hardening.forbid_dumping`).

Each job's child runs in a box of its own (``box/``, mode 0700) through its launcher with
``--strict`` (`confine.py`): its own user, network and mount namespaces with an empty tmpfs
over the spool, so it reaches no other job's directory, then no capabilities, no new
privileges and a seccomp filter. It fails closed: at start the sandbox runs the launcher
once (`probe_isolation`), and where any step is refused it logs why and exits 3 without
serving a job, so the pod never parses a file unconfined (the worker counts the checks it
then hands over unanswered, `runner.SANDBOX_UNANSWERED`). A job is the spool's
``jobs/<name>``, which the worker moves in whole (`runner`'s docstring).

The isolation rule (spec §7): one customer's file per pod at any moment. The sandbox runs
one check at a time, fixed, and empties every finished job before it claims the next
(`_wipe`); the worker takes one check at a time too. So while a parse runs, no other
customer's file or process exists in the pod to reach. Scale with replicas of the
Deployment, never with checks at once. Before each claim it also makes sure no process of
an earlier job is alive (`clear_job_processes`): it is the pod's child subreaper, so a
job's orphans become its own, and it kills what it finds; one that survives makes it exit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Final

from scadbuddy.core.logging import configure_logging
from scadbuddy.intake_worker import confine
from scadbuddy.intake_worker.child import PREVIEW, UPLOAD, VERDICT
from scadbuddy.intake_worker.hardening import forbid_dumping
from scadbuddy.intake_worker.hardening import prctl as hardening_prctl
from scadbuddy.intake_worker.runner import (
    BOX,
    BOX_MODE,
    CANCEL,
    DONE,
    JOBS,
    MAX_REQUEST_BYTES,
    MAX_VERDICT_BYTES,
    OUT,
    REQUEST,
    SPOOL_FILE_MODE,
    SPOOL_POLL,
    TAKEN,
    CheckCrashedError,
    check_request,
    open_dir,
    read_output,
    run_child,
)
from scadbuddy.intake_worker.settings import FORBIDDEN_ENV, IntakeRefusal
from scadbuddy.intake_worker.wire import MAX_PREVIEW_BYTES
from scadbuddy.storefront.wire import MAX_MODEL_FILE_BYTES

logger = logging.getLogger(__name__)

#: Besides `FORBIDDEN_ENV`: the sandbox holds no proof of the worker's.
SANDBOX_FORBIDDEN_ENV: Final = (
    *FORBIDDEN_ENV,
    "SCADBUDDY_SERVICE_TOKEN_FILE",
    "SCADBUDDY_SERVICE_BOOTSTRAP_FILE",
)


def check_sandbox_environment(environ: Mapping[str, str]) -> None:
    reasons = [
        f"{name} is set; the intake sandbox holds no secret, key or proof"
        for name in SANDBOX_FORBIDDEN_ENV
        if name in environ
    ]
    if reasons:
        raise IntakeRefusal(reasons)


def probe_isolation(spool: Path) -> str | None:
    """Whether a check can be confined here (`confine.py --strict`, running a program
    that does nothing): None when it can, else why not."""
    out = subprocess.run(
        confine.command(str(spool), [sys.executable, "-I", "-c", "pass"], strict=True),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if out.returncode != 0:
        return out.stderr.strip() or f"the launcher exited with {out.returncode}"
    return None


def _make(dir_fd: int, name: str, data: bytes) -> None:
    fd = os.open(
        name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, SPOOL_FILE_MODE, dir_fd=dir_fd
    )
    try:
        os.write(fd, data)
        os.fchmod(fd, SPOOL_FILE_MODE)
    finally:
        os.close(fd)


def _answer(job_fd: int, answer: str) -> None:
    # The worker may have given up and removed the job meanwhile.
    with suppress(OSError):
        _make(job_fd, DONE, answer.encode())


def _claim(job_fd: int) -> bool:
    try:
        _make(job_fd, TAKEN, b"")
    except OSError:
        return False
    return True


def _copy_upload(job_fd: int, box_fd: int) -> None:
    """The worker's upload copied into the box as a file the sandbox makes, read with
    ``O_NOFOLLOW`` (a symlink is refused) and capped at the largest model file."""
    source = os.open(
        UPLOAD, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=job_fd
    )
    try:
        found = os.fstat(source)
        if not stat.S_ISREG(found.st_mode) or found.st_size > MAX_MODEL_FILE_BYTES:
            raise CheckCrashedError("the job's upload is not a plain file within its cap")
        target = os.open(
            UPLOAD, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=box_fd
        )
        try:
            copied = 0
            while copied < found.st_size:
                sent = os.sendfile(target, source, copied, found.st_size - copied)
                if sent == 0:
                    break
                copied += sent
        finally:
            os.close(target)
    finally:
        os.close(source)


def _hand_back(box_fd: int, out_fd: int) -> None:
    """The child's verdict and preview, read as untrusted from the box, written into the
    worker's ``out/`` as files the sandbox makes."""
    own = os.geteuid()
    with suppress(CheckCrashedError):
        _make(out_fd, VERDICT, read_output(box_fd, VERDICT, MAX_VERDICT_BYTES, own))
    with suppress(CheckCrashedError):
        _make(out_fd, PREVIEW, read_output(box_fd, PREVIEW, MAX_PREVIEW_BYTES, own))


async def run_job(job: Path, spool: Path) -> None:
    """One job (the claim is ``taken``): its child run in a box of its own, then ``done``."""
    try:
        job_fd = open_dir(job)
    except OSError:
        return
    try:
        if not _claim(job_fd):
            return
        await _run_claimed(job_fd, spool)
    finally:
        os.close(job_fd)


async def _run_claimed(job_fd: int, spool: Path) -> None:
    try:
        request = json.loads(read_output(job_fd, REQUEST, MAX_REQUEST_BYTES, None))
        checked = check_request(
            str(request["filename"]), float(request["timeout"]), int(request["address_space"])
        )
    except (CheckCrashedError, ValueError, KeyError, TypeError):
        _answer(job_fd, "bad-request")
        return
    box_fd: int | None = None
    try:
        os.mkdir(BOX, BOX_MODE, dir_fd=job_fd)
        box_fd = open_dir(BOX, dir_fd=job_fd)
        os.fchmod(box_fd, BOX_MODE)
        _copy_upload(job_fd, box_fd)
    except (OSError, CheckCrashedError):
        if box_fd is not None:
            os.close(box_fd)
        with suppress(OSError):
            shutil.rmtree(BOX, dir_fd=job_fd)
        _answer(job_fd, "bad-upload")
        return
    cancelled = asyncio.Event()

    async def watch_cancel() -> None:
        while True:
            try:
                os.stat(CANCEL, dir_fd=job_fd, follow_symlinks=False)
                break
            except FileNotFoundError:
                await asyncio.sleep(SPOOL_POLL)
            except OSError:
                break
        cancelled.set()

    watcher = asyncio.create_task(watch_cancel())
    try:
        # The box by its descriptor (the magic link of this process's fd), never by a
        # path another child could have swapped.
        code = await run_child(
            f"/proc/{os.getpid()}/fd/{box_fd}",
            checked["filename"],
            hide=str(spool),
            strict=True,
            timeout=checked["timeout"],
            address_space=checked["address_space"],
            cancelled=cancelled,
        )
    except CheckCrashedError:
        answer = "timeout"
    else:
        answer = str(code)
        if code == 0:
            try:
                out_fd = open_dir(OUT, dir_fd=job_fd)
            except OSError:
                answer = "no-out"
            else:
                try:
                    _hand_back(box_fd, out_fd)
                finally:
                    os.close(out_fd)
    finally:
        watcher.cancel()
        os.close(box_fd)
        with suppress(OSError):
            shutil.rmtree(BOX, dir_fd=job_fd)
    _answer(job_fd, answer)


#: How long the processes of an earlier job may take to die once killed.
JOB_EXIT_GRACE: Final = 5.0
_PR_SET_CHILD_SUBREAPER: Final = 36


class JobOutlivedError(RuntimeError):
    """A process of an earlier job is still alive: no next job may be claimed."""


def job_processes() -> list[int]:
    """This process's descendants in another PID namespace than its own, or whose
    namespace cannot be read: the processes of a job, since each parse has a PID namespace
    of its own and its launcher and parser are non-dumpable (`confine.py`). Between jobs no
    other descendant exists. A zombie does not count, and is reaped when it is this
    process's child (the sandbox is the pod's child subreaper)."""
    own = os.readlink("/proc/self/ns/pid")
    found: list[int] = []
    todo = [os.getpid()]
    while todo:
        parent = todo.pop()
        try:
            tasks = os.listdir(f"/proc/{parent}/task")
        except OSError:
            continue
        for tid in tasks:
            try:
                with open(f"/proc/{parent}/task/{tid}/children") as listed:
                    children = [int(pid) for pid in listed.read().split()]
            except OSError:
                continue
            for pid in children:
                todo.append(pid)
                if _zombie(pid):
                    if parent == os.getpid():
                        with suppress(ChildProcessError):
                            os.waitpid(pid, os.WNOHANG)
                    continue
                try:
                    if os.readlink(f"/proc/{pid}/ns/pid") != own:
                        found.append(pid)
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except OSError:
                    found.append(pid)
    return found


def _zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat_file:
            return stat_file.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")
    except (OSError, IndexError):
        return True


async def clear_job_processes() -> None:
    """No process of an earlier job left alive: each found is killed, and one still alive
    after `JOB_EXIT_GRACE` raises `JobOutlivedError`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + JOB_EXIT_GRACE
    while left := job_processes():
        if loop.time() > deadline:
            raise JobOutlivedError(f"processes of an earlier job outlived it: {left[:10]}")
        for pid in left:
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        await asyncio.sleep(SPOOL_POLL)


def _wipe(job: Path) -> None:
    """Everything in a finished job removed but ``done`` (the job's directory is the
    worker's, in its ``jobs/``, which only it can remove)."""
    try:
        job_fd = open_dir(job)
    except OSError:
        return
    try:
        for name in os.listdir(job_fd):
            if name == DONE:
                continue
            with suppress(OSError):
                if stat.S_ISDIR(os.stat(name, dir_fd=job_fd, follow_symlinks=False).st_mode):
                    shutil.rmtree(name, dir_fd=job_fd)
                else:
                    os.unlink(name, dir_fd=job_fd)
    finally:
        os.close(job_fd)


async def serve(spool: Path, stop: asyncio.Event) -> None:
    """Claims and runs the spool's ready jobs one at a time until ``stop``; before each
    claim, every finished job is emptied (the isolation rule, the module docstring)."""
    while not stop.is_set():
        jobs = sorted((spool / JOBS).glob("*"))
        ready = [
            job
            for job in jobs
            if not (job / DONE).exists()
            and not (job / BOX).exists()
            and not (job / TAKEN).exists()
            and not (job / TAKEN).is_symlink()
        ]
        if ready:
            for job in jobs:
                if (job / DONE).exists():
                    _wipe(job)
            await clear_job_processes()
            await run_job(ready[0], spool)
            continue
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), SPOOL_POLL)


async def _main(spool: Path) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    logger.info("intake sandbox serving %s, one check at a time", spool)
    await serve(spool, stop)


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging(os.environ.get("SCADBUDDY_LOG_LEVEL", "INFO").upper())
    try:
        check_sandbox_environment(os.environ)
    except IntakeRefusal as refusal:
        for reason in refusal.reasons:
            logger.error(reason)
        return 2
    forbid_dumping()
    spool = Path(os.environ.get("SCADBUDDY_INTAKE_WORK_DIR", "/tmp"))
    refused = probe_isolation(spool)
    if refused is not None:
        logger.error(
            "the intake sandbox refuses to start: a check cannot be confined here (%s)."
            " Allow unshare(CLONE_NEWUSER) for this container (its seccomp profile).",
            refused[:300],
        )
        return 3
    # A job's orphans become this process's children, so `job_processes` sees them.
    if hardening_prctl(_PR_SET_CHILD_SUBREAPER, 1) != 0:
        logger.error("the intake sandbox refuses to start: it cannot be the child subreaper")
        return 3
    try:
        asyncio.run(_main(spool))
    except JobOutlivedError as error:
        logger.error("the intake sandbox stops: %s", error)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
