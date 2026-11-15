"""A session's transcript behind CRUD routes (#5299 step 9, part b): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`), area
``transcripts``. Each route is one method of the agent's transcript store
(``agent/src/sessions/store.ts`` `TranscriptRows`) and runs its SQL, a transaction as one
transaction (#5299 D2); the SQL is in ``agent_data/transcripts.py``. The API seals and opens
every entry with the session's data key (D5): the agent sends and gets each entry's JSON text.

- ``POST …/sessions/{id}/entries``: an append, in one transaction under the session's
  payload-key lock (shared), refused ``forgotten`` (410) once the session is forgotten, and,
  with ``fence``, only while the row still names that turn placement or no turn
  (``written: false`` otherwise, ``turnHost.ts`` ``holdsOrLast``);
- ``GET …/sessions/{id}/transcript``: the main transcript as it loads, a fork's through its
  chain in one snapshot (``entries: null`` for none);
- ``GET …/sessions/{id}/entries`` (``subpath``), ``…/entries/last``, ``…/cwd``,
  ``…/subkeys``, ``…/transcript/head``, ``…/entries/landed`` and ``POST
  …/transcript/moved``: the reads; ``POST …/entries/delete``: a delete;
- ``GET …/transcripts`` (``project_key``): the sessions of a project and their mtimes.

The session id is the only id in a path, a UUID checked by its pattern; a subpath and a
project key travel in the query or the body. Every body is bounded. An append is safe to send
again: a row with a uuid is not written twice (the SDK's idempotency key), and a row without
one is a new row each time, as over SQL.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Annotated, Any

import psycopg
from fastapi import Depends, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.agent_data import transcripts
from scadbuddy.agent_data.rotation import kek_at
from scadbuddy.agent_data.workspace import Keks
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
#: A subagent transcript's subpath and a project key (the SDK's sanitized cwd).
SUBPATH_MAX = 1024
PROJECT_KEY_MAX = 4096
#: One append's entries: the SDK hands over a batch per message; this is far above one.
ENTRIES_MAX = 1000
#: One entry's JSON text, and one append's body. Claude Code writes a large tool result to a
#: file of its own and keeps a pointer in the transcript; an image a user sends is at most
#: ``IMAGE_BYTES_MAX`` (3.75 MB) as base64. The agent splits a batch to stay under the body's.
ENTRY_CHARS_MAX = 16 * 1024 * 1024
BODY_BYTES_MAX = 32 * 1024 * 1024
#: The uuids a warm process wrote itself, which ``moved`` leaves out (the agent's ``own``);
#: past this the agent recycles the process instead of asking.
OWN_MAX = 20_000
UUID_MAX = 256

SessionIdPath = Annotated[str, Path(pattern=UUID_PATTERN)]


async def _bounded(request: Request) -> None:
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        raise ApiError(status.HTTP_411_LENGTH_REQUIRED, "a body names its Content-Length")
    if int(length) > BODY_BYTES_MAX:
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE, f"a body is at most {BODY_BYTES_MAX} bytes"
        )


Bounded = Depends(_bounded)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _keks(state: Any) -> Keks:
    return Keks(
        kek_at(state.settings.secret_key_file), kek_at(state.settings.secret_key_previous_file)
    )


async def _in_conn[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    def run() -> T:
        with state.settings_store.pool.connection() as conn:
            return fn(conn)

    try:
        return await asyncio.to_thread(run)
    except transcripts.SubjectForgottenError as err:
        raise ApiError(status.HTTP_410_GONE, str(err), reason="forgotten") from None
    except transcripts.KeyUnavailableError as err:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, str(err), reason="unavailable"
        ) from None


class Fence(_Body):
    attempt: str = Field(min_length=1, max_length=500)
    turn_id: str = Field(min_length=1, max_length=500)


class Append(_Body):
    project_key: str = Field(max_length=PROJECT_KEY_MAX)
    subpath: str = Field(max_length=SUBPATH_MAX)
    entries: list[Annotated[str, Field(max_length=ENTRY_CHARS_MAX)]] = Field(
        min_length=1, max_length=ENTRIES_MAX
    )
    fence: Fence | None = None


class Written(BaseModel):
    #: False when the fence refused it.
    written: bool


@router.post(
    "/sessions/{session_id}/entries", name="crud_agent_transcript_append", dependencies=[Bounded]
)
async def append(session_id: SessionIdPath, body: Append, state: StateDep) -> Written:
    for text in body.entries:
        try:
            json.loads(text)
        except ValueError:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "an entry is not JSON") from None
    fence = (
        None if body.fence is None else transcripts.Fence(body.fence.attempt, body.fence.turn_id)
    )
    keks = _keks(state)
    written = await _in_conn(
        state,
        lambda conn: transcripts.append(
            conn, session_id, body.project_key, body.subpath, body.entries, fence, keks
        ),
    )
    return Written(written=written)


class Entries(BaseModel):
    #: Each entry's JSON text, in order; null for a transcript never written.
    entries: list[str] | None


@router.get("/sessions/{session_id}/transcript", name="crud_agent_transcript")
async def load(session_id: SessionIdPath, state: StateDep) -> Entries:
    keks = _keks(state)
    texts = await _in_conn(state, lambda conn: transcripts.transcript(conn, session_id, keks))
    return Entries(entries=texts or None)


Subpath = Annotated[str, Query(min_length=1, max_length=SUBPATH_MAX)]


@router.get("/sessions/{session_id}/entries", name="crud_agent_transcript_subpath")
async def load_subpath(session_id: SessionIdPath, subpath: Subpath, state: StateDep) -> Entries:
    keks = _keks(state)
    texts = await _in_conn(
        state, lambda conn: transcripts.subpath_entries(conn, session_id, subpath, keks)
    )
    return Entries(entries=texts or None)


class Entry(BaseModel):
    entry: str | None


@router.get("/sessions/{session_id}/entries/last", name="crud_agent_transcript_last")
async def last_of_type(
    session_id: SessionIdPath,
    state: StateDep,
    entry_type: Annotated[str, Query(alias="type", pattern=transcripts.ENTRY_TYPE.pattern)],
    subpath: Annotated[str, Query(max_length=SUBPATH_MAX)] = "",
) -> Entry:
    keks = _keks(state)
    text = await _in_conn(
        state, lambda conn: transcripts.last_of_type(conn, session_id, subpath, entry_type, keks)
    )
    return Entry(entry=text)


class Cwd(BaseModel):
    cwd: str | None


@router.get("/sessions/{session_id}/cwd", name="crud_agent_transcript_cwd")
async def last_cwd(session_id: SessionIdPath, state: StateDep) -> Cwd:
    keks = _keks(state)
    return Cwd(cwd=await _in_conn(state, lambda conn: transcripts.last_cwd(conn, session_id, keks)))


class ProjectSession(BaseModel):
    session_id: str
    mtime: int


class ProjectSessions(BaseModel):
    sessions: list[ProjectSession]


@router.get("/transcripts", name="crud_agent_transcripts")
async def list_sessions(
    state: StateDep, project_key: Annotated[str, Query(max_length=PROJECT_KEY_MAX)]
) -> ProjectSessions:
    rows = await _in_conn(state, lambda conn: transcripts.list_sessions(conn, project_key))
    return ProjectSessions(sessions=[ProjectSession.model_validate(r) for r in rows])


class Delete(_Body):
    #: Null: the main transcript with its subagent transcripts.
    subpath: str | None = Field(max_length=SUBPATH_MAX)


class Done(BaseModel):
    ok: bool = True


@router.post(
    "/sessions/{session_id}/entries/delete",
    name="crud_agent_transcript_delete",
    dependencies=[Bounded],
)
async def delete(session_id: SessionIdPath, body: Delete, state: StateDep) -> Done:
    await _in_conn(state, lambda conn: transcripts.delete(conn, session_id, body.subpath))
    return Done()


class Subkeys(BaseModel):
    subkeys: list[str]


@router.get("/sessions/{session_id}/subkeys", name="crud_agent_transcript_subkeys")
async def subkeys(session_id: SessionIdPath, state: StateDep) -> Subkeys:
    return Subkeys(
        subkeys=await _in_conn(state, lambda conn: transcripts.subkeys(conn, session_id))
    )


class Head(BaseModel):
    #: Whether there is a main transcript to resume: rows of its own, or a fork's range.
    exists: bool
    #: Its own newest row with a uuid (a bigint as text), or null.
    since: str | None


@router.get("/sessions/{session_id}/transcript/head", name="crud_agent_transcript_head")
async def head(session_id: SessionIdPath, state: StateDep) -> Head:
    return Head.model_validate(
        await _in_conn(state, lambda conn: transcripts.head(conn, session_id))
    )


class MovedBody(_Body):
    since: str | None = Field(pattern=r"^[0-9]{1,19}$")
    own: list[Annotated[str, Field(max_length=UUID_MAX)]] = Field(max_length=OWN_MAX)


class Moved(BaseModel):
    moved: bool


@router.post(
    "/sessions/{session_id}/transcript/moved",
    name="crud_agent_transcript_moved",
    dependencies=[Bounded],
)
async def moved(session_id: SessionIdPath, body: MovedBody, state: StateDep) -> Moved:
    since = None if body.since is None else int(body.since)
    return Moved(
        moved=await _in_conn(
            state, lambda conn: transcripts.moved(conn, session_id, since, body.own)
        )
    )


class Landed(BaseModel):
    landed: bool


@router.get("/sessions/{session_id}/entries/landed", name="crud_agent_transcript_landed")
async def landed(
    session_id: SessionIdPath,
    state: StateDep,
    after: Annotated[int, Query(ge=0, le=2**63 - 1)],
) -> Landed:
    return Landed(
        landed=await _in_conn(state, lambda conn: transcripts.user_landed(conn, session_id, after))
    )
