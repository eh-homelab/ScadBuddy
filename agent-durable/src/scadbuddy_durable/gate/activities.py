"""``open_input`` and ``resolve_input`` as activities on the ``agent`` queue (spec §6.6).

DurableSession (phase 5c) runs them: ``open_input`` once the plugin's
``pending_approvals()`` gains a call, ``resolve_input`` before every ``agent.decide``.
How it retries ``resolve_input`` depends on who waits: on the ``respond`` path a
person waits on the Update's answer, so the activity is bounded at 30 s; on the timer
path nobody waits, so it retries with no deadline (backoff capped at 5 minutes) until
Postgres takes the write, and the entry stays *resolving* meanwhile.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any

import psycopg
from temporalio import activity
from temporalio.common import RetryPolicy

from scadbuddy_durable.gate.store import OpenInput, ResolveInput, open_input, resolve_input

Connect = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]]

# workflow.execute_activity(..., **RESOLVE_ON_RESPOND) on the respond path.
RESOLVE_ON_RESPOND: dict[str, Any] = {"schedule_to_close_timeout": timedelta(seconds=30)}
# The timer path: start_to_close bounds one attempt, the retries have no end.
RESOLVE_ON_TIMER: dict[str, Any] = {
    "start_to_close_timeout": timedelta(seconds=30),
    "retry_policy": RetryPolicy(maximum_interval=timedelta(minutes=5), maximum_attempts=0),
}


class GateActivities:
    """The gate's writers, over the connection ``connect`` gives (5c's pool)."""

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    @activity.defn(name="open_input")
    async def open_input(self, args: OpenInput) -> bool:
        async with self._connect() as conn:
            return await open_input(conn, args)

    @activity.defn(name="resolve_input")
    async def resolve_input(self, args: ResolveInput) -> bool:
        async with self._connect() as conn:
            return await resolve_input(conn, args)
