"""Runs one check in a child process (`child.py`): its own process group, an address-space
cap it sets on itself, and a timeout, past which (or on cancellation) the whole group is
killed, as `workflows/template_process.py` runs template Python (spec 2026-11-13 §7)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from scadbuddy.intake_worker.wire import FileFacts

#: The child's address space: the largest measured fixture's model inflates to 470 MiB
#: (spec §7.1), and the read keeps its meshes besides.
CHILD_ADDRESS_SPACE: Final = 4 << 30
CHILD_TIMEOUT: Final = 300.0
#: The largest verdict a child may write.
MAX_VERDICT_BYTES: Final = 1 << 20
#: The variables the child gets: none of the worker's (its proof's path among them).
_ENV_KEEP: Final = ("PATH", "LANG", "LC_ALL", "TMPDIR")


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


async def run_check(
    file: Path,
    filename: str,
    scratch: Path,
    *,
    timeout: float = CHILD_TIMEOUT,
    address_space: int = CHILD_ADDRESS_SPACE,
) -> Verdict:
    """The child's verdict on ``file``; `CheckCrashedError` when it gave none."""
    preview = scratch / "preview.png"
    result = scratch / "verdict.json"
    env = child_env(scratch)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        # -I: the scratch directory (its working directory, which holds the customer's
        # file) and the environment never reach its import path.
        "-I",
        "-m",
        "scadbuddy.intake_worker.child",
        str(file),
        filename,
        str(preview),
        str(result),
        str(address_space),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd=scratch,
        start_new_session=True,
        # Only its standard streams (all /dev/null): no inherited descriptor, so it cannot
        # reach the worker's sockets (Temporal, the CRUD service) or its proof.
        close_fds=True,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout)
    except TimeoutError:
        _kill_group(process)
        await process.wait()
        raise CheckCrashedError(f"the check timed out after {timeout:g}s") from None
    except asyncio.CancelledError:
        _kill_group(process)
        await process.wait()
        raise
    _kill_group(process)
    if process.returncode != 0 or not result.is_file():
        raise CheckCrashedError(f"the check exited with {process.returncode}")
    if result.stat().st_size > MAX_VERDICT_BYTES:
        raise CheckCrashedError("the check wrote a verdict past its cap")
    verdict = json.loads(result.read_text())
    if verdict["outcome"] == "ready":
        if not preview.is_file():
            raise CheckCrashedError("the check wrote no preview")
        return Verdict("ready", facts=FileFacts.model_validate(verdict["facts"]), preview=preview)
    return Verdict("refused", reason=str(verdict["reason"]))
