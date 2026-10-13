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


def workflow_id(task_id: str) -> str:
    return f"bgtask-{task_id}"


def event_notice(task_id: str, description: str, text: str) -> str:
    """One event, as agent/src/background/monitor.ts ``eventNotice`` writes it."""
    return (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n"
        f'<summary>Monitor event: "{description}"</summary>\n'
        f"<event>{text}</event>\n</task-notification>"
    )


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
                "SELECT description, spec, state FROM ai_background_tasks WHERE id = %s", (task_id,)
            )
            row = await cur.fetchone()
        if row is None or row[2] != "active":
            return None
        return {"description": row[0], "spec": row[1]}

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
        handle = activity.client().get_workflow_handle(workflow_id(task_id))

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
            batch: list[str] = []
            deadline: float | None = None
            loop = asyncio.get_running_loop()
            while True:
                wait = None if deadline is None else max(0.0, deadline - loop.time())
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), wait)
                except TimeoutError:
                    raw = None
                if raw is None or (raw == b"" and batch):
                    if batch and not await emit(batch):
                        too_many = True
                        return
                    batch, deadline = [], None
                    if raw is None:
                        continue
                if raw == b"":
                    return
                batch.append(raw.decode("utf-8", "replace").rstrip("\n"))
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
        except asyncio.CancelledError:
            kill()
            raise
        finally:
            beating.cancel()
