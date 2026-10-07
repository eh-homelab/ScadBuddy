"""The session's committed messages (`ai_durable_inputs`), which the workflow runs (#1056).

The agent service commits each message here before it asks Temporal anything; the
`send_message` Update only nudges the run with the id. A run loads the pending messages
when it starts and when nudged (LOAD_INPUTS), and takes each one with a compare-and-set
just before its turn starts (START_INPUT), so a message a Stop abandoned meanwhile never
runs, and one that ran never runs again, whichever run (or restore) asks. A take a Stop
cut short is released (RELEASE_INPUT): abandoned, and logged as interrupted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from psycopg_pool import AsyncConnectionPool
from temporalio import activity

from scadbuddy_durable.models import (
    LOAD_INPUTS,
    RELEASE_INPUT,
    START_INPUT,
    STOPPED_BEFORE_IT_RAN,
    Loaded,
    LoadInputs,
    Message,
    ReleaseInput,
    StartInput,
)
from scadbuddy_durable.projector import log_events, notify_events
from scadbuddy_durable.translate import PROTOCOL_VERSION

# The bus `replica` of what this module logs: no agent replica's own.
REPLICA = "agent-durable-inputs"

# Taken by `token`'s take, or not taken yet.
_TAKEABLE = "(status = 'pending' OR (status = 'run' AND taken_by = %(token)s))"


class Inputs:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def load(self, inp: LoadInputs) -> Loaded:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT id, text, context, note FROM ai_durable_inputs"
                " WHERE session_id = %s AND status = 'pending' ORDER BY seq",
                (inp.session_id,),
            )
            pending = [Message(str(r[0]), r[1], r[2], r[3]) for r in await cur.fetchall()]
            status: str | None = None
            if inp.asked is not None:
                cur = await conn.execute(
                    "SELECT status FROM ai_durable_inputs WHERE id = %s AND session_id = %s",
                    (inp.asked, inp.session_id),
                )
                row = await cur.fetchone()
                status = None if row is None else str(row[0])
        return Loaded(pending, status)

    async def start(self, inp: StartInput) -> bool:
        """Takes a pending message for its turn; True when it is this take's to run.

        A retry of the same take (its first answer lost) finds the message taken with its
        token and answers True again; any other take of a message already `run` is
        refused, so no reordering of loads and takes runs a message twice.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE ai_durable_inputs SET status = 'run', taken_by = %(token)s"
                f" WHERE id = %(id)s AND session_id = %(sid)s AND {_TAKEABLE} RETURNING id",
                {"id": inp.id, "sid": inp.session_id, "token": inp.token},
            )
            return await cur.fetchone() is not None

    async def release(self, inp: ReleaseInput) -> bool:
        """A take a Stop cut short: the message never starts. Abandons it, if it is still
        pending or taken by this take, and logs the `interrupted` error the agent service
        logs for the messages its Stop abandoned, in one transaction. False when it was
        not this take's (the Stop's own abandon logged it)."""
        events: list[dict[str, Any]] = [
            {
                "v": PROTOCOL_VERSION,
                "type": "error",
                "sessionId": inp.session_id,
                "code": "interrupted",
                "message": STOPPED_BEFORE_IT_RAN,
            }
        ]
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "UPDATE ai_durable_inputs SET status = 'abandoned'"
                    f" WHERE id = %(id)s AND session_id = %(sid)s AND {_TAKEABLE} RETURNING id",
                    {"id": inp.id, "sid": inp.session_id, "token": inp.token},
                )
                if await cur.fetchone() is None:
                    return False
                seq = await log_events(conn, inp.session_id, events)
            await notify_events(conn, inp.session_id, events, seq, REPLICA)
        return True


def make_input_activities(inputs: Inputs) -> list[Callable[..., Awaitable[Any]]]:
    """LOAD_INPUTS, START_INPUT and RELEASE_INPUT bound to `inputs`, for the worker."""

    @activity.defn(name=LOAD_INPUTS)
    async def load_inputs(inp: LoadInputs) -> Loaded:
        return await inputs.load(inp)

    @activity.defn(name=START_INPUT)
    async def start_input(inp: StartInput) -> bool:
        return await inputs.start(inp)

    @activity.defn(name=RELEASE_INPUT)
    async def release_input(inp: ReleaseInput) -> bool:
        return await inputs.release(inp)

    return [load_inputs, start_input, release_input]
