"""The idempotency ledger of the CRUD routes' non-natural writes (#5298, plan D5).

A write whose retry after a lost answer would change the state or the answer takes an
``Idempotency-Key``. A route runs it through `idempotent_write`, which opens the write's
transaction and, inside it, answers the reply stored under that key, refuses the key with
another route or body (422), or runs the write and records its reply. The ledger row is
only ever inserted whole, with the reply, and commits with the write or not at all: a
rolled-back write leaves no reply to replay. Two requests with one key are serialised by a
transaction-scoped advisory lock on the key, so the second waits and replays the first.
Rows are kept `RETENTION` (`prune`, in the auth sweep). `idempotent` and `record` are the
two halves, for a route whose write cannot be one callable. `stored` reads a reply with no
lock, for a route that answers a retry before it does any other work.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
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
#: A write sent without its ``Idempotency-Key`` (`required_key`): nothing was done.
KEY_REQUIRED_PROBLEM: Final = "https://scadbuddy.dev/problems/crud/idempotency-key-required"

#: The header a non-natural write route takes.
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
#: The same, for a route that answers a request without it with a 428 (`required_key`)
#: rather than a 422 of its shape.
MaybeIdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)
]


def required_key(key: str | None) -> str:
    """``key``, or a 428 problem when the request carried none."""
    if key is None:
        raise ApiError(
            428,
            "This write needs an Idempotency-Key header. Nothing was done.",
            type_=KEY_REQUIRED_PROBLEM,
        )
    return key


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
    the key is new (then write, and `record` the reply in the same transaction). It holds
    the key's lock to the transaction's end. ``route`` names the route, as
    ``"POST /blob-index/{key}"``; ``body`` is the request's JSON-able body."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s::integer, hashtext(%s))",
        (LOCK_CLASS, f"{caller}\x1f{key}"),
    )
    return stored(conn, caller, key, route, body)


def stored(
    conn: Connection[DictRow], caller: CrudCaller, key: str, route: str, body: Any
) -> Replay | None:
    """The reply stored under ``key``, read with no lock: what a route looks up before
    any other work, so a retry gets the first answer whatever changed since. None when
    the key is new, or its write has not committed yet; then `idempotent_write` decides.
    422 for a key used with another route or body."""
    row = conn.execute(
        "SELECT route, body_sha256, status, body FROM crud_requests WHERE caller = %s AND key = %s",
        (caller, key),
    ).fetchone()
    if row is None:
        return None
    if row["route"] != route or row["body_sha256"] != _digest(body):
        raise ApiError(
            422,
            "This Idempotency-Key was used for another request.",
            type_=KEY_PROBLEM,
        )
    return Replay(row["status"], row["body"])


def record(
    conn: Connection[DictRow],
    caller: CrudCaller,
    key: str,
    route: str,
    body: Any,
    reply: Replay,
) -> None:
    """After the write, in its transaction: the reply a retry of ``key`` gets."""
    conn.execute(
        "INSERT INTO crud_requests (caller, key, route, body_sha256, status, body)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (caller, key, route, _digest(body), reply.status, Jsonb(reply.body)),
    )


def idempotent_write(
    conn: Connection[DictRow],
    caller: CrudCaller,
    key: str,
    route: str,
    body: Any,
    write: Callable[[], Replay],
) -> Replay:
    """``write`` once per ``key``, in one transaction with its ledger row: the stored reply
    of a key seen before, else ``write``'s, recorded. A ``write`` that raises records
    nothing, and nothing it wrote commits."""
    with conn.transaction():
        stored = idempotent(conn, caller, key, route, body)
        if stored is not None:
            return stored
        reply = write()
        record(conn, caller, key, route, body, reply)
        return reply


def prune(conn: Connection[DictRow], older_than: timedelta = RETENTION) -> int:
    """Drop the replies older than ``older_than``; how many there were."""
    cursor = conn.execute("DELETE FROM crud_requests WHERE created_at < now() - %s", (older_than,))
    return cursor.rowcount
