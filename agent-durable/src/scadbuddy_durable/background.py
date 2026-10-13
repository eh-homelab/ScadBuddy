"""A durable session's Monitor commands (agent/src/background/), on the ``agent`` queue.

The agent keeps a session's Monitor as a ``BackgroundTask`` workflow on ``agent-tools``
(agent/src/temporal/backgroundTask.ts). A durable session's Bash runs here, in the
engine's working directory, so its Monitor commands do too: ``bg_monitor_command`` runs
one with bash, makes each batch of stdout lines (within 200 ms) an event in
``ai_background_events``, signals the workflow ``bg_events``, and returns how the watch
ended. It mirrors agent/src/background/monitor.ts (``runCommand``, ``eventNotice``), which
runs a classic session's; the workflow sends the events to the session.

The command gets a shell's environment only (PATH, HOME, LANG, LC_ALL, TZ, TERM), never
the worker's: that holds the database URL and the key file's path.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import psycopg
from temporalio import activity

Connect = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]]

BATCH_S = 0.2
HEARTBEAT_S = 5.0
# agent/src/background/store.ts MAX_EVENTS, EVENT_MAX_CHARS.
MAX_EVENTS = 200
EVENT_MAX_CHARS = 8000
EVENTS_SIGNAL = "bg_events"
_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM")


def workflow_id(session_id: str, task_id: str) -> str:
    """agent/src/background/service.ts ``backgroundWorkflowId``: it names the session,
    whose key the codec seals the task's payloads under."""
    return f"bgtask-{session_id}-{task_id}"


def event_notice(task_id: str, description: str, text: str) -> str:
    """One event, as agent/src/background/monitor.ts ``eventNotice`` writes it."""
    return (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n"
        f'<summary>Monitor event: "{description}"</summary>\n'
        f"<event>{text}</event>\n</task-notification>"
    )


class LineSplitter:
    """Lines of a stream, never holding more than EVENT_MAX_CHARS of one: a longer line is
    cut there and the rest of it dropped (agent/src/background/monitor.ts ``lineSplitter``)."""

    def __init__(self) -> None:
        self._line = ""
        self._dropping = False
        self._out: list[str] = []

    def push(self, chunk: str) -> None:
        parts = chunk.split("\n")
        for i, part in enumerate(parts):
            if not self._dropping:
                self._line += part
                if len(self._line) > EVENT_MAX_CHARS:
                    self._out.append(self._line[:EVENT_MAX_CHARS] + " […cut]")
                    self._line = ""
                    self._dropping = True
            if i < len(parts) - 1:
                if not self._dropping:
                    self._out.append(self._line)
                self._line = ""
                self._dropping = False

    def end(self) -> None:
        if self._line and not self._dropping:
            self._out.append(self._line)
        self._line = ""

    def take(self) -> list[str]:
        out, self._out = self._out, []
        return out


def monitor_env(env: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if env is None else env
    return {name: source[name] for name in _ENV if name in source}


class MonitorActivities:
    def __init__(self, connect: Connect, cwd: str) -> None:
        self._connect = connect
        self._cwd = cwd

    async def _task(self, task_id: str) -> dict[str, Any] | None:
        async with self._connect() as conn:
            cur = await conn.execute(
                "SELECT description, spec, state, session_id FROM ai_background_tasks "
                "WHERE id = %s",
                (task_id,),
            )
            row = await cur.fetchone()
        if row is None or row[2] != "active":
            return None
        return {"description": row[0], "spec": row[1], "session_id": str(row[3])}

    async def _append(self, task_id: str, text: str) -> bool:
        async with self._connect() as conn:
            cur = await conn.execute(
                "INSERT INTO ai_background_events (task_id, text) SELECT %s, %s "
                "WHERE (SELECT count(*) FROM ai_background_events WHERE task_id = %s) < %s "
                "RETURNING seq",
                (task_id, text[:EVENT_MAX_CHARS], task_id, MAX_EVENTS),
            )
            return await cur.fetchone() is not None

    @activity.defn(name="bg_monitor_command")
    async def bg_monitor_command(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args["taskId"])
        task = await self._task(task_id)
        if task is None:
            return {"end": {"outcome": "cancelled"}, "events": 0}
        spec = task["spec"]
        description = str(task["description"])
        timeout_s = float(spec["timeoutMs"]) / 1000
        os.makedirs(self._cwd, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            str(spec["command"]),
            cwd=self._cwd,
            env=monitor_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        events = 0
        too_many = False
        handle = activity.client().get_workflow_handle(workflow_id(task["session_id"], task_id))

        async def emit(lines: list[str]) -> bool:
            nonlocal events
            if not await self._append(
                task_id, event_notice(task_id, description, "\n".join(lines))
            ):
                return False
            events += 1
            with contextlib.suppress(Exception):
                await handle.signal(EVENTS_SIGNAL)
            return True

        async def read() -> None:
            nonlocal too_many
            assert proc.stdout is not None
            splitter = LineSplitter()
            batch: list[str] = []
            chars = 0
            dropped = 0
            deadline: float | None = None
            loop = asyncio.get_running_loop()

            async def flush() -> bool:
                nonlocal batch, chars, dropped, deadline
                lines = batch + (
                    [f"[…{dropped} more line(s) in this batch cut]"] if dropped else []
                )
                batch, chars, dropped, deadline = [], 0, 0, None
                return not lines or await emit(lines)

            while True:
                wait = None if deadline is None else max(0.0, deadline - loop.time())
                try:
                    chunk: bytes | None = await asyncio.wait_for(proc.stdout.read(65536), wait)
                except TimeoutError:
                    chunk = None
                if chunk is None or chunk == b"":
                    if chunk == b"":
                        splitter.end()
                    for line in splitter.take():
                        batch.append(line)
                    if not await flush():
                        too_many = True
                        return
                    if chunk == b"":
                        return
                    continue
                splitter.push(chunk.decode("utf-8", "replace"))
                for line in splitter.take():
                    # One batch is one event: past EVENT_MAX_CHARS its lines are counted, not kept.
                    if chars + len(line) > EVENT_MAX_CHARS:
                        dropped += 1
                    else:
                        batch.append(line)
                        chars += len(line) + 1
                    if deadline is None:
                        deadline = loop.time() + BATCH_S

        async def beat() -> None:
            while True:
                activity.heartbeat()
                await asyncio.sleep(HEARTBEAT_S)

        def kill() -> None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

        beating = asyncio.create_task(beat())
        try:
            try:
                await asyncio.wait_for(read(), timeout_s)
            except TimeoutError:
                kill()
                await proc.wait()
                return {"end": {"outcome": "expired"}, "events": events}
            if too_many:
                kill()
                await proc.wait()
                return {"end": {"outcome": "too_many"}, "events": events}
            code = await proc.wait()
            end: dict[str, Any] = (
                {"outcome": "exited", "code": code}
                if code >= 0
                else {"outcome": "exited", "code": None, "signal": signal.Signals(-code).name}
            )
            return {"end": end, "events": events}
        except BaseException:
            # Cancelled, or an event that could not be written: the command never
            # outlives the watch.
            kill()
            raise
        finally:
            beating.cancel()
