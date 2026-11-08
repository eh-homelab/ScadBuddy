"""A visitor's render limits (#5282, spec 2026-11-02 §5.4): counted in Postgres per visitor
and per client address, a minute's and a day's, and a concurrency limit across every
visitor. A render a visitor shares with another costs nothing."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.core.settings import Settings
from scadbuddy.crud.idempotency import LOCK_CLASS, Replay, record
from scadbuddy.orders.storefront.limits import RenderLimited, RenderLimits
from scadbuddy.orders.storefront.renders import RENDER_ROUTE
from scadbuddy.orders.storefront.visitors import VisitorStore
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS, PgPool

pytestmark = pytest.mark.requires_postgres

T0 = datetime(2026, 11, 8, 12, tzinfo=UTC)
#: The default rates, and room for every render at once: each test of a rate limit
#: renders more than the default concurrency of 2.
SETTINGS = Settings(
    database_url=UNUSED_DATABASE_URL,
    temporal_address=UNUSED_TEMPORAL_ADDRESS,
    storefront_render_concurrency=50,
)
CONCURRENT = SETTINGS.model_copy(update={"storefront_render_concurrency": 2})

type Conn = Connection[DictRow]


def token(n: int) -> str:
    return f"{n:043d}"


@pytest.fixture
def limits(pg_pool: PgPool) -> RenderLimits:
    return RenderLimits(pg_pool)


@pytest.fixture
def conn(pg_pool: PgPool) -> Iterator[Conn]:
    with pg_pool.connection() as conn:
        yield conn


@pytest.fixture
def visitors(pg_pool: PgPool) -> list[str]:
    store = VisitorStore(pg_pool)
    return [store.ensure(token(n)).id for n in range(5)]


@pytest.fixture
def visitor(visitors: list[str]) -> str:
    return visitors[0]


def test_the_fourth_render_in_a_minute_is_limited(
    limits: RenderLimits, conn: Conn, visitor: str
) -> None:
    for n in range(3):
        limits.admit(
            conn,
            visitor,
            "203.0.113.1",
            listing_id="l",
            render_key=f"k{n}",
            settings=SETTINGS,
            now=T0,
        )
    with pytest.raises(RenderLimited) as refused:
        limits.admit(
            conn,
            visitor,
            "203.0.113.1",
            listing_id="l",
            render_key="k3",
            settings=SETTINGS,
            now=T0 + timedelta(seconds=10),
        )
    assert (refused.value.limit, refused.value.retry_after) == ("per_minute", 50)
    # A minute on, the first three have left the window.
    limits.admit(
        conn,
        visitor,
        "203.0.113.1",
        listing_id="l",
        render_key="k3",
        settings=SETTINGS,
        now=T0 + timedelta(seconds=60),
    )


def test_a_new_cookie_on_the_same_client_is_still_limited(
    limits: RenderLimits, conn: Conn, visitors: list[str]
) -> None:
    for n, v in enumerate(visitors[:3]):
        limits.admit(
            conn, v, "203.0.113.1", listing_id="l", render_key=f"k{n}", settings=SETTINGS, now=T0
        )
    with pytest.raises(RenderLimited) as refused:
        limits.admit(
            conn,
            visitors[3],
            "203.0.113.1",
            listing_id="l",
            render_key="k9",
            settings=SETTINGS,
            now=T0,
        )
    assert refused.value.limit == "per_minute"


def test_a_new_address_for_the_same_visitor_is_still_limited(
    limits: RenderLimits, conn: Conn, visitor: str
) -> None:
    for n in range(3):
        limits.admit(
            conn,
            visitor,
            f"203.0.113.{n}",
            listing_id="l",
            render_key=f"k{n}",
            settings=SETTINGS,
            now=T0,
        )
    with pytest.raises(RenderLimited):
        limits.admit(
            conn,
            visitor,
            "198.51.100.7",
            listing_id="l",
            render_key="k9",
            settings=SETTINGS,
            now=T0,
        )


def test_the_day_limit_counts_a_day(limits: RenderLimits, conn: Conn, visitor: str) -> None:
    daily = SETTINGS.model_copy(update={"storefront_renders_per_day": 4})
    for n in range(4):
        limits.admit(
            conn,
            visitor,
            "203.0.113.1",
            listing_id="l",
            render_key=f"k{n}",
            settings=daily,
            now=T0 + timedelta(minutes=n),
        )
    later = T0 + timedelta(hours=1)
    with pytest.raises(RenderLimited) as refused:
        limits.admit(
            conn, visitor, "203.0.113.1", listing_id="l", render_key="k9", settings=daily, now=later
        )
    assert refused.value.limit == "per_day"
    assert refused.value.retry_after == 23 * 3600


def test_concurrency_counts_every_visitor(
    limits: RenderLimits, conn: Conn, visitors: list[str]
) -> None:
    limits.admit(
        conn,
        visitors[0],
        "198.51.100.1",
        listing_id="l",
        render_key="a",
        settings=CONCURRENT,
        now=T0,
    )
    limits.admit(
        conn,
        visitors[1],
        "198.51.100.2",
        listing_id="l",
        render_key="b",
        settings=CONCURRENT,
        now=T0,
    )
    with pytest.raises(RenderLimited) as refused:
        limits.admit(
            conn,
            visitors[2],
            "198.51.100.3",
            listing_id="l",
            render_key="c",
            settings=CONCURRENT,
            now=T0,
        )
    assert (refused.value.limit, refused.value.retry_after) == ("concurrency", 5)
    # A render older than the render timeout is not open any more.
    after = T0 + timedelta(seconds=CONCURRENT.render_timeout + 1)
    limits.admit(
        conn,
        visitors[2],
        "198.51.100.3",
        listing_id="l",
        render_key="c",
        settings=CONCURRENT,
        now=after,
    )


def test_a_settled_render_is_not_open(
    limits: RenderLimits, pg_pool: PgPool, conn: Conn, visitors: list[str]
) -> None:
    row = limits.admit(
        conn,
        visitors[0],
        "198.51.100.1",
        listing_id="l",
        render_key="a",
        settings=CONCURRENT,
        now=T0,
    )
    limits.admit(
        conn,
        visitors[1],
        "198.51.100.2",
        listing_id="l",
        render_key="b",
        settings=CONCURRENT,
        now=T0,
    )
    conn.execute(
        "INSERT INTO render_jobs (id, slug, inputs, state, created_at, render_key)"
        " VALUES ('job-a', 'demo', '{}', 'done', now(), 'a')"
    )
    limits.attach(row, "job-a")
    limits.admit(
        conn,
        visitors[2],
        "198.51.100.3",
        listing_id="l",
        render_key="c",
        settings=CONCURRENT,
        now=T0,
    )


def test_a_free_render_is_not_counted(limits: RenderLimits, conn: Conn, visitor: str) -> None:
    for n in range(5):
        limits.free(conn, visitor, "203.0.113.1", listing_id="l", render_key=f"k{n}")
    for n in range(3):
        limits.admit(
            conn,
            visitor,
            "203.0.113.1",
            listing_id="l",
            render_key=f"c{n}",
            settings=SETTINGS,
            now=T0,
        )


def test_only_the_visitor_who_asked_owns_a_render(
    limits: RenderLimits, conn: Conn, visitors: list[str]
) -> None:
    counted = limits.admit(
        conn, visitors[0], "203.0.113.1", listing_id="l", render_key="k", settings=SETTINGS, now=T0
    )
    shared = limits.free(conn, visitors[1], "203.0.113.2", listing_id="l", render_key="k")
    assert not limits.owns(visitors[0], "job-1")
    limits.attach(counted, "job-1")
    limits.attach(shared, "job-1")
    assert limits.owns(visitors[0], "job-1") and limits.owns(visitors[1], "job-1")
    assert not limits.owns(visitors[2], "job-1")


def test_two_addresses_in_one_64_share_a_render_allowance(
    limits: RenderLimits, conn: Conn, visitors: list[str]
) -> None:
    """Rotating IPv6 addresses inside one /64 is one client, as at the edge."""
    for n, v in enumerate(visitors[:3]):
        limits.admit(
            conn,
            v,
            f"2001:db8:1:2::{n + 1}",
            listing_id="l",
            render_key=f"k{n}",
            settings=SETTINGS,
            now=T0,
        )
    with pytest.raises(RenderLimited) as refused:
        limits.admit(
            conn,
            visitors[3],
            "2001:db8:1:2:ffff::9",
            listing_id="l",
            render_key="k9",
            settings=SETTINGS,
            now=T0,
        )
    assert refused.value.limit == "per_minute"
    shared = limits.free(conn, visitors[4], "2001:db8:1:2::77", listing_id="l", render_key="k")
    row = conn.execute("SELECT client FROM storefront_renders WHERE id = %s", (shared,)).fetchone()
    assert row is not None and row["client"] == "2001:db8:1:2::/64"


@pytest.mark.parametrize(
    ("address", "key"),
    [
        ("203.0.113.1", "203.0.113.1"),
        ("::ffff:203.0.113.1", "203.0.113.1"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
    ],
)
def test_the_client_key_is_shared_by_both_sides(address: str, key: str) -> None:
    from scadbuddy.core.proxies import client_key
    from scadbuddy.storefront import ratelimit

    assert client_key(address) == key
    assert ratelimit.client_key is client_key


def test_attach_needs_the_row(limits: RenderLimits) -> None:
    """A job is never handed to a visitor without a row that owns it."""
    with pytest.raises(LookupError):
        limits.attach(987654, "job-x")


def _ledger(conn: Conn, key: str, row: int) -> None:
    record(conn, "storefront", key, RENDER_ROUTE, {"b": 1}, Replay(200, {"row": row}))


def _ledgered(conn: Conn, key: str) -> bool:
    found = conn.execute(
        "SELECT 1 FROM crud_requests WHERE caller = 'storefront' AND key = %s", (key,)
    ).fetchone()
    return found is not None


def test_undo_forgets_only_the_reply_naming_its_row(
    limits: RenderLimits, conn: Conn, visitor: str
) -> None:
    row = limits.admit(
        conn, visitor, "203.0.113.1", listing_id="l", render_key="k", settings=SETTINGS, now=T0
    )
    _ledger(conn, "mine", row)
    _ledger(conn, "other", row + 1000)
    limits.undo(row, "other")
    assert _ledgered(conn, "other")
    limits.undo(row, "mine")
    assert not _ledgered(conn, "mine")
    assert conn.execute("SELECT 1 FROM storefront_renders WHERE id = %s", (row,)).fetchone() is None


def test_undo_waits_for_the_keys_lock(
    limits: RenderLimits, pg_pool: PgPool, conn: Conn, visitor: str
) -> None:
    row = limits.admit(
        conn, visitor, "203.0.113.1", listing_id="l", render_key="k", settings=SETTINGS, now=T0
    )
    _ledger(conn, "held", row)
    with pg_pool.connection() as holder, holder.transaction():
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s::integer, hashtext(%s))",
            (LOCK_CLASS, "storefront\x1fheld"),
        )
        undone = threading.Thread(target=limits.undo, args=(row, "held"))
        undone.start()
        undone.join(0.5)
        assert undone.is_alive() and _ledgered(conn, "held")
    undone.join(5)
    assert not undone.is_alive() and not _ledgered(conn, "held")
