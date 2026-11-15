"""Storefront visitors (#5282, spec 2026-11-02 §5.2): one row per visitor id the edge
minted. The row keeps only that id's sha256 (hex), never the id, so the database alone
never lets anyone act as a visitor. ``storefront_visitors.id`` is the row's own key: the
edge never sees it, and the drafts and the visitor actor name it."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.orders.models import Actor
from scadbuddy.orders.store import Conn

_VISITOR = "id, created_at, last_seen_at"
#: The edge's visitor id: 32 random bytes as URL-safe base64, unpadded.
VISITOR_ID = re.compile(r"[A-Za-z0-9_-]{43}")
#: The most visitors, and the most render rows, one `VisitorStore.sweep` deletes: the
#: draft sweep's batch (`Lifecycle.sweep_drafts`).
SWEEP_BATCH = 200


@dataclass(frozen=True)
class Visitor:
    id: str
    created_at: datetime
    last_seen_at: datetime


def token_sha256(visitor_id: str) -> str:
    """The id's sha256 (hex); ``ValueError`` for an id not of the edge's shape."""
    if not VISITOR_ID.fullmatch(visitor_id):
        raise ValueError("a visitor id is 43 URL-safe base64 characters")
    return hashlib.sha256(visitor_id.encode()).hexdigest()


def visitor_actor(visitor: Visitor) -> Actor:
    """The actor of what the visitor does (plan point 7): its row, never its id."""
    return Actor(kind="visitor", principal=f"storefront_visitor:{visitor.id}")


class VisitorStore:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def ensure(self, visitor_id: str) -> Visitor:
        """The visitor's row, made on its first write, and ``last_seen_at`` set to now."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO storefront_visitors (token_sha256) VALUES (%s)"
                " ON CONFLICT (token_sha256) DO UPDATE SET last_seen_at = now()"
                f" RETURNING {_VISITOR}",
                (token_sha256(visitor_id),),
            ).fetchone()
        assert row is not None
        return Visitor(**row)

    def find(self, visitor_id: str) -> Visitor | None:
        """The visitor's row, or None for a visitor that never wrote. ``last_seen_at``
        moves at most once a minute, so a read rarely writes."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "WITH touched AS (UPDATE storefront_visitors SET last_seen_at = now()"
                " WHERE token_sha256 = %(hash)s AND last_seen_at < now() - interval '1 minute'"
                f" RETURNING {_VISITOR})"
                " SELECT * FROM touched UNION ALL"
                f" SELECT {_VISITOR} FROM storefront_visitors WHERE token_sha256 = %(hash)s"
                " AND NOT EXISTS (SELECT 1 FROM touched)",
                {"hash": token_sha256(visitor_id)},
            ).fetchone()
        return None if row is None else Visitor(**row)

    @staticmethod
    def sweep(
        conn: Conn,
        *,
        visitors_before: datetime,
        renders_before: datetime,
        limit: int = SWEEP_BATCH,
    ) -> tuple[int, int]:
        """The housekeeping pass (#5282), on the caller's connection: up to ``limit``
        `storefront_renders` rows made before ``renders_before``, oldest first, then up
        to ``limit`` visitors last seen before ``visitors_before`` that no order names
        and no account holds (their newer renders cascade). A visitor that joined an
        account (#5283) is kept: its orders are the account's. A visitor with a cart keeps
        it until the draft sweep takes the cart. A larger backlog drains over later runs.
        The counts deleted: (visitors, renders)."""
        renders = conn.execute(
            "DELETE FROM storefront_renders WHERE id IN (SELECT id FROM storefront_renders"
            " WHERE created_at < %s ORDER BY created_at LIMIT %s)",
            (renders_before, limit),
        ).rowcount
        visitors = conn.execute(
            "DELETE FROM storefront_visitors WHERE id IN (SELECT v.id FROM storefront_visitors v"
            " WHERE v.last_seen_at < %s"
            " AND v.account_id IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.storefront_visitor_id = v.id)"
            " ORDER BY v.last_seen_at LIMIT %s)",
            (visitors_before, limit),
        ).rowcount
        return visitors, renders
