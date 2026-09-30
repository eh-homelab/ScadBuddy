"""Run a template function in a subprocess, in its own process group, killed with every
child it spawned on cancellation or timeout (spec 2026-09-27 §3.4, §5.2)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import tempfile
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any

from scadbuddy.core.fontconfig import env_for
from scadbuddy.render.runner import LOG_TAIL_LINES

MAX_RESULT_BYTES = 1 << 20


class TemplateError(RuntimeError):
    def __init__(self, message: str, log_tail: list[str], *, retryable: bool = False) -> None:
        super().__init__(message)
        self.log_tail = log_tail
        self.retryable = retryable


def template_out_key(call: Any, source_sha: str) -> str:
    """Where a call's emitted files go. ``source_sha`` is `activities.py`'s own sha256,
    so a live template (revision None) edited between two calls never reuses the
    first call's directory; identical calls on one source write identical bytes."""
    raw = json.dumps(
        [call.slug, call.revision, source_sha, call.name, call.args, call.kwargs],
        sort_keys=True,
        default=str,
    )
    return "act-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _kill_group(process: asyncio.subprocess.Process) -> None:
    # Under start_new_session the group id is the child's pid. Not os.getpgid(pid):
    # once the leader is reaped that lookup fails while its children still run.
    # Accepted edge: after a normal exit with the group already empty, the pid could
    # be reused as another new session's group id before this runs; a surviving
    # grandchild keeps the id taken, which is the case this kill exists for.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def _tail(log: Path) -> list[str]:
    with log.open("rb") as f:
        lines = deque(f, maxlen=LOG_TAIL_LINES)
    return [line.decode("utf-8", errors="replace").rstrip("\n") for line in lines]


async def run_template(
    model_dir: Path,
    request: dict[str, Any],
    *,
    out: Path,
    out_key: str,
    python: str,
    data_dir: Path,
    timeout: float,
) -> Any:
    with tempfile.TemporaryDirectory(prefix="scadbuddy-template-") as scratch:
        result_path = Path(scratch) / "result.json"
        # The allowlist openscad gets (#281), not the worker's environment (§9).
        env = env_for(data_dir)
        env.update(
            {
                "SCADBUDDY_TEMPLATE_OUT": str(out),
                "SCADBUDDY_TEMPLATE_OUT_KEY": out_key,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        # Output goes to a file, not a pipe: asyncio's `wait()` returns only once every
        # pipe has closed, so a grandchild still holding one would keep it waiting
        # after the function returned.
        log = Path(scratch) / "output.log"
        with log.open("wb") as output:
            process = await asyncio.create_subprocess_exec(
                python,
                "-m",
                "scadbuddy.workflows.template_runner",
                str(model_dir),
                str(result_path),
                cwd=model_dir,
                stdin=asyncio.subprocess.PIPE,
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        if process.stdin is None:
            raise RuntimeError("the template process has no stdin")
        with suppress(BrokenPipeError, ConnectionResetError):
            process.stdin.write(json.dumps(request).encode())
            await process.stdin.drain()
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except TimeoutError:
            _kill_group(process)
            await process.wait()
            name = request.get("name")
            raise TemplateError(
                f"pipeline/activities.py:{name} timed out after {timeout:g}s", _tail(log)
            ) from None
        except asyncio.CancelledError:
            _kill_group(process)
            await process.wait()
            raise
        # The function returned; whatever it left running goes with its group.
        _kill_group(process)
        tail = _tail(log)
        if process.returncode != 0 or not result_path.is_file():
            raise TemplateError(
                f"the template process exited with {process.returncode}", tail, retryable=True
            )
        if result_path.stat().st_size > MAX_RESULT_BYTES:
            raise TemplateError(
                "the template function returned more than 1 MiB;"
                " return scadbuddy.template.emit(name, data) instead",
                tail,
            )
        reply = json.loads(result_path.read_text(encoding="utf-8"))
        if "error" in reply:
            where = (
                reply["file"] if reply.get("line") is None else f"{reply['file']}:{reply['line']}"
            )
            raise TemplateError(f"{where}: {reply['error']}", tail)
        return reply["ok"]
