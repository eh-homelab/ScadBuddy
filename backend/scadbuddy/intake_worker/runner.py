"""Runs one check in a child process (`child.py`): its own process group, an address-space
cap it sets on itself, and a timeout, past which (or on cancellation) the whole group is
killed, as `workflows/template_process.py` runs template Python (spec 2026-11-13 §7).

Two ways to reach the child:

- local (the default, for development and tests): the worker starts it itself;
- the sandbox container (``SCADBUDDY_INTAKE_SANDBOX=true``, the deployment's): the worker
  only hands the job over a spool directory it shares with a second container of the pod
  (`sandbox.py`), which runs as another user with no service-account token, no proof and a
  read-only root, and starts the child there. The worker never runs the parse.

The spool protocol, in the job's own directory (the check's scratch folder): the worker
writes ``upload.bin`` and ``request.json``, then ``ready``; the sandbox claims it by making
``taken`` (``O_EXCL``), runs the child, which writes ``verdict.json`` and, for a ready
file, ``preview.png``, then writes ``done`` (the child's exit, or ``timeout``). The worker
waits for ``done`` up to the child's timeout plus `SPOOL_GRACE`; past it, or on
cancellation, it writes ``cancel``, which the sandbox answers by killing the child's group.
Whatever came back is read as untrusted: capped, then validated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from scadbuddy.intake_worker.wire import MAX_PREVIEW_BYTES, FileFacts

logger = logging.getLogger(__name__)

#: The child's address space: the largest measured fixture's model inflates to 470 MiB
#: (spec §7.1), and the read keeps its meshes besides.
CHILD_ADDRESS_SPACE: Final = 4 << 30
CHILD_TIMEOUT: Final = 300.0
#: The largest verdict a child may write.
MAX_VERDICT_BYTES: Final = 1 << 20
#: The variables the child gets: none of the worker's (its proof's path among them).
_ENV_KEEP: Final = ("PATH", "LANG", "LC_ALL", "TMPDIR")
#: How long past the child's timeout the worker waits for the sandbox's ``done``.
SPOOL_GRACE: Final = 30.0
SPOOL_POLL: Final = 0.2
#: The spool files' modes: the two containers share a group (the pod's ``fsGroup``).
SPOOL_DIR_MODE: Final = 0o770
SPOOL_FILE_MODE: Final = 0o640
UPLOAD: Final = "upload.bin"
REQUEST: Final = "request.json"
READY: Final = "ready"
TAKEN: Final = "taken"
DONE: Final = "done"
CANCEL: Final = "cancel"
VERDICT: Final = "verdict.json"
PREVIEW: Final = "preview.png"

#: Whether a refused namespace isolation was logged yet (once per process).
_isolation_logged = False


class CheckCrashedError(RuntimeError):
    """The child timed out, crashed or wrote no verdict: the check could not finish."""


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str | None = None
    facts: FileFacts | None = None
    preview: Path | None = None


def _kill_group(process: asyncio.subprocess.Process) -> None:
    # Under start_new_session the group id is the child's pid (template_process.py).
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def child_env(scratch: Path) -> dict[str, str]:
    """The child's environment: `_ENV_KEEP` of the worker's, nothing else."""
    env = {name: os.environ[name] for name in _ENV_KEEP if name in os.environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "MPLCONFIGDIR": str(scratch)})
    return env


async def run_child(
    file: Path,
    filename: str,
    scratch: Path,
    *,
    timeout: float = CHILD_TIMEOUT,
    address_space: int = CHILD_ADDRESS_SPACE,
    cancelled: asyncio.Event | None = None,
) -> int:
    """The child run on ``file``; its exit code. `CheckCrashedError` past ``timeout``; the
    group is killed then, on cancellation and when ``cancelled`` is set."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        # -I: the scratch directory (its working directory, which holds the customer's
        # file) and the environment never reach its import path.
        "-I",
        "-m",
        "scadbuddy.intake_worker.child",
        str(file),
        filename,
        str(scratch / PREVIEW),
        str(scratch / VERDICT),
        str(address_space),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=child_env(scratch),
        cwd=scratch,
        start_new_session=True,
        # Only its standard streams (all /dev/null): no inherited descriptor, so it cannot
        # reach the worker's sockets (Temporal, the CRUD service) or its proof.
        close_fds=True,
    )
    waits = [asyncio.ensure_future(process.wait())]
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


def read_verdict(scratch: Path) -> Verdict:
    """The child's verdict in ``scratch``, read as untrusted: capped and validated."""
    global _isolation_logged
    result, preview = scratch / VERDICT, scratch / PREVIEW
    if not result.is_file():
        raise CheckCrashedError("the check wrote no verdict")
    if result.stat().st_size > MAX_VERDICT_BYTES:
        raise CheckCrashedError("the check wrote a verdict past its cap")
    try:
        verdict = json.loads(result.read_text())
        outcome = verdict["outcome"]
        isolation = verdict.get("isolation")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CheckCrashedError("the check wrote a verdict that is not one") from None
    if isolation is not None and not _isolation_logged:
        _isolation_logged = True
        logger.warning("the check runs without its own namespaces: %s", str(isolation)[:200])
    if outcome == "ready":
        if not preview.is_file():
            raise CheckCrashedError("the check wrote no preview")
        if preview.stat().st_size > MAX_PREVIEW_BYTES:
            raise CheckCrashedError("the check wrote a preview past its cap")
        return Verdict("ready", facts=FileFacts.model_validate(verdict["facts"]), preview=preview)
    if outcome == "refused":
        return Verdict("refused", reason=str(verdict["reason"])[:500])
    raise CheckCrashedError("the check wrote a verdict that is not one")


async def _run_spooled(
    file: Path, filename: str, scratch: Path, *, timeout: float, address_space: int
) -> None:
    """The job handed to the sandbox container (the module docstring); returns once its
    ``done`` says the child exited 0."""
    if file.parent != scratch or file.name != UPLOAD:
        raise ValueError("a spooled check's file is the job's upload.bin")
    scratch.chmod(SPOOL_DIR_MODE)
    file.chmod(SPOOL_FILE_MODE)
    request = {"filename": filename, "timeout": timeout, "address_space": address_space}
    (scratch / REQUEST).write_text(json.dumps(request))
    (scratch / REQUEST).chmod(SPOOL_FILE_MODE)
    (scratch / READY).touch(mode=SPOOL_FILE_MODE)
    done = scratch / DONE
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout + SPOOL_GRACE
    try:
        while not done.is_file():
            if loop.time() > deadline:
                raise CheckCrashedError("the sandbox gave no answer in time")
            await asyncio.sleep(SPOOL_POLL)
    except BaseException:
        with suppress(OSError):
            (scratch / CANCEL).touch(mode=SPOOL_FILE_MODE)
        raise
    answer = done.read_text()[:64].strip()
    if answer != "0":
        raise CheckCrashedError(f"the check exited with {answer}")


async def run_check(
    file: Path,
    filename: str,
    scratch: Path,
    *,
    timeout: float = CHILD_TIMEOUT,
    address_space: int = CHILD_ADDRESS_SPACE,
    sandbox: bool = False,
) -> Verdict:
    """The child's verdict on ``file``; `CheckCrashedError` when it gave none. With
    ``sandbox`` the sandbox container runs it (the module docstring)."""
    if sandbox:
        await _run_spooled(file, filename, scratch, timeout=timeout, address_space=address_space)
    else:
        code = await run_child(
            file, filename, scratch, timeout=timeout, address_space=address_space
        )
        if code != 0:
            raise CheckCrashedError(f"the check exited with {code}")
    return read_verdict(scratch)
