"""``python -m scadbuddy.intake_worker.sandbox``: the intake pod's sandbox container (#6133,
spec 2026-11-13 §7). It runs every check's child (`runner.run_child`), so the customer's
file is parsed in a container that holds no service-account token, no proof, no database
secret and no KEK, as another user than the worker, on a read-only root. It shares only the
spool (``SCADBUDDY_INTAKE_WORK_DIR``, an emptyDir) with the worker; the protocol is
`runner`'s docstring. It refuses to start with a database secret, the KEK or a proof
(`check_sandbox_environment`), and makes itself non-dumpable (`hardening.forbid_dumping`).

Each job's child runs in a box of its own (``box/``, mode 0700) and, where the kernel
allows it, in its own mount namespace with an empty tmpfs over the spool, so it reaches no
other job's directory (`hardening.isolate`). At start the sandbox tries that isolation once
(`probe_isolation`); where it is refused, it runs one job at a time, so no two children's
jobs are open at once.
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
from scadbuddy.intake_worker.child import PREVIEW, UPLOAD, VERDICT
from scadbuddy.intake_worker.hardening import forbid_dumping
from scadbuddy.intake_worker.runner import (
    BOX,
    BOX_MODE,
    CANCEL,
    DONE,
    MAX_REQUEST_BYTES,
    MAX_VERDICT_BYTES,
    OUT,
    READY,
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
    """Whether a child can take its namespaces here: None when it can, else why not."""
    code = (
        "import sys; from scadbuddy.intake_worker.hardening import isolate;"
        " print(isolate(sys.argv[1]) or '')"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, str(spool)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if out.returncode != 0:
        return f"the probe exited with {out.returncode}"
    return out.stdout.strip() or None


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


async def serve(spool: Path, concurrency: int, stop: asyncio.Event) -> None:
    """Claims and runs the spool's ready jobs, at most ``concurrency`` at once, until
    ``stop``."""
    slots = asyncio.Semaphore(concurrency)
    running: set[asyncio.Task[None]] = set()

    async def one(job: Path) -> None:
        try:
            await run_job(job, spool)
        finally:
            slots.release()

    while not stop.is_set():
        for ready in sorted(spool.glob(f"*/{READY}")):
            job = ready.parent
            if (job / DONE).exists() or (job / BOX).exists() or slots.locked():
                continue
            with suppress(OSError):
                os.stat(job / TAKEN, follow_symlinks=False)
                continue
            await slots.acquire()
            task = asyncio.create_task(one(job))
            running.add(task)
            task.add_done_callback(running.discard)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), SPOOL_POLL)
    for task in running:
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)


async def _main(spool: Path, concurrency: int) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    logger.info("intake sandbox serving %s, %d at once", spool, concurrency)
    await serve(spool, concurrency, stop)


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
    concurrency = int(os.environ.get("SCADBUDDY_INTAKE_CONCURRENCY", "2"))
    if not 1 <= concurrency <= 16:
        logger.error("SCADBUDDY_INTAKE_CONCURRENCY must be between 1 and 16")
        return 2
    refused = probe_isolation(spool)
    if refused is not None:
        logger.warning(
            "a check cannot take its own namespaces (%s): one check at a time", refused[:200]
        )
        concurrency = 1
    asyncio.run(_main(spool, concurrency))
    return 0


if __name__ == "__main__":
    sys.exit(main())
