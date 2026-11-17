"""The agent's /mcp session relay behind CRUD routes (#5299 step 11): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`). Each route is
one statement, or one transaction, of the agent's ``mcp/relayRows.ts`` ``PgRelayRows``, so the
relay keeps its design (#2086) and only its storage moves (#5299 D2). The agent uses them once
``…/areas`` lists ``mcp-relay``, and its own SQL until then (D9).

Area ``mcp-relay``:

- ``POST …/mcp-relay/sessions``: a session's directory row, written by the replica that holds it;
- ``POST …/mcp-relay/sessions/touch``, ``…/sessions/remove``, ``…/sessions/owner``: its
  ``last_seen`` moved, the row deleted (with ``replica``, only while that replica still holds
  it), and the replica that holds it;
- ``POST …/mcp-relay/counts``: the open sessions across every replica, and the caller's;
- ``POST …/mcp-relay/send``: a relay message, its body row (when it has one) inserted and its
  header NOTIFYed on ``scadbuddy_mcp`` in one transaction, so the row is readable before the
  header is heard;
- ``POST …/mcp-relay/rows/take`` and ``…/rows/withdraw``: a body row taken with
  ``DELETE … RETURNING`` by the replica it is for, or withdrawn by its sender;
- ``POST …/mcp-relay/sweep``: the rows nobody took and the directory rows of dead replicas.

A session id is a credential: the directory and every message carry its SHA-256 only, and
every id is in the body, never the path. A header is checked against the relay's own shapes and
refused 422 at the size Postgres refuses a NOTIFY payload, so nothing NOTIFYed here is one the
agent would not have sent. A body row is bounded by the API's JSON gate (`api/limits.py`
``MAX_TEXT_BODY_BYTES``, 8 MiB, refused before it is read). A relayed request is at most the MCP
SDK's 4 MiB body (``DEFAULT_MAX_REQUEST_BODY_SIZE``), but the row escapes it once more, so one
heavy in quotes, backslashes or control characters can pass 8 MiB. The agent measures the
encoded body first and answers such a request 413 itself (``relayRows.ts``
``RelayBodyTooLargeError``), so it never reaches this gate. An answer's chunk too large for the
NOTIFY goes as several rows of at most 2^20 UTF-16 units each (``sessionRelay.ts``
``ROW_TEXT_MAX``), under 6 MiB once escaped, so an answer of any size is relayed whole.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from fastapi import status
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

#: The channel the agent's relay LISTENs on (``mcp/sessionRelay.ts`` ``MCP_CHANNEL``).
MCP_CHANNEL = "scadbuddy_mcp"
#: Postgres refuses a NOTIFY payload of 8000 bytes or more (default build,
#: https://www.postgresql.org/docs/current/sql-notify.html).
NOTIFY_PAYLOAD_MAX = 7999
#: The agent's ``STALE_MESSAGES``: relay rows older than this are nobody's any more.
STALE_MESSAGES = "10 minutes"
#: A caller's key: a principal id, or ``anonymous@<client ip>``.
CALLER_KEY_MAX = 1024
#: The longest idle window a sweep or a count may name (a week).
IDLE_SECONDS_MAX = 7 * 24 * 3600
#: A header's own bounds: the answer's headers and an inline chunk's text.
HEADERS_MAX = 200
HEADER_NAME_MAX = 256
HEADER_VALUE_MAX = 8192
INLINE_TEXT_MAX = 8000

SHA256_HEX = r"^[0-9a-f]{64}$"
Hash = Annotated[str, Field(pattern=SHA256_HEX)]
CallerKey = Annotated[str, Field(min_length=1, max_length=CALLER_KEY_MAX)]
IdleSeconds = Annotated[float, Field(gt=0, le=IDLE_SECONDS_MAX)]
HeaderPair = tuple[
    Annotated[str, Field(max_length=HEADER_NAME_MAX)],
    Annotated[str, Field(max_length=HEADER_VALUE_MAX)],
]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _pool(state: Any) -> Any:
    return state.settings_store.pool


async def _in_tx[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    """``fn`` in one transaction on a pooled connection, off the event loop."""

    def run() -> T:
        with _pool(state).connection() as conn, conn.transaction():
            return fn(conn)

    return await asyncio.to_thread(run)


class Done(BaseModel):
    done: bool


# -- the directory ------------------------------------------------------------------------


class Register(_Body):
    hash: Hash
    replica: UUID
    caller_key: CallerKey


@router.post("/mcp-relay/sessions", name="crud_agent_mcp_relay_register")
async def register(body: Register, state: StateDep) -> Done:
    await _in_tx(
        state,
        lambda conn: conn.execute(
            "INSERT INTO ai_mcp_sessions (id_hash, replica, caller_key) VALUES (%s, %s, %s)"
            " ON CONFLICT (id_hash) DO UPDATE SET replica = EXCLUDED.replica,"
            " caller_key = EXCLUDED.caller_key, last_seen = now()",
            (body.hash, body.replica, body.caller_key),
        ),
    )
    return Done(done=True)


class SessionHash(_Body):
    hash: Hash


@router.post("/mcp-relay/sessions/touch", name="crud_agent_mcp_relay_touch")
async def touch(body: SessionHash, state: StateDep) -> Done:
    await _in_tx(
        state,
        lambda conn: conn.execute(
            "UPDATE ai_mcp_sessions SET last_seen = now() WHERE id_hash = %s", (body.hash,)
        ),
    )
    return Done(done=True)


class Remove(_Body):
    hash: Hash
    #: Only while this replica still holds it: a gone owner's row, never a new owner's.
    replica: UUID | None = None


@router.post("/mcp-relay/sessions/remove", name="crud_agent_mcp_relay_remove")
async def remove(body: Remove, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        if body.replica is None:
            cursor = conn.execute(
                "DELETE FROM ai_mcp_sessions WHERE id_hash = %s RETURNING id_hash", (body.hash,)
            )
        else:
            cursor = conn.execute(
                "DELETE FROM ai_mcp_sessions WHERE id_hash = %s AND replica = %s RETURNING id_hash",
                (body.hash, body.replica),
            )
        return cursor.fetchone() is not None

    return Done(done=await _in_tx(state, run))


class Owner(BaseModel):
    replica: str | None


@router.post("/mcp-relay/sessions/owner", name="crud_agent_mcp_relay_owner")
async def owner(body: SessionHash, state: StateDep) -> Owner:
    row = await _in_tx(
        state,
        lambda conn: conn.execute(
            "SELECT replica::text AS replica FROM ai_mcp_sessions WHERE id_hash = %s", (body.hash,)
        ).fetchone(),
    )
    return Owner(replica=None if row is None else row["replica"])


class CountsAsk(_Body):
    caller_key: CallerKey
    idle_seconds: IdleSeconds


class Counts(BaseModel):
    total: int
    caller: int


@router.post("/mcp-relay/counts", name="crud_agent_mcp_relay_counts")
async def counts(body: CountsAsk, state: StateDep) -> Counts:
    row = await _in_tx(
        state,
        lambda conn: conn.execute(
            "SELECT count(*)::int AS total,"
            " (count(*) FILTER (WHERE caller_key = %s))::int AS caller"
            " FROM ai_mcp_sessions WHERE last_seen > now() - make_interval(secs => %s)",
            (body.caller_key, body.idle_seconds),
        ).fetchone(),
    )
    return Counts(total=row["total"], caller=row["caller"]) if row else Counts(total=0, caller=0)


class SweepAsk(_Body):
    idle_seconds: IdleSeconds


@router.post("/mcp-relay/sweep", name="crud_agent_mcp_relay_sweep")
async def sweep(body: SweepAsk, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> None:
        conn.execute(
            "DELETE FROM ai_mcp_relay_messages WHERE created_at < now() - %s::interval",
            (STALE_MESSAGES,),
        )
        # A dead replica's sessions: idle by now, since nothing could use them.
        conn.execute(
            "DELETE FROM ai_mcp_sessions WHERE last_seen < now() - make_interval(secs => %s)",
            (body.idle_seconds,),
        )

    await _in_tx(state, run)
    return Done(done=True)


# -- the messages -------------------------------------------------------------------------


class _Header(_Body):
    req: UUID
    to: UUID


class ReqHeader(_Header):
    t: Literal["req"]
    row: UUID


class AckHeader(_Header):
    t: Literal["ack"]


class BeatHeader(_Header):
    t: Literal["beat"]


class HeadHeader(_Header):
    t: Literal["head"]
    status: Annotated[int, Field(ge=100, le=599)]
    headers: Annotated[list[HeaderPair], Field(max_length=HEADERS_MAX)]


class ChunkHeader(_Header):
    t: Literal["chunk"]
    text: Annotated[str, Field(max_length=INLINE_TEXT_MAX)] | None = None
    row: UUID | None = None


class EndHeader(_Header):
    t: Literal["end"]
    error: bool | None = None


class CancelHeader(_Header):
    t: Literal["cancel"]


Header = Annotated[
    ReqHeader | AckHeader | BeatHeader | HeadHeader | ChunkHeader | EndHeader | CancelHeader,
    Field(discriminator="t"),
]


class Send(_Body):
    header: Header
    #: The row the header names (``row``), when it has one: a request, or a chunk too large to
    #: ride in the NOTIFY.
    body: dict[str, Any] | None = None


def _payload(header: Any) -> str:
    """The header as the agent's relay reads it (``HeaderSchema``), ids as text."""
    # ensure_ascii=False: the agent sizes an inline chunk by its UTF-8 JSON, which is what
    # Postgres counts too; \uXXXX escapes would make that chunk too large here.
    return json.dumps(
        header.model_dump(mode="json", exclude_none=True), separators=(",", ":"), ensure_ascii=False
    )


@router.post("/mcp-relay/send", name="crud_agent_mcp_relay_send")
async def send(body: Send, state: StateDep) -> Done:
    header = body.header
    row: UUID | None = getattr(header, "row", None)
    if (row is None) != (body.body is None):
        raise ApiError(422, "a body is sent with the header that names its row, and only then")
    payload = _payload(header)
    if len(payload.encode()) > NOTIFY_PAYLOAD_MAX:
        raise ApiError(422, f"a relay header is at most {NOTIFY_PAYLOAD_MAX} bytes")

    def run(conn: psycopg.Connection[Any]) -> None:
        if row is not None:
            conn.execute(
                "INSERT INTO ai_mcp_relay_messages (id, body) VALUES (%s, %s)",
                (row, Jsonb(body.body)),
            )
        conn.execute("SELECT pg_notify(%s, %s)", (MCP_CHANNEL, payload))

    try:
        await _in_tx(state, run)
    except psycopg.errors.UniqueViolation:
        raise ApiError(status.HTTP_409_CONFLICT, "a relay message with this row exists") from None
    return Done(done=True)


class MessageRow(_Body):
    row: UUID


class Taken(BaseModel):
    body: dict[str, Any] | None


@router.post("/mcp-relay/rows/take", name="crud_agent_mcp_relay_take")
async def take(body: MessageRow, state: StateDep) -> Taken:
    row = await _in_tx(
        state,
        lambda conn: conn.execute(
            "DELETE FROM ai_mcp_relay_messages WHERE id = %s RETURNING body", (body.row,)
        ).fetchone(),
    )
    return Taken(body=None if row is None else row["body"])


@router.post("/mcp-relay/rows/withdraw", name="crud_agent_mcp_relay_withdraw")
async def withdraw(body: MessageRow, state: StateDep) -> Done:
    row = await _in_tx(
        state,
        lambda conn: conn.execute(
            "DELETE FROM ai_mcp_relay_messages WHERE id = %s RETURNING id", (body.row,)
        ).fetchone(),
    )
    return Done(done=row is not None)
