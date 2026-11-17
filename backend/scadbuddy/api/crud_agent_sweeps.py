"""The agent's sweeps and a forget's own deletes behind CRUD routes (#5299, #6501): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`).

Area ``session-sweeps``: ``POST …/sessions/live``, which of a list of session ids still have an
``ai_sessions`` row: the agent's sweeps of its pod's directories (``temporal/turnServers.ts``
``sweepTurnDirs``, ``workspace/directory.ts`` ``SessionDirectory.sweep``) remove the rest.

Area ``forget-rows``: ``POST …/sessions/{id}/forget-rows``, the last step of a session's forget
(``sessions/forget.ts`` ``forgetSubject``): its transcript rows and its ``ai_sessions`` row
(whose cascade takes the rest), then the workspace bytes kept in Postgres that no file names
any more, and the warm-evict NOTIFY. Only for a session already forgotten: with no
``ai_forgotten_subjects`` row it answers 409 ``not-forgotten`` and deletes nothing, so this route
never forgets a session on its own.

The session id is the only id in a path, a UUID checked by its pattern. Every body is bounded.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

import psycopg
from fastapi import status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.agent_data.access import notify_warm_evict
from scadbuddy.api.crud_agent_events import UUID_PATTERN, Bounded, SessionIdPath
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")
logger = logging.getLogger(__name__)

#: The most ids one ``…/sessions/live`` asks about; the agent asks in pages of this many.
LIVE_IDS_MAX = 1000


class LiveAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[Annotated[str, Field(pattern=UUID_PATTERN)]] = Field(max_length=LIVE_IDS_MAX)


class Live(BaseModel):
    #: The ids asked about that have a row.
    ids: list[str]


def _live(state: Any, ids: list[str]) -> list[str]:
    with state.settings_store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id::text AS id FROM ai_sessions WHERE id = ANY(%s::uuid[])", (ids,)
        ).fetchall()
    return [r["id"] for r in rows]


@router.post("/sessions/live", name="crud_agent_sessions_live", dependencies=[Bounded])
async def live(body: LiveAsk, state: StateDep) -> Live:
    return Live(ids=await asyncio.to_thread(_live, state, body.ids) if body.ids else [])


class Removed(BaseModel):
    #: The transcript rows and the session row deleted.
    rows: int


class NotForgottenError(Exception):
    pass


_DROP_UNNAMED = (
    "DELETE FROM ai_session_file_bytes WHERE sha256 IN ("
    " SELECT b.sha256 FROM ai_session_file_bytes b WHERE b.sha256 = ANY(%s::text[])"
    " AND NOT EXISTS (SELECT 1 FROM ai_session_files f WHERE f.bytes_sha256 = b.sha256)"
    " FOR UPDATE SKIP LOCKED)"
)


def _forget_rows(state: Any, session_id: str) -> int:
    with state.settings_store.pool.connection() as conn:
        with conn.transaction():
            # The tombstone, held in the delete's transaction: no session is deleted unforgotten.
            forgotten = conn.execute(
                "SELECT 1 FROM ai_forgotten_subjects WHERE subject = %s FOR SHARE",
                (f"session-{session_id}",),
            ).fetchone()
            if forgotten is None:
                raise NotForgottenError
            shared = [
                r["bytes_sha256"]
                for r in conn.execute(
                    "SELECT DISTINCT bytes_sha256 FROM ai_session_files"
                    " WHERE session_id = %s::uuid AND bytes_sha256 IS NOT NULL",
                    (session_id,),
                ).fetchall()
            ]
            entries = conn.execute(
                "DELETE FROM ai_session_entries WHERE session_id = %s", (session_id,)
            ).rowcount
            sessions = conn.execute(
                "DELETE FROM ai_sessions WHERE id = %s::uuid", (session_id,)
            ).rowcount
        # A write naming the same bytes meanwhile fails this on the foreign key: the sweep
        # deletes them later (the agent's `dropUnnamedBytes`, statement for statement).
        if shared:
            try:
                with conn.transaction():
                    conn.execute(_DROP_UNNAMED, (shared,))
            except psycopg.Error as err:
                logger.warning(
                    "forget: unnamed workspace bytes left for the sweep (%s)", type(err).__name__
                )
        # A session's warm process, on whichever replica holds it (warmEvict.ts). Sent once the
        # rows' transaction has committed, not in it: the pool is autocommit (`core/pg_pool.py`
        # `row_pool`), so this NOTIFY is delivered as it runs.
        notify_warm_evict(conn, session_id)
    return int(entries + sessions)


@router.post("/sessions/{session_id}/forget-rows", name="crud_agent_session_forget_rows")
async def forget_rows(session_id: SessionIdPath, state: StateDep) -> Removed:
    try:
        rows = await asyncio.to_thread(_forget_rows, state, session_id)
    except NotForgottenError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"session {session_id} is not forgotten: forget its subject first",
            reason="not-forgotten",
        ) from None
    return Removed(rows=rows)
