"""The idempotency ledger of the CRUD routes' non-natural writes (#5298, plan D5).

A write whose retry after a lost answer would change the state or the answer takes an
``Idempotency-Key``. Inside the write's own transaction the route calls `idempotent`
first: it answers the reply stored under that key, refuses the key with another route or
body (422), or answers None, and the route writes and then calls `record`. The ledger row
commits with the write or not at all, so a rolled-back write leaves no reply to replay.
Two requests with one key are serialised by a transaction-scoped advisory lock on the
key, so the second waits and replays the first. Rows are kept `RETENTION` (`prune`, in
the auth sweep).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Final

from fastapi import Header
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import CrudCaller

#: How long a reply is kept for a retry (D5).
RETENTION: Final = timedelta(hours=24)
#: The advisory lock class of a ``(caller, key)`` ("SCRQ").
LOCK_CLASS: Final = 0x5343_5251
KEY_PROBLEM: Final = "https://scadbuddy.dev/problems/crud/idempotency-key-reused"

#: The header a non-natural write route takes.
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


@dataclass(frozen=True)
class Replay:
    """A write's reply: what the route answered, and answers again to a retry."""

    status: int
    body: Any


def _digest(body: Any) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def idempotent(
    conn: Connection[DictRow], caller: CrudCaller, key: str, route: str, body: Any
) -> Replay | None:
    """In the write's transaction, before it: the stored reply of ``key``, or None when
    the key is new (then write, and `record` the reply). ``route`` names the route, as
    ``"POST /blob-index/{key}"``; ``body`` is the request's JSON-able body."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s::integer, hashtext(%s))",
        (LOCK_CLASS, f"{caller}\x1f{key}"),
    )
    row = conn.execute(
        "SELECT route, body_sha256, status, body FROM crud_requests WHERE caller = %s AND key = %s",
        (caller, key),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO crud_requests (caller, key, route, body_sha256, status, body)"
            " VALUES (%s, %s, %s, %s, 0, 'null')",
            (caller, key, route, _digest(body)),
        )
        return None
    if row["route"] != route or row["body_sha256"] != _digest(body):
        raise ApiError(
            422,
            "This Idempotency-Key was used for another request.",
            type_=KEY_PROBLEM,
        )
    return Replay(row["status"], row["body"])


def record(conn: Connection[DictRow], caller: CrudCaller, key: str, reply: Replay) -> None:
    """After the write, in its transaction: the reply a retry of ``key`` gets."""
    conn.execute(
        "UPDATE crud_requests SET status = %s, body = %s WHERE caller = %s AND key = %s",
        (reply.status, Jsonb(reply.body), caller, key),
    )


def prune(conn: Connection[DictRow], older_than: timedelta = RETENTION) -> int:
    """Drop the replies older than ``older_than``; how many there were."""
    cursor = conn.execute("DELETE FROM crud_requests WHERE created_at < now() - %s", (older_than,))
    return cursor.rowcount
