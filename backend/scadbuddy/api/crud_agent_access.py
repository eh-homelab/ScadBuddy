"""The agent's tool access, tool listings, leases and plugin sessions behind its CRUD routes
(#5299 step 5): under ``/api/v1/internal/crud/agent/``, for the ``agent`` credential only
(`crud_router`). Each route is one of the agent store's methods and runs what that method
ran over SQL, a transaction as one transaction (#5299 D2).

- area ``access`` (``agent/src/access/store.ts`` ``AccessStore``): the rows of some sources
  (``POST …/access/rows``), one row (``GET …/access/row``), and a write
  (``POST …/access/write``): the warm process cache's eviction and the edits in one
  transaction, and with ``skip_unchanged`` the rows read ``FOR UPDATE`` first and an edit
  that changes nothing dropped. The agent checks each source and item before it calls, and
  writes the audit row itself;
- also area ``access`` (``access/listings.ts`` ``ToolListings``): the listings of some sources
  (``POST …/tool-listings/read``), and a connection test's listing
  (``POST …/tool-listings/plugin``, ``…/tool-listings/package-server``), stored under the
  plugin's or the package's row lock only while the plugin still stands at the URL tested
  (or the package at the content hash listed) and only while the caller's lease
  ``tool-listing:<source>`` holds, else 409 ``lease-lost``;
- also area ``access``, the leases (#5299 D3, `agent_data/leases.py`):
  ``POST …/leases/{key}`` takes one for ``token`` (waiting on the API up to ``wait_s``) or
  answers 409 ``busy``, ``PUT`` renews it or answers 409 ``lease-lost``, ``DELETE``
  releases it. Each is natural on the token. The ``plugins`` area's token refresh takes
  them too;
- area ``plugin-sessions`` (``plugins/pluginSessions.ts``): an upstream MCP session seen and
  ended, the database's clock as the start mark, and the sweep's two reads.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Annotated, Any, Literal
from uuid import UUID

import psycopg
from fastapi import Path, Query, status
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from scadbuddy.agent_data import leases
from scadbuddy.agent_data.access import (
    SOURCE_ITEM,
    forget_source,
    notify_warm_evict,
    package_server_source,
    remote_source,
    write_tools,
)
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

#: ``access/store.ts`` ``SOURCE_RE``.
SOURCE_PATTERN = (
    r"^(builtin:(scadbuddy|browser|http|own-plugin|claude-code)|remote:[a-z][a-z0-9-]*"
    r"|package:[^\x00]+)$"
)
SOURCE_MAX = 600
ITEM_MAX = 1200
BY_MAX = 600
SOURCES_MAX = 1000
EDITS_MAX = 200
TOOLS_PER_EDIT_MAX = 1024
#: ``access/listings.ts`` ``LISTING_CAPS``: the agent caps a listing exactly; this bound is
#: twice its total, so only a body far past it is refused here.
LISTING_TOOLS_MAX = 512
LISTING_TOTAL_MAX = 2 * 2_097_152
OMITTED = Literal[
    "description", "inputSchema", "outputSchema", "other", "title", "annotations", "keys"
]
PLUGIN_NAME = r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$"
PACKAGE_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
CONTENT_HASH = r"^sha256:[0-9a-f]{64}$"
#: The route's reasons: a lease another holder keeps; a lease this holder no longer holds.
LEASE_BUSY = "busy"
LEASE_LOST = "lease-lost"
#: How often a waiting acquire tries again, and the longest wait, in seconds.
ACQUIRE_POLL = 0.25
MAX_WAIT = 25.0
#: How many lease acquires of this process may wait on a grant's row lock at once
#: (``leases.acquire``'s ``wait``). Each holds a settings-store pool connection while it
#: waits; past the cap an acquire does not wait on the row and polls as for any busy lease.
MAX_ROW_WAITS = 2
_row_waits = 0
#: ``plugins/pluginSessions.ts``: a session id, a plugin's ledger id, a replica's host.
SESSION_ID_MAX = 500
REPLICAS_MAX = 500

Source = Annotated[str, Field(min_length=1, max_length=SOURCE_MAX, pattern=SOURCE_PATTERN)]
Item = Annotated[str, Field(min_length=1, max_length=ITEM_MAX)]
Access = Literal["off", "on", "read", "write", "outward"]
ToolAccess = Literal["off", "read", "write", "outward"]
Ledger = Annotated[str, Field(min_length=1, max_length=200)]
Replica = Annotated[str, Field(min_length=1, max_length=300)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _in_tx[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    """``fn`` in one transaction on a pooled connection, off the event loop."""

    def run() -> T:
        with state.settings_store.pool.connection() as conn, conn.transaction():
            return fn(conn)

    return await asyncio.to_thread(run)


def _get(row: Any, key: str) -> Any:
    return row[key] if isinstance(row, dict) else getattr(row, key)


# -- access --------------------------------------------------------------------------------


class AccessRow(BaseModel):
    source: str
    item: str
    access: str
    updated_by: str


def _row(r: Any) -> AccessRow:
    return AccessRow(
        source=_get(r, "source"),
        item=_get(r, "item"),
        access=_get(r, "access"),
        updated_by=_get(r, "updated_by"),
    )


class Sources(_Body):
    sources: list[Source] = Field(max_length=SOURCES_MAX)


class AccessRows(BaseModel):
    rows: list[AccessRow]


@router.post("/access/rows", name="crud_agent_access_rows")
async def access_rows(body: Sources, state: StateDep) -> AccessRows:
    if not body.sources:
        return AccessRows(rows=[])

    def run(conn: psycopg.Connection[Any]) -> list[AccessRow]:
        rows = conn.execute(
            "SELECT source, item, access, updated_by FROM ai_access WHERE source = ANY(%s)"
            " ORDER BY source, item",
            (body.sources,),
        ).fetchall()
        return [_row(r) for r in rows]

    return AccessRows(rows=await _in_tx(state, run))


class OneRow(BaseModel):
    row: AccessRow | None


@router.get("/access/row", name="crud_agent_access_row")
async def access_row(
    state: StateDep,
    source: Annotated[str, Query(min_length=1, max_length=SOURCE_MAX, pattern=SOURCE_PATTERN)],
    item: Annotated[str, Query(min_length=1, max_length=ITEM_MAX)],
) -> OneRow:
    def run(conn: psycopg.Connection[Any]) -> AccessRow | None:
        r = conn.execute(
            "SELECT source, item, access, updated_by FROM ai_access"
            " WHERE source = %s AND item = %s",
            (source, item),
        ).fetchone()
        return None if r is None else _row(r)

    return OneRow(row=await _in_tx(state, run))


class ItemsEdit(_Body):
    source: Source
    kind: Literal["items"]
    #: An item's access, or null to delete its row.
    items: dict[Item, Access | None] = Field(max_length=TOOLS_PER_EDIT_MAX)


class ReplaceEdit(_Body):
    source: Source
    kind: Literal["replace"]
    tools: dict[Item, ToolAccess] = Field(max_length=TOOLS_PER_EDIT_MAX)

    @field_validator("tools")
    @classmethod
    def _no_switch(cls, tools: dict[str, str]) -> dict[str, str]:
        if SOURCE_ITEM in tools:
            raise ValueError("a tool row cannot be the source's switch")
        return tools


class ForgetEdit(_Body):
    source: Source
    kind: Literal["forget"]


Edit = Annotated[ItemsEdit | ReplaceEdit | ForgetEdit, Field(discriminator="kind")]


class AccessWrite(_Body):
    by: str = Field(min_length=1, max_length=BY_MAX)
    edits: list[Edit] = Field(min_length=1, max_length=EDITS_MAX)
    skip_unchanged: bool = False


class AccessWritten(BaseModel):
    #: The edits written, by their place in the request: all of them without
    #: ``skip_unchanged``.
    applied: list[int]


def _apply(rows: list[AccessRow], edit: Edit, by: str) -> list[AccessRow]:
    """``access/store.ts`` ``applyEdit``: ``rows`` as they stand after ``edit``."""
    mine = edit.source
    if isinstance(edit, ForgetEdit):
        return [r for r in rows if r.source != mine]
    if isinstance(edit, ReplaceEdit):
        kept = [r for r in rows if r.source != mine or r.item == SOURCE_ITEM]
        return kept + [
            AccessRow(source=mine, item=item, access=access, updated_by=by)
            for item, access in edit.tools.items()
        ]
    out = list(rows)
    for item, access in edit.items.items():
        out = [r for r in out if not (r.source == mine and r.item == item)]
        if access is not None:
            out.append(AccessRow(source=mine, item=item, access=access, updated_by=by))
    return out


def _shape(rows: list[AccessRow], source: str) -> list[str]:
    return sorted(f"{r.item}={r.access}" for r in rows if r.source == source)


def _run_edit(conn: psycopg.Connection[Any], edit: Edit, by: str) -> None:
    if isinstance(edit, ForgetEdit):
        forget_source(conn, edit.source)
        return
    if isinstance(edit, ReplaceEdit):
        write_tools(conn, edit.source, edit.tools, by)
        return
    for item, access in edit.items.items():
        if access is None:
            conn.execute(
                "DELETE FROM ai_access WHERE source = %s AND item = %s", (edit.source, item)
            )
        else:
            conn.execute(
                "INSERT INTO ai_access (source, item, access, updated_by)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (source, item) DO UPDATE"
                " SET access = EXCLUDED.access, updated_by = EXCLUDED.updated_by,"
                " updated_at = now()",
                (edit.source, item, access, by),
            )


@router.post("/access/write", name="crud_agent_access_write")
async def access_write(body: AccessWrite, state: StateDep) -> AccessWritten:
    def run(conn: psycopg.Connection[Any]) -> list[int]:
        applied = list(range(len(body.edits)))
        if body.skip_unchanged:
            sources = sorted({e.source for e in body.edits})
            current = [
                _row(r)
                for r in conn.execute(
                    "SELECT source, item, access, updated_by FROM ai_access"
                    " WHERE source = ANY(%s) FOR UPDATE",
                    (sources,),
                ).fetchall()
            ]
            applied = [
                i
                for i, e in enumerate(body.edits)
                if _shape(_apply(current, e, body.by), e.source) != _shape(current, e.source)
            ]
            if not applied:
                return []
        # First: the notification is queued in this transaction, so a failing write drops it.
        notify_warm_evict(conn)
        for i in applied:
            _run_edit(conn, body.edits[i], body.by)
        return applied

    try:
        applied = await _in_tx(state, run)
    except psycopg.errors.CheckViolation as e:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"the access table refused the write: {e}"
        ) from None
    return AccessWritten(applied=applied)


# -- tool listings ---------------------------------------------------------------------------


class StoredTool(_Body):
    raw: dict[str, Any]
    omitted: list[OMITTED] = Field(max_length=7)

    @field_validator("raw")
    @classmethod
    def _named(cls, raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw.get("name"), str):
            raise ValueError("a listed tool needs a string name")
        return raw


def _bounded(tools: list[StoredTool]) -> list[StoredTool]:
    text = json.dumps([t.model_dump() for t in tools], ensure_ascii=False, separators=(",", ":"))
    if len(text) > LISTING_TOTAL_MAX:
        raise ValueError("the listing is larger than a listing can be")
    return tools


class Listing(BaseModel):
    source: str
    tools: Any
    listed_at: datetime
    truncated: bool


class Listings(BaseModel):
    listings: list[Listing]


@router.post("/tool-listings/read", name="crud_agent_tool_listings")
async def tool_listings(body: Sources, state: StateDep) -> Listings:
    if not body.sources:
        return Listings(listings=[])

    def run(conn: psycopg.Connection[Any]) -> list[Listing]:
        rows = conn.execute(
            "SELECT source, tools, listed_at, truncated FROM ai_tool_listings"
            " WHERE source = ANY(%s)",
            (body.sources,),
        ).fetchall()
        return [
            Listing(
                source=_get(r, "source"),
                tools=_get(r, "tools"),
                listed_at=_get(r, "listed_at"),
                truncated=_get(r, "truncated"),
            )
            for r in rows
        ]

    return Listings(listings=await _in_tx(state, run))


class _ListingPut(_Body):
    tools: list[StoredTool] = Field(max_length=LISTING_TOOLS_MAX)
    truncated: bool = False
    #: The caller's lease on ``tool-listing:<source>``.
    lease_token: UUID

    @field_validator("tools")
    @classmethod
    def _size(cls, tools: list[StoredTool]) -> list[StoredTool]:
        return _bounded(tools)


class PluginListing(_ListingPut):
    name: str = Field(pattern=PLUGIN_NAME)
    url: str = Field(min_length=1, max_length=2048)


class PackageServerListing(_ListingPut):
    pkg: str = Field(pattern=PACKAGE_NAME)
    server: str = Field(min_length=1, max_length=SOURCE_MAX)
    content_hash: str = Field(pattern=CONTENT_HASH)


class Stored(BaseModel):
    #: False: the plugin or its pin changed since the test; nothing was stored.
    stored: bool


def _lease_lost() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT, "The lease is not held by this token.", reason=LEASE_LOST
    )


def _upsert_listing(
    conn: psycopg.Connection[Any], source: str, tools: list[StoredTool], truncated: bool
) -> None:
    conn.execute(
        "INSERT INTO ai_tool_listings (source, tools, truncated) VALUES (%s, %s, %s)"
        " ON CONFLICT (source) DO UPDATE SET tools = EXCLUDED.tools,"
        " truncated = EXCLUDED.truncated, listed_at = now()",
        (source, Jsonb([t.model_dump() for t in tools]), truncated),
    )


@router.post("/tool-listings/plugin", name="crud_agent_tool_listing_plugin")
async def tool_listing_plugin(body: PluginListing, state: StateDep) -> Stored:
    """``ToolListings.exclusive``'s put: under the plugin's row lock, while the lease holds."""
    source = remote_source(body.name)

    def run(conn: psycopg.Connection[Any]) -> bool:
        if not leases.held_in(conn, f"tool-listing:{source}", str(body.lease_token)):
            raise _lease_lost()
        plugin = conn.execute(
            "SELECT 1 FROM ai_plugins WHERE name = %s AND url = %s FOR UPDATE",
            (body.name, body.url),
        ).fetchone()
        if plugin is None:
            return False
        _upsert_listing(conn, source, body.tools, body.truncated)
        return True

    return Stored(stored=await _in_tx(state, run))


@router.post("/tool-listings/package-server", name="crud_agent_tool_listing_package_server")
async def tool_listing_package_server(body: PackageServerListing, state: StateDep) -> Stored:
    """``ToolListings.exclusiveServer``'s put: under the package's row lock, while its pin is
    the content hash listed and the lease holds."""
    source = package_server_source(body.pkg, body.server)

    def run(conn: psycopg.Connection[Any]) -> bool:
        if not leases.held_in(conn, f"tool-listing:{source}", str(body.lease_token)):
            raise _lease_lost()
        row = conn.execute(
            "SELECT 1 FROM ai_plugin_packages WHERE name = %s AND content_hash = %s FOR UPDATE",
            (body.pkg, body.content_hash),
        ).fetchone()
        if row is None:
            return False
        _upsert_listing(conn, source, body.tools, body.truncated)
        return True

    return Stored(stored=await _in_tx(state, run))


# -- leases --------------------------------------------------------------------------------

LeaseKey = Annotated[str, Path(min_length=1, max_length=SOURCE_MAX + 20)]


class LeaseAsk(_Body):
    token: UUID
    ttl_s: float = Field(default=leases.LEASE_TTL, gt=0, le=leases.MAX_TTL)
    wait_s: float = Field(default=0, ge=0, le=MAX_WAIT)


class LeaseRenew(_Body):
    token: UUID
    ttl_s: float = Field(default=leases.LEASE_TTL, gt=0, le=leases.MAX_TTL)


class LeaseHeld(BaseModel):
    token: UUID
    expires_at: datetime


def _checked_key(key: str) -> str:
    if not leases.valid_key(key):
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "That is not a lease key.")
    return key


@router.post("/leases/{key:path}", name="crud_agent_acquire_lease")
async def acquire_lease(key: LeaseKey, body: LeaseAsk, state: StateDep) -> LeaseHeld:
    _checked_key(key)
    token = str(body.token)
    deadline = time.monotonic() + body.wait_s
    global _row_waits
    while True:
        waits = key.startswith("plugin-oauth:") and _row_waits < MAX_ROW_WAITS
        wait = max(0.0, deadline - time.monotonic()) if waits else 0.0
        if waits:
            _row_waits += 1
        try:
            expires = await _in_tx(
                state, partial(leases.acquire, key=key, token=token, ttl=body.ttl_s, wait=wait)
            )
        finally:
            if waits:
                _row_waits -= 1
        left = deadline - time.monotonic()
        if expires is not None:
            return LeaseHeld(token=body.token, expires_at=expires)
        if left <= 0:
            raise ApiError(
                status.HTTP_409_CONFLICT, "Another holder keeps the lease.", reason=LEASE_BUSY
            )
        await asyncio.sleep(min(ACQUIRE_POLL, left))


@router.put("/leases/{key:path}", name="crud_agent_renew_lease")
async def renew_lease(key: LeaseKey, body: LeaseRenew, state: StateDep) -> LeaseHeld:
    _checked_key(key)
    expires = await _in_tx(state, lambda conn: leases.renew(conn, key, str(body.token), body.ttl_s))
    if expires is None:
        raise _lease_lost()
    return LeaseHeld(token=body.token, expires_at=expires)


@router.delete(
    "/leases/{key:path}",
    name="crud_agent_release_lease",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def release_lease(key: LeaseKey, token: UUID, state: StateDep) -> None:
    _checked_key(key)
    await _in_tx(state, lambda conn: leases.release(conn, key, str(token)))


# -- plugin sessions ---------------------------------------------------------------------------


class SessionSeen(_Body):
    plugin: Ledger
    id: str = Field(min_length=1, max_length=SESSION_ID_MAX)
    replica: Replica


class SessionEnded(_Body):
    plugin: Ledger
    id: str = Field(min_length=1, max_length=SESSION_ID_MAX)


class Ok(BaseModel):
    ok: bool = True


@router.post("/plugin-sessions/seen", name="crud_agent_plugin_session_seen")
async def plugin_session_seen(body: SessionSeen, state: StateDep) -> Ok:
    def run(conn: psycopg.Connection[Any]) -> None:
        conn.execute(
            "INSERT INTO ai_plugin_sessions (plugin_id, upstream_session_id, replica)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (body.plugin, body.id, body.replica),
        )

    await _in_tx(state, run)
    return Ok()


@router.post("/plugin-sessions/ended", name="crud_agent_plugin_session_ended")
async def plugin_session_ended(body: SessionEnded, state: StateDep) -> Ok:
    def run(conn: psycopg.Connection[Any]) -> None:
        conn.execute(
            "DELETE FROM ai_plugin_sessions WHERE plugin_id = %s AND upstream_session_id = %s",
            (body.plugin, body.id),
        )

    await _in_tx(state, run)
    return Ok()


class Mark(BaseModel):
    #: The database's clock as Postgres's text, to the microsecond.
    mark: str


@router.get("/plugin-sessions/mark", name="crud_agent_plugin_session_mark")
async def plugin_session_mark(state: StateDep) -> Mark:
    def run(conn: psycopg.Connection[Any]) -> str:
        row = conn.execute("SELECT now()::text AS t").fetchone()
        return str(_get(row, "t"))

    return Mark(mark=await _in_tx(state, run))


class SessionRow(BaseModel):
    plugin_id: str
    upstream_session_id: str
    old: bool


class SessionRows(BaseModel):
    rows: list[SessionRow]


class DeadAsk(_Body):
    #: The replicas whose agent-tools worker polls.
    live: list[Replica] = Field(min_length=1, max_length=REPLICAS_MAX)
    grace_ms: int = Field(ge=0, le=7 * 24 * 3_600_000)
    give_up_ms: int = Field(ge=0, le=30 * 24 * 3_600_000)


@router.post("/plugin-sessions/dead", name="crud_agent_plugin_sessions_dead")
async def plugin_sessions_dead(body: DeadAsk, state: StateDep) -> SessionRows:
    """``sweepDeadReplicas``'s read: the rows of replicas that do not poll, past the grace."""

    def run(conn: psycopg.Connection[Any]) -> list[SessionRow]:
        rows = conn.execute(
            "SELECT plugin_id, upstream_session_id,"
            " created_at < now() - make_interval(secs => %s) AS old"
            " FROM ai_plugin_sessions"
            " WHERE replica <> ALL(%s) AND created_at < now() - make_interval(secs => %s)",
            (body.give_up_ms / 1000, body.live, body.grace_ms / 1000),
        ).fetchall()
        return [_session_row(r) for r in rows]

    return SessionRows(rows=await _in_tx(state, run))


class OwnAsk(_Body):
    replica: Replica
    mark: str = Field(min_length=10, max_length=64)


@router.post("/plugin-sessions/own", name="crud_agent_plugin_sessions_own")
async def plugin_sessions_own(body: OwnAsk, state: StateDep) -> SessionRows:
    """``endOwnSessions``'s read: this replica's rows from before ``mark``."""

    def run(conn: psycopg.Connection[Any]) -> list[SessionRow]:
        rows = conn.execute(
            "SELECT plugin_id, upstream_session_id, false AS old FROM ai_plugin_sessions"
            " WHERE replica = %s AND created_at < %s::text::timestamptz",
            (body.replica, body.mark),
        ).fetchall()
        return [_session_row(r) for r in rows]

    try:
        return SessionRows(rows=await _in_tx(state, run))
    except psycopg.errors.InvalidDatetimeFormat:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "That is not a mark.") from None


def _session_row(r: Any) -> SessionRow:
    return SessionRow(
        plugin_id=_get(r, "plugin_id"),
        upstream_session_id=_get(r, "upstream_session_id"),
        old=bool(_get(r, "old")),
    )
