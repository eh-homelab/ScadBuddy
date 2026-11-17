"""A session's row and what its tool calls touched (#5299 step 9, part a), for
``api/crud_agent_sessions.py`` areas ``sessions`` and ``session-resources``:
``ai_sessions`` and ``ai_session_resources``.

Each function is the SQL of ``agent/src/sessions/rows.ts`` `PgSessionRows` (the session
manager's reads and writes of the row) or ``agent/src/sessions/touched.ts``
`SessionResources`, statement for statement, a transaction as one transaction. A durable
turn's writes of the row with its events are ``turns.py``'s (#6449).

``insert`` takes the owner's start lock (``ai_sessions.start:<kind>:<id>``) before it counts
the owner's recent sessions, and a fork's parent's payload-key lock shared before it reads
the parent's tombstone, as the agent's SQL does: an agent pod still on SQL takes the same
locks, so the two paths exclude each other.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from scadbuddy.workflows.payload_codec import payload_key_lock

#: A pending handoff offer, read as none once it has expired (``rows.ts`` ``LIVE_OFFER``).
LIVE_OFFER = "pending_owner_until > now()"
#: The session whose budget this one spends from: itself, or a fork's lineage root (#823).
_ROOT = "coalesce(ai_sessions.budget_root_id, ai_sessions.id)"
#: The budget a session spends from, and what every session spending from it has spent
#: (``turnShared.ts`` ``POOL_BUDGET``, ``POOL_COST``).
POOL_BUDGET = (
    f"coalesce((SELECT r.budget_usd FROM ai_sessions r WHERE r.id = {_ROOT}),"
    " ai_sessions.budget_usd)"
)
POOL_COST = (
    f"(SELECT sum(m.cost_usd) FROM ai_sessions m WHERE coalesce(m.budget_root_id, m.id) = {_ROOT})"
)
#: A timestamp as the agent's Date writes it: ISO 8601 in UTC, milliseconds.
_MS = '\'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"\''


def _iso(column: str, name: str) -> str:
    return f"to_char({column} AT TIME ZONE 'UTC', {_MS}) AS {name}"


#: The row as the session manager reads it (``rows.ts`` ``COLUMNS``), timestamps as text.
COLUMNS = (
    "id::text AS id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id,"
    f" CASE WHEN {LIVE_OFFER} THEN pending_owner_kind END AS offer_kind,"
    f" CASE WHEN {LIVE_OFFER} THEN pending_owner_id END AS offer_id,"
    f" CASE WHEN {LIVE_OFFER} THEN pending_owner_label END AS offer_label,"
    f" CASE WHEN {LIVE_OFFER} THEN"
    f" to_char(pending_owner_until AT TIME ZONE 'UTC', {_MS}) END AS offer_until,"
    " status, title, tags, scope, parent_id::text AS parent_id, runtime, max_turns,"
    f" {POOL_BUDGET} AS budget_usd, cost_usd, {POOL_COST} AS pool_cost_usd, turns,"
    " (runtime = 'unified' AND durable_turn_id IS NOT NULL) AS turn_active,"
    f" {_iso('created_at', 'created_at')}, {_iso('updated_at', 'updated_at')},"
    f" CASE WHEN archived_at IS NULL THEN NULL ELSE"
    f" to_char(archived_at AT TIME ZONE 'UTC', {_MS}) END AS archived_at"
)
#: What a turn's claim reads besides (``rows.ts`` ``TURN_COLUMNS``, for ``manager.ts``
#: ``ClaimRow`` and ``runningDurableTurn``): ``claimed_at`` at Postgres's microseconds
#: (``CLAIMED_AT``, #1383).
TURN_COLUMNS = (
    "unpriced_cost_usd, agent_model, agent_effort, agent_type, background_tools, turn_flow,"
    " turn_tiers, durable_turn_id::text AS durable_turn_id,"
    " to_char(coalesce(turn_started_at, updated_at) AT TIME ZONE 'UTC',"
    ' \'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\') AS claimed_at'
)


def get(conn: Connection[Any], session_id: str) -> dict[str, Any] | None:
    """The row with what a turn's claim reads, or None."""
    row: dict[str, Any] | None = conn.execute(
        f"SELECT {COLUMNS}, {TURN_COLUMNS} FROM ai_sessions WHERE id = %s", (session_id,)
    ).fetchone()
    return row


def like_literal(text: str) -> str:
    """``text`` as a literal part of an ILIKE pattern (``rows.ts`` ``likeLiteral``)."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_query(
    principal: tuple[str, str] | None,
    *,
    status: str | None = None,
    origin: str | None = None,
    archived: str = "exclude",
    resource: dict[str, Any] | None = None,
    search: str | None = None,
    accept: list[str] | None = None,
    with_file: bool = False,
    limit: int = 100,
) -> tuple[str, dict[str, Any]]:
    """``rows.ts`` ``listQuery``: the owner-or-creator-or-offered filter (none for the
    browser user, ``principal`` None), then the filters, newest first."""
    where: list[str] = []
    params: dict[str, Any] = {}
    if principal is not None:
        params["kind"], params["pid"] = principal
        where.append(
            "((owner_kind = %(kind)s AND owner_id = %(pid)s)"
            " OR (creator_kind = %(kind)s AND creator_id = %(pid)s)"
            f" OR (pending_owner_kind = %(kind)s AND pending_owner_id = %(pid)s AND {LIVE_OFFER}))"
        )
    if status:
        params["status"] = status
        where.append("status = %(status)s")
    if origin:
        params["origin"] = origin
        where.append("origin = %(origin)s")
    if archived != "include":
        where.append("archived_at IS NOT NULL" if archived == "only" else "archived_at IS NULL")
    if resource is not None:
        if resource["type"] == "model":
            params["model"] = resource["id"]
            match = "r.model_slug = %(model)s"
        else:
            params["rtype"], params["rid"] = resource["type"], resource["id"]
            match = "r.resource_type = %(rtype)s AND r.resource_id = %(rid)s"
        where.append(
            "EXISTS (SELECT 1 FROM ai_session_resources r"
            f" WHERE r.session_id = ai_sessions.id AND {match})"
        )
    accepted: list[str] = []
    for n, entry in enumerate((accept or []) if (search or with_file) else []):
        key = f"accept{n}"
        if entry.startswith("."):
            params[key] = f"%{like_literal(entry)}"
            accepted.append(f"lower(f.path) LIKE %({key})s")
        elif entry.endswith("/*"):
            params[key] = f"{like_literal(entry[:-1])}%"
            accepted.append(f"lower(f.media_type) LIKE %({key})s")
        else:
            params[key] = entry
            accepted.append(f"lower(f.media_type) = %({key})s")

    def file_where(extra: str) -> str:
        kinds = f" AND ({' OR '.join(accepted)})" if accepted else ""
        return (
            "EXISTS (SELECT 1 FROM ai_session_files f"
            f" WHERE f.workspace = 'session:' || ai_sessions.id::text{kinds}{extra})"
        )

    if search:
        params["search"] = f"%{like_literal(search)}%"
        where.append(f"(title ILIKE %(search)s OR {file_where(' AND f.path ILIKE %(search)s')})")
    if with_file:
        where.append(file_where(""))
    params["limit"] = min(max(limit, 1), 500)
    text = (
        f"SELECT {COLUMNS} FROM ai_sessions"
        f"{' WHERE ' + ' AND '.join(where) if where else ''}"
        " ORDER BY updated_at DESC LIMIT %(limit)s"
    )
    return text, params


def list_sessions(
    conn: Connection[Any], principal: tuple[str, str] | None, **filters: Any
) -> list[dict[str, Any]]:
    text, params = list_query(principal, **filters)
    return list(conn.execute(text.encode(), params).fetchall())


def _recent(conn: Connection[Any], owner: tuple[str, str], window_ms: int) -> int:
    row = conn.execute(
        "SELECT count(*)::int AS n FROM ai_sessions"
        " WHERE owner_kind = %s AND owner_id = %s"
        " AND created_at > now() - (%s * interval '1 millisecond')",
        (owner[0], owner[1], window_ms),
    ).fetchone()
    return 0 if row is None else int(row["n"])


def started_recently(conn: Connection[Any], owner: tuple[str, str], window_ms: int) -> int:
    """How many sessions ``owner`` started in the last ``window_ms`` (a precheck; the insert's
    count under the lock is the one that holds)."""
    return _recent(conn, owner, window_ms)


def insert(
    conn: Connection[Any],
    *,
    session_id: str,
    origin: str,
    owner: dict[str, str],
    title: str,
    tags: list[str],
    scope: dict[str, Any],
    parent_id: str | None,
    runtime: str | None,
    max_turns: int,
    budget_usd: float,
    budget_of: str | None,
    fork_of: tuple[str, int] | None,
    background_tools: bool | None,
    limit: tuple[int, int] | None,
) -> str | None:
    """Inserts the row (``rows.ts`` ``PgSessionRows.insert``), in one transaction: with
    ``limit`` (most sessions, window in ms) under the owner's start lock and refused
    ``rate_limited`` past it; a fork (``fork_of``: parent, cut) under its parent's lock,
    shared, and refused ``forgotten`` once the parent is. None when it was inserted."""
    with conn.transaction():
        if limit is not None:
            # Per owner, held until commit, so concurrent starts count each other.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"ai_sessions.start:{owner['kind']}:{owner['id']}",),
            )
            if _recent(conn, (owner["kind"], owner["id"]), limit[1]) >= limit[0]:
                return "rate_limited"
        if fork_of is not None:
            # A forget of the parent either went first, and its tombstone refuses the fork
            # here, or waits for this to commit and then finds the fork to copy its range into.
            subject = f"session-{fork_of[0]}"
            conn.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtext(%s))", (payload_key_lock(subject),)
            )
            gone = conn.execute(
                "SELECT 1 FROM ai_forgotten_subjects WHERE subject = %s", (subject,)
            ).fetchone()
            if gone is not None:
                return "forgotten"
        conn.execute(
            "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
            " creator_id, status, title, tags, scope, parent_id, runtime, max_turns, budget_usd,"
            " budget_root_id, fork_parent_id, fork_cut, background_tools)"
            " VALUES (%(id)s, %(origin)s, %(kind)s, %(oid)s, %(label)s, %(kind)s, %(oid)s, 'idle',"
            " %(title)s, %(tags)s, %(scope)s, %(parent)s, %(runtime)s, %(max_turns)s, %(budget)s,"
            " (SELECT coalesce(budget_root_id, id) FROM ai_sessions WHERE id = %(budget_of)s),"
            " %(fork_parent)s, %(fork_cut)s,"
            " CASE WHEN %(bg)s::boolean IS NOT NULL THEN %(bg)s::boolean"
            " WHEN %(parent)s::uuid IS NOT NULL THEN coalesce((SELECT background_tools"
            " FROM ai_sessions WHERE id = %(parent)s::uuid), true) ELSE true END)",
            {
                "id": session_id,
                "origin": origin,
                "kind": owner["kind"],
                "oid": owner["id"],
                "label": owner["label"],
                "title": title,
                "tags": tags,
                "scope": Jsonb(scope),
                "parent": parent_id,
                "runtime": runtime,
                "max_turns": max_turns,
                "budget": budget_usd,
                "budget_of": budget_of,
                "fork_parent": None if fork_of is None else fork_of[0],
                "fork_cut": None if fork_of is None else fork_of[1],
                "bg": background_tools,
            },
        )
    return None


def set_agent(
    conn: Connection[Any], session_id: str, model: str | None, effort: str | None, kind: str | None
) -> None:
    """A Workflow agent's chat's own model, effort and subagent (``rows.ts`` ``setAgent``, for
    ``manager.ts`` ``startChild``)."""
    conn.execute(
        "UPDATE ai_sessions SET agent_model = %s, agent_effort = %s, agent_type = %s WHERE id = %s",
        (model, effort, kind, session_id),
    )


def offer(
    conn: Connection[Any],
    session_id: str,
    owner: tuple[str, str],
    to: dict[str, str],
    ttl_ms: int,
) -> bool:
    """A handoff offer to ``to``, conditional on the owner the caller read; whether it applied."""
    cur = conn.execute(
        "UPDATE ai_sessions SET pending_owner_kind = %s, pending_owner_id = %s,"
        " pending_owner_label = %s,"
        " pending_owner_until = now() + (%s * interval '1 millisecond'), updated_at = now()"
        " WHERE id = %s AND owner_kind = %s AND owner_id = %s",
        (to["kind"], to["id"], to["label"], ttl_ms, session_id, owner[0], owner[1]),
    )
    return cur.rowcount > 0


def cancel_offer(conn: Connection[Any], session_id: str, to: tuple[str, str]) -> bool:
    """Ends the live offer to ``to``; whether there was one."""
    cur = conn.execute(
        "UPDATE ai_sessions SET pending_owner_kind = NULL, pending_owner_id = NULL,"
        " pending_owner_label = NULL, pending_owner_until = NULL, updated_at = now()"
        " WHERE id = %s AND pending_owner_kind = %s AND pending_owner_id = %s"
        f" AND {LIVE_OFFER}",
        (session_id, to[0], to[1]),
    )
    return cur.rowcount > 0


def transfer(
    conn: Connection[Any],
    session_id: str,
    owner: tuple[str, str],
    to: dict[str, str],
    accepting: bool,
) -> bool:
    """The owner becomes ``to`` and any offer goes, conditional on the owner the caller read
    and, ``accepting``, on the offer still being the live one to ``to``; whether it applied."""
    offer_still = (
        f" AND pending_owner_kind = %(kind)s AND pending_owner_id = %(tid)s AND {LIVE_OFFER}"
        if accepting
        else ""
    )
    cur = conn.execute(
        (
            "UPDATE ai_sessions SET owner_kind = %(kind)s, owner_id = %(tid)s,"
            " owner_label = %(label)s, pending_owner_kind = NULL, pending_owner_id = NULL,"
            " pending_owner_label = NULL, pending_owner_until = NULL, updated_at = now()"
            " WHERE id = %(id)s AND owner_kind = %(okind)s AND owner_id = %(oid)s" + offer_still
        ).encode(),
        {
            "kind": to["kind"],
            "tid": to["id"],
            "label": to["label"],
            "id": session_id,
            "okind": owner[0],
            "oid": owner[1],
        },
    )
    return cur.rowcount > 0


def raise_budget(
    conn: Connection[Any],
    session_id: str,
    by: tuple[str, str],
    add_usd: float,
    max_usd: float,
) -> float | None:
    """Adds ``add_usd`` to the budget of the lineage's root, in one statement, while ``by``
    owns the session and the result stays at most ``max_usd``: the budget after, or None."""
    row = conn.execute(
        "UPDATE ai_sessions r SET budget_usd = round((r.budget_usd + %(add)s)::numeric, 2)"
        "::double precision, updated_at = now()"
        " FROM ai_sessions s"
        " WHERE s.id = %(id)s AND s.owner_kind = %(kind)s"
        " AND (s.owner_id = %(pid)s OR s.owner_kind = 'browser')"
        " AND r.id = coalesce(s.budget_root_id, s.id)"
        " AND round((r.budget_usd + %(add)s)::numeric, 2) <= %(max)s"
        " RETURNING r.budget_usd",
        {"add": add_usd, "id": session_id, "kind": by[0], "pid": by[1], "max": max_usd},
    ).fetchone()
    return None if row is None else float(row["budget_usd"])


def discard(conn: Connection[Any], session_id: str) -> bool:
    """A fork that failed before it was announced (``rows.ts`` ``discard``, for ``manager.ts``
    ``dropFailedFork``): its own transcript rows, its row (whose cascade takes the rest) and
    its payload key, under the key's lock. Only a session that has logged no event: a failed
    fork logs none (its log's transaction rolled back), and nothing that was ever announced
    goes this way. Whether it went."""
    with conn.transaction():
        # The lock a key's creation takes (payload_keys.py), held over the row's delete: a
        # creation that saw the row standing commits its key before this deletes it, and one
        # after this commit sees no row and makes none.
        subject = f"session-{session_id}"
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (payload_key_lock(subject),))
        row = conn.execute(
            "SELECT 1 FROM ai_sessions WHERE id = %s AND event_seq = 0 FOR UPDATE", (session_id,)
        ).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM ai_session_entries WHERE session_id = %s", (session_id,))
        conn.execute("DELETE FROM ai_sessions WHERE id = %s", (session_id,))
        conn.execute("DELETE FROM ai_payload_keys WHERE subject = %s", (subject,))
    return True


# --- what a session's tool calls touched (#931) ------------------------------------------


def record_resources(conn: Connection[Any], session_id: str, rows: list[dict[str, Any]]) -> bool:
    """A call's rows (``touched.ts`` ``SessionResources.record``), in one transaction; False
    when the session is gone."""
    with conn.transaction():
        standing = conn.execute(
            "SELECT 1 FROM ai_sessions WHERE id = %s FOR KEY SHARE", (session_id,)
        ).fetchone()
        if standing is None:
            return False
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ai_session_resources (session_id, tool, resource_type, resource_id,"
                " action, model_slug, before_id, after_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        session_id,
                        r["tool"],
                        r["resource_type"],
                        r["resource_id"],
                        r["action"],
                        r["model_slug"],
                        r["before_id"],
                        r["after_id"],
                    )
                    for r in rows
                ],
            )
    return True


def list_resources(conn: Connection[Any], session_id: str) -> list[dict[str, Any]]:
    """Everything the session touched, oldest first."""
    return list(
        conn.execute(
            "SELECT resource_type, resource_id, action, model_slug, before_id, after_id, tool,"
            f" {_iso('at', 'at')}"
            " FROM ai_session_resources WHERE session_id = %s ORDER BY id",
            (session_id,),
        ).fetchall()
    )
