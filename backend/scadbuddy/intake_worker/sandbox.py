"""``python -m scadbuddy.intake_worker.sandbox``: the intake pod's sandbox container (#6133,
spec 2026-11-13 §7). It runs every check's child (`runner.run_child`), so the customer's
file is parsed in a container that holds no service-account token, no proof, no database
secret and no KEK, as another user than the worker, on a read-only root. It shares only the
spool (``SCADBUDDY_INTAKE_WORK_DIR``, an emptyDir) with the worker; the protocol is
`runner`'s docstring. It refuses to start with a database secret, the KEK or a proof
(`check_sandbox_environment`), and makes itself non-dumpable (`hardening.forbid_dumping`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Final

from scadbuddy.core.logging import configure_logging
from scadbuddy.intake_worker.hardening import forbid_dumping
from scadbuddy.intake_worker.runner import (
    CANCEL,
    CHILD_ADDRESS_SPACE,
    CHILD_TIMEOUT,
    DONE,
    READY,
    REQUEST,
    SPOOL_FILE_MODE,
    SPOOL_POLL,
    TAKEN,
    UPLOAD,
    CheckCrashedError,
    run_child,
)
from scadbuddy.intake_worker.settings import FORBIDDEN_ENV, IntakeRefusal

logger = logging.getLogger(__name__)

#: Besides `FORBIDDEN_ENV`: the sandbox holds no proof of the worker's.
SANDBOX_FORBIDDEN_ENV: Final = (
    *FORBIDDEN_ENV,
    "SCADBUDDY_SERVICE_TOKEN_FILE",
    "SCADBUDDY_SERVICE_BOOTSTRAP_FILE",
)
#: The largest ``request.json`` the sandbox reads.
MAX_REQUEST_BYTES: Final = 4096


def check_sandbox_environment(environ: Mapping[str, str]) -> None:
    reasons = [
        f"{name} is set; the intake sandbox holds no secret, key or proof"
        for name in SANDBOX_FORBIDDEN_ENV
        if name in environ
    ]
    if reasons:
        raise IntakeRefusal(reasons)


def _claim(job: Path) -> bool:
    try:
        fd = os.open(job / TAKEN, os.O_CREAT | os.O_EXCL | os.O_WRONLY, SPOOL_FILE_MODE)
    except OSError:
        return False
    os.close(fd)
    return True


def _answer(job: Path, answer: str) -> None:
    # The worker may have given up and removed the job meanwhile.
    with suppress(OSError):
        path = job / DONE
        path.write_text(answer)
        path.chmod(SPOOL_FILE_MODE)


async def run_job(job: Path) -> None:
    """One claimed job: its child run, then ``done``."""
    try:
        raw = (job / REQUEST).read_bytes()[:MAX_REQUEST_BYTES]
        request = json.loads(raw)
        filename = str(request["filename"])
        timeout = min(float(request["timeout"]), CHILD_TIMEOUT)
        address_space = min(int(request["address_space"]), CHILD_ADDRESS_SPACE)
    except (OSError, ValueError, KeyError, TypeError):
        _answer(job, "bad-request")
        return
    cancelled = asyncio.Event()

    async def watch_cancel() -> None:
        while not (job / CANCEL).exists() and job.is_dir():
            await asyncio.sleep(SPOOL_POLL)
        cancelled.set()

    watcher = asyncio.create_task(watch_cancel())
    try:
        code = await run_child(
            job / UPLOAD,
            filename,
            job,
            timeout=timeout,
            address_space=address_space,
            cancelled=cancelled,
        )
    except CheckCrashedError:
        _answer(job, "timeout")
    else:
        _answer(job, str(code))
    finally:
        watcher.cancel()


async def serve(spool: Path, concurrency: int, stop: asyncio.Event) -> None:
    """Claims and runs the spool's ready jobs, at most ``concurrency`` at once, until
    ``stop``."""
    slots = asyncio.Semaphore(concurrency)
    running: set[asyncio.Task[None]] = set()

    async def one(job: Path) -> None:
        try:
            await run_job(job)
        finally:
            slots.release()

    while not stop.is_set():
        for ready in sorted(spool.glob(f"*/{READY}")):
            job = ready.parent
            if (job / TAKEN).exists() or slots.locked():
                continue
            await slots.acquire()
            if not _claim(job):
                slots.release()
                continue
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
    logger.info("intake sandbox serving %s", spool)
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
    asyncio.run(_main(spool, concurrency))
    return 0


if __name__ == "__main__":
    sys.exit(main())
