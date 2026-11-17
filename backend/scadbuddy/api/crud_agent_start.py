"""What the agent read at its start and per request over its own SQL, behind CRUD routes
(#5299, #6501): under ``/api/v1/internal/crud/agent/``, for the ``agent`` credential only
(`crud_router`).

Area ``migrations``: ``POST …/migrations/ledger``, the rows of ``ai_migrations`` for the files
the agent's build expects (``agent/src/db/schema.ts`` ``readLedger``): whether the ledger
exists, whether it is keyed by file id (#491), and each expected file's checksum. The API
applies the agent's migrations at its start (#5299 D6); the agent waits until every file it
expects is in the ledger.

Area ``auth-sessions``: ``POST …/auth-sessions/lookup``, the live signed-in session whose
cookie hashes to ``id_hash`` (``agent/src/auth/browserSession.ts``), as the view
``auth_sessions_agent`` answers it: its principal, name and kind, nothing else. Only the
cookie's SHA-256 travels, in the body, never in a path or a query.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.crud_agent_events import Bounded
from scadbuddy.api.deps import StateDep
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

#: An agent migration's id (root CLAUDE.md "Migrations (#491)").
MIGRATION_ID_PATTERN = r"^[0-9]{8}T[0-9]{4}Z_[a-z0-9_]{1,100}$"
#: Far past the agent's files (100 on 2026-11-16).
MIGRATION_IDS_MAX = 5000


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LedgerAsk(_Body):
    ids: list[Annotated[str, Field(pattern=MIGRATION_ID_PATTERN)]] = Field(
        max_length=MIGRATION_IDS_MAX
    )


class LedgerRow(BaseModel):
    id: str
    checksum: str | None


class Ledger(BaseModel):
    #: Whether ``ai_migrations`` exists.
    found: bool
    #: Whether it is keyed by file id; false while it is still positional (before #491).
    keyed: bool
    rows: list[LedgerRow]


def _ledger(state: Any, ids: list[str]) -> Ledger:
    with state.settings_store.pool.connection() as conn:
        head = conn.execute(
            "SELECT to_regclass('ai_migrations') IS NOT NULL AS found,"
            " EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('ai_migrations')"
            " AND attname = 'id' AND NOT attisdropped) AS keyed"
        ).fetchone()
        found, keyed = bool(head and head["found"]), bool(head and head["keyed"])
        if not found or not keyed:
            return Ledger(found=found, keyed=keyed, rows=[])
        rows = conn.execute(
            "SELECT id, checksum FROM ai_migrations WHERE id = ANY(%s::text[])", (ids,)
        ).fetchall()
    return Ledger(found=True, keyed=True, rows=[LedgerRow.model_validate(r) for r in rows])


@router.post("/migrations/ledger", name="crud_agent_migrations_ledger", dependencies=[Bounded])
async def ledger(body: LedgerAsk, state: StateDep) -> Ledger:
    return await asyncio.to_thread(_ledger, state, body.ids)


class SessionAsk(_Body):
    #: The session cookie's SHA-256, lowercase hex.
    id_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PersonSession(BaseModel):
    principal: str
    name: str | None
    kind: str


class AuthSessionAnswer(BaseModel):
    #: None for no live session.
    session: PersonSession | None


def _session(state: Any, id_hash: str) -> PersonSession | None:
    with state.settings_store.pool.connection() as conn:
        row = conn.execute(
            "SELECT principal, name, kind FROM auth_sessions_agent WHERE id_hash = %s",
            (bytes.fromhex(id_hash),),
        ).fetchone()
    return None if row is None else PersonSession.model_validate(row)


@router.post("/auth-sessions/lookup", name="crud_agent_auth_session_lookup", dependencies=[Bounded])
async def lookup(body: SessionAsk, state: StateDep) -> AuthSessionAnswer:
    return AuthSessionAnswer(session=await asyncio.to_thread(_session, state, body.id_hash))
