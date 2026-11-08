"""A visitor's render limits (#5282, spec 2026-11-02 §5.4, plan point 14).

Each render a visitor asks for is a ``storefront_renders`` row. A render that costs the
shop a run is ``counted``; a render the visitor shares with another (an open job of the
same inputs, or a finished one still held) is not (`free`). `admit` counts the rows of
the visitor and of the client address in the last minute and the last day against
``storefront_renders_per_minute`` and ``storefront_renders_per_day``, and the counted
rows still open across every visitor against ``storefront_render_concurrency``. It holds
transaction-scoped advisory locks on the visitor, on the client and on the open renders,
always in that order, so two requests cannot both pass one limit.

A row names its job once the submit answers (`attach`); a visitor reads only the jobs its
rows name (`owns`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.core.settings import Settings

type Conn = Connection[DictRow]
type Pool = ConnectionPool[Connection[DictRow]]
Limit = Literal["per_minute", "per_day", "concurrency"]

#: The advisory lock classes: a visitor ("SFRV"), a client ("SFRC") and the open
#: renders of every visitor ("SFRO").
LOCK_VISITOR: Final = 0x5346_5256
LOCK_CLIENT: Final = 0x5346_5243
LOCK_OPEN: Final = 0x5346_524F
MINUTE: Final = timedelta(minutes=1)
DAY: Final = timedelta(days=1)
#: What a visitor refused by the concurrency limit waits: the open renders end at no
#: time this side can know.
CONCURRENCY_RETRY: Final = 5
#: The job states that are no longer open.
SETTLED: Final = ("done", "failed", "cancelled")


class RenderLimited(Exception):  # noqa: N818  (the plan names it)
    """Over a render limit: ``limit`` names it, ``retry_after`` is the seconds until a
    render would pass it."""

    def __init__(self, limit: Limit, retry_after: int) -> None:
        super().__init__(f"over the {limit} render limit; try again in {retry_after} s")
        self.limit: Limit = limit
        self.retry_after = retry_after


@dataclass(frozen=True)
class OwnedRender:
    """The newest row of a visitor's for one job: the listing it asked and its colours."""

    listing_id: str
    colours: dict[int, str]


def _seconds_until(leaves: datetime, at: datetime) -> int:
    return max(1, math.ceil((leaves - at).total_seconds()))


class RenderLimits:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    @staticmethod
    def _insert(
        conn: Conn,
        visitor_id: str,
        client: str,
        *,
        listing_id: str,
        render_key: str,
        counted: bool,
        colours: Mapping[int, str],
        at: datetime | None,
    ) -> int:
        row = conn.execute(
            "INSERT INTO storefront_renders"
            " (visitor_id, client, listing_id, render_key, counted, colours, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp())) RETURNING id",
            (
                visitor_id,
                client,
                listing_id,
                render_key,
                counted,
                Jsonb({str(slot): f for slot, f in colours.items()}),
                at,
            ),
        ).fetchone()
        assert row is not None
        row_id: int = row["id"]
        return row_id

    @staticmethod
    def _rate(
        conn: Conn,
        visitor_id: str,
        client: str,
        *,
        window: timedelta,
        limit: int,
        at: datetime,
    ) -> int | None:
        """The seconds until a render passes the rate of ``window``, or None when it
        passes now."""
        waits: list[int] = []
        for column, value in (("visitor_id", visitor_id), ("client", client)):
            row = conn.execute(
                "SELECT count(*) AS n, min(created_at) AS oldest FROM storefront_renders"
                f" WHERE counted AND {column} = %s AND created_at > %s AND created_at <= %s",
                (value, at - window, at),
            ).fetchone()
            assert row is not None
            if row["n"] >= limit:
                waits.append(_seconds_until(row["oldest"] + window, at))
        return max(waits) if waits else None

    def admit(
        self,
        conn: Conn,
        visitor_id: str,
        client: str,
        *,
        listing_id: str,
        render_key: str,
        settings: Settings,
        now: datetime | None = None,
        colours: Mapping[int, str] | None = None,
    ) -> int:
        """A counted row for a render that costs a run, and its id; `RenderLimited` when
        the visitor, the client or the open renders are at their limit. ``visitor_id``
        is the visitor's row id (``storefront_visitors.id``)."""
        # The concurrency limit is the whole shop's, so every admit takes one lock
        # (`LOCK_OPEN`) and admits run one at a time. Keep this transaction short: a
        # few counts and one insert, never a Temporal call or other I/O.
        with conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s::integer, hashtext(%s)),"
                " pg_advisory_xact_lock(%s::integer, hashtext(%s)),"
                " pg_advisory_xact_lock(%s::integer, 0)",
                (LOCK_VISITOR, visitor_id, LOCK_CLIENT, client, LOCK_OPEN),
            )
            at = now
            if at is None:
                row = conn.execute("SELECT clock_timestamp() AS at").fetchone()
                assert row is not None
                at = row["at"]
            assert at is not None
            for limit, window, most in (
                ("per_minute", MINUTE, settings.storefront_renders_per_minute),
                ("per_day", DAY, settings.storefront_renders_per_day),
            ):
                wait = self._rate(conn, visitor_id, client, window=window, limit=most, at=at)
                if wait is not None:
                    raise RenderLimited(limit, wait)  # type: ignore[arg-type]
            row = conn.execute(
                "SELECT count(*) AS n FROM storefront_renders r"
                " WHERE r.counted AND r.created_at > %s"
                " AND (r.job_id IS NULL OR EXISTS (SELECT 1 FROM render_jobs j"
                "  WHERE j.id = r.job_id AND j.state <> ALL(%s)))",
                (at - timedelta(seconds=settings.render_timeout), list(SETTLED)),
            ).fetchone()
            assert row is not None
            if row["n"] >= settings.storefront_render_concurrency:
                raise RenderLimited("concurrency", CONCURRENCY_RETRY)
            return self._insert(
                conn,
                visitor_id,
                client,
                listing_id=listing_id,
                render_key=render_key,
                counted=True,
                colours=colours or {},
                at=now,
            )

    def free(
        self,
        conn: Conn,
        visitor_id: str,
        client: str,
        *,
        listing_id: str,
        render_key: str,
        colours: Mapping[int, str] | None = None,
    ) -> int:
        """A row for a render the visitor shares with another: it costs nothing, and it
        lets the visitor read the job. Its id."""
        return self._insert(
            conn,
            visitor_id,
            client,
            listing_id=listing_id,
            render_key=render_key,
            counted=False,
            colours=colours or {},
            at=None,
        )

    def attach(self, row_id: int, job_id: str) -> None:
        """The job the row's submit answered with."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE storefront_renders SET job_id = %s WHERE id = %s", (job_id, row_id)
            )

    def uncount(self, row_id: int) -> None:
        """The row's render never ran (the queue was full): it counts against no limit."""
        with self._pool.connection() as conn:
            conn.execute("UPDATE storefront_renders SET counted = false WHERE id = %s", (row_id,))

    def drop(self, conn: Conn, row_id: int) -> None:
        """The row of a render whose submit failed before it named a job: the visitor is
        not charged for it."""
        conn.execute("DELETE FROM storefront_renders WHERE id = %s AND job_id IS NULL", (row_id,))

    def owned(self, visitor_id: str, job_id: str) -> OwnedRender | None:
        """The visitor's newest row for ``job_id``, or None when the visitor never asked
        for that job."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT listing_id, colours FROM storefront_renders"
                " WHERE visitor_id = %s AND job_id = %s ORDER BY id DESC LIMIT 1",
                (visitor_id, job_id),
            ).fetchone()
        if row is None:
            return None
        colours = {int(slot): str(f) for slot, f in row["colours"].items()}
        return OwnedRender(listing_id=row["listing_id"], colours=colours)

    def owns(self, visitor_id: str, job_id: str) -> bool:
        """Whether the visitor asked for ``job_id``."""
        return self.owned(visitor_id, job_id) is not None
