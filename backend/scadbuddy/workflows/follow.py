"""``FollowPrint``: one print followed until it settles (#1053, spec 2026-10-01 §4.4).

It replaces the API process's watcher tasks (#268): Temporal keeps the follow across a
restart, so nothing records prints to resume or locks one per replica. The reads are
one heartbeating activity (`bambuddy/follow.py`), on the ``bambuddy`` queue.

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
#: Bambuddy down for a while is the attempt's own error interval; a crash retries.
FOLLOW_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5), maximum_interval=timedelta(minutes=1)
)
#: The progress route's start or poke never holds its read up for long.
RPC_TIMEOUT = timedelta(seconds=5)


def follow_id(output_id: str) -> str:
    return f"follow-print-{output_id}"


@workflow.defn(name=FOLLOW_WORKFLOW)
class FollowPrint:
    def __init__(self) -> None:
        self.poked = False

    @workflow.signal(name=POKE_SIGNAL)
    def poke(self) -> None:
        self.poked = True

    @workflow.run
    async def run(self, output_id: str) -> str:
        fresh = False
        while True:
            self.poked = False
            attempt: workflow.ActivityHandle[str] = workflow.start_activity(
                FOLLOW_ACTIVITY,
                FollowInput(output_id=output_id, fresh=fresh),
                result_type=str,
                # A print that keeps moving is followed past `MAX_AGE`; a quiet one
                # ends within it, so a day more is the attempt's outer bound.
                start_to_close_timeout=MAX_AGE * 2,
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


async def follow(client: Client, task_queue: str, output_id: str) -> None:
    """Make sure ``output_id``'s print is followed: start its `FollowPrint` unless one
    is running. It never pokes: only a new print does (`PrintRun`), and a poke on every
    read would restart the attempt each time. Best effort: the caller's read has
    answered either way."""
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


def _recent_watches(pool: ConnectionPool[Any], cutoff: datetime) -> list[str]:
    with pool.connection() as conn, conn.transaction():
        rows = conn.execute("DELETE FROM print_watches RETURNING output_id, printed_at").fetchall()
    return [row["output_id"] for row in rows if row["printed_at"] >= cutoff]


async def resume_followed(
    pool: ConnectionPool[Any], client: Client, task_queue: str, now: datetime
) -> list[str]:
    """Once, after the upgrade from the in-process watcher (#268): follow on Temporal
    the prints it recorded within `MAX_AGE`, and empty its `print_watches` log, which
    nothing writes any more."""
    recent = await asyncio.to_thread(_recent_watches, pool, now - MAX_AGE)
    for output_id in recent:
        await follow(client, task_queue, output_id)
    return recent
