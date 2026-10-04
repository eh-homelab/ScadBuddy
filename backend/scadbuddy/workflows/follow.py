"""``FollowPrint``: one print followed until it settles (#1053, spec 2026-10-01 §4.4).

It replaces the API process's watcher tasks (#268): Temporal keeps the follow across a
restart, so nothing records prints to resume or locks one per replica. The reads are
one heartbeating activity (`bambuddy/follow.py`), on a queue of its own beside the
``bambuddy`` one (`follow_queue`).

The ``poke`` signal is a new print of the output (`PrintRun`): the running attempt is
cancelled and a fresh one reads at once, with its age from now. A progress read only
makes sure a follow is running (`follow`).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from psycopg_pool import ConnectionPool
from temporalio import workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from scadbuddy.bambuddy.follow import FOLLOW_ACTIVITY, MAX_AGE, FollowInput

logger = logging.getLogger(__name__)

FOLLOW_WORKFLOW = "FollowPrint"
POKE_SIGNAL = "poke"
#: Each wait heartbeats at least every `HEARTBEAT_SLICE` (5 s).
FOLLOW_HEARTBEAT = timedelta(seconds=30)
#: Bambuddy down for a while is the attempt's own error interval; a crash or a
#: timeout retries, without end.
FOLLOW_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5), maximum_interval=timedelta(minutes=1)
)
#: Not a bound on the follow: a print that keeps moving past it (a long print, a
#: multi-plate queue) times the attempt out, and `FOLLOW_RETRY` starts the next, which
#: resumes the age its heartbeat carried. Only `MAX_AGE` without a change, counted
#: from that age, ends a follow (or the print settling, or going).
ATTEMPT_TIMEOUT = MAX_AGE * 2
#: Each poke adds an attempt's events to the history: past this many, or when the
#: server suggests it, the follow continues as new (with a fresh attempt).
MAX_POKES = 100
#: The progress route's start or poke never holds its read up for long.
RPC_TIMEOUT = timedelta(seconds=5)


def follow_id(output_id: str) -> str:
    return f"follow-print-{output_id}"


def follow_queue(task_queue: str) -> str:
    """Where ``follow_print`` runs, beside ``task_queue`` (``bambuddy``) on a worker of
    its own: a follow holds its slot for hours, and must never take one a ``PrintRun``
    or an ``Operation`` is waiting for (review #1091 1)."""
    return f"{task_queue}-follow"


@workflow.defn(name=FOLLOW_WORKFLOW)
class FollowPrint:
    def __init__(self) -> None:
        self.poked = False

    @workflow.signal(name=POKE_SIGNAL)
    def poke(self) -> None:
        self.poked = True

    @workflow.run
    async def run(self, output_id: str, fresh: bool = False) -> str:
        pokes = 0
        while True:
            self.poked = False
            attempt: workflow.ActivityHandle[str] = workflow.start_activity(
                FOLLOW_ACTIVITY,
                FollowInput(output_id=output_id, fresh=fresh),
                result_type=str,
                task_queue=follow_queue(workflow.info().task_queue),
                start_to_close_timeout=ATTEMPT_TIMEOUT,
                heartbeat_timeout=FOLLOW_HEARTBEAT,
                retry_policy=FOLLOW_RETRY,
            )
            await workflow.wait_condition(lambda: self.poked or attempt.done())  # noqa: B023
            if attempt.done() and not self.poked:
                ended: str = await attempt
                return ended
            # TRY_CANCEL: the old attempt hears of it at its next heartbeat (throttled to
            # most of `FOLLOW_HEARTBEAT`); the fresh one reads now. A change both see is
            # published once (`ProgressObserver` dedupes).
            attempt.cancel()
            with suppress(ActivityError, asyncio.CancelledError):
                await attempt
            fresh = True
            pokes += 1
            if pokes >= MAX_POKES or workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(args=[output_id, fresh])


async def follow(client: Client, task_queue: str, output_id: str) -> bool:
    """Make sure ``output_id``'s print is followed: start its `FollowPrint` unless one
    is running. It never pokes: only a new print does (`PrintRun`), and a poke on every
    read would restart the attempt each time. Best effort: the caller's read has
    answered either way. True when a follow is running."""
    try:
        # The outer bound is for a lazy client's first connect, which retries for minutes.
        async with asyncio.timeout(RPC_TIMEOUT.total_seconds() + 2):
            await client.start_workflow(
                FOLLOW_WORKFLOW,
                output_id,
                id=follow_id(output_id),
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                rpc_timeout=RPC_TIMEOUT,
            )
    except WorkflowAlreadyStartedError:
        pass
    except Exception:
        logger.warning("could not follow a print", extra={"output_id": output_id}, exc_info=True)
        return False
    return True


def _recent_watches(pool: ConnectionPool[Any], cutoff: datetime) -> list[str]:
    with pool.connection() as conn:
        found = conn.execute("SELECT to_regclass('print_watches') AS t").fetchone()
        if found is None or found["t"] is None:
            return []
        with conn.transaction():
            conn.execute("DELETE FROM print_watches WHERE printed_at < %s", (cutoff,))
            rows = conn.execute("SELECT output_id FROM print_watches").fetchall()
    return [row["output_id"] for row in rows]


def _forget_watches(pool: ConnectionPool[Any], output_ids: list[str]) -> None:
    with pool.connection() as conn:
        conn.execute("DELETE FROM print_watches WHERE output_id = ANY(%s)", (output_ids,))


async def resume_followed(
    pool: ConnectionPool[Any], client: Client, task_queue: str, now: datetime
) -> list[str]:
    """On each boot after the upgrade from the in-process watcher (#268): follow on
    Temporal the prints it recorded within `MAX_AGE`, and empty its `print_watches` log,
    which nothing writes any more. A row leaves only once its follow is running, so one
    that did not start waits for the next boot. The migration that drops the table
    removes this call."""
    recent = await asyncio.to_thread(_recent_watches, pool, now - MAX_AGE)
    followed = [output_id for output_id in recent if await follow(client, task_queue, output_id)]
    if followed:
        await asyncio.to_thread(_forget_watches, pool, followed)
    return followed
