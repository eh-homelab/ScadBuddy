"""The session's committed messages (`ai_durable_inputs`), which the workflow runs (#1056).

The agent service commits each message here before it asks Temporal anything; the
`send_message` Update only nudges the run with the id. A run loads the pending messages
when it starts and when nudged (LOAD_INPUTS), and takes each one with a compare-and-set
just before its turn starts (START_INPUT), so a message a Stop abandoned meanwhile never
runs, and one that ran never runs again, whichever run (or restore) asks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from psycopg_pool import AsyncConnectionPool
from temporalio import activity

from scadbuddy_durable.models import LOAD_INPUTS, START_INPUT, Loaded, LoadInputs, Message, StartInput


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
        """Takes a pending message for its turn; True when it is this run's to run.

        A retry after the take committed finds it `run` and answers True again: two runs
        of one workflow ID never run at once, and a restore loads only pending messages,
        so nothing else asks about a message once it ran.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE ai_durable_inputs SET status = 'run'"
                " WHERE id = %s AND session_id = %s AND status IN ('pending', 'run') RETURNING id",
                (inp.id, inp.session_id),
            )
            return await cur.fetchone() is not None


def make_input_activities(
    inputs: Inputs,
) -> tuple[Callable[[LoadInputs], Awaitable[Loaded]], Callable[[StartInput], Awaitable[bool]]]:
    """LOAD_INPUTS and START_INPUT bound to `inputs`, for the worker to register."""

    @activity.defn(name=LOAD_INPUTS)
    async def load_inputs(inp: LoadInputs) -> Loaded:
        return await inputs.load(inp)

    @activity.defn(name=START_INPUT)
    async def start_input(inp: StartInput) -> bool:
        return await inputs.start(inp)

    return load_inputs, start_input
