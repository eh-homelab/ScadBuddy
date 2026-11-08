"""An order's nested Bambuddy projects (#4253, spec 2026-10-18 §8.1): one per order,
named by its number, and one per item that prints nested under it, each made once."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

import scadbuddy.orders.projects as projects_module
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.integrations import (
    NOT_LINKED,
    ArchiveFacts,
    ArchiveMedia,
    CopiedMedia,
    MaintenanceTask,
    ShelfSpool,
    SpoolGroup,
)
from scadbuddy.orders.printing import print_item
from scadbuddy.orders.projects import ensure_projects, item_project, item_project_name
from scadbuddy.orders.store import OrderStore, item_subject, order_subject
from scadbuddy.rows.component import POOL_SIZE as ROW_POOL_SIZE
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop
from tests.orders.test_printing import Starts, run_row, set_run, started

pytestmark = pytest.mark.requires_postgres


class Projects:
    """The seam with a Bambuddy that makes each project it is asked for."""

    linked = True

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.made: list[tuple[str, int | None]] = []
        self.fail_after = fail_after
        #: Projects deleted in Bambuddy since they were made.
        self.deleted: set[int] = set()
        #: Every ``project_exists`` asked, in order.
        self.asked: list[int] = []

    async def project_exists(self, project_id: int) -> bool:
        self.asked.append(project_id)
        return project_id not in self.deleted

    async def spool_inventory(self) -> list[ShelfSpool]:
        raise AssertionError("not asked")

    async def spool_groups(self) -> list[SpoolGroup]:
        raise AssertionError("not asked")

    async def maintenance_tasks(self, printer_id: int) -> list[MaintenanceTask]:
        raise AssertionError("not asked")

    async def archive(self, archive_id: int) -> ArchiveFacts | None:
        raise AssertionError("not asked")

    async def archive_cost(self, archive_id: int) -> float | None:
        raise AssertionError("not asked")

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> CopiedMedia:
        raise AssertionError("not asked")

    async def ensure_project(self, name: str, parent_id: int | None) -> int:
        if self.fail_after is not None and len(self.made) >= self.fail_after:
            raise RuntimeError("Bambuddy is down")
        # Slow enough that a second ensure would overlap without the lock.
        await asyncio.sleep(0.05)
        self.made.append((name, parent_id))
        return 100 + len(self.made)


@pytest.fixture(name="shop")
def _shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def test_an_accepted_order_gets_its_project_and_one_per_item(shop: Shop) -> None:
    order = shop.order()
    first, second = shop.item(order), shop.item(order)
    shop.quoted(order, first)
    shop.quoted(order, second)
    order = shop.lifecycle.accept_order(order.id, BY)
    seam = Projects()
    projects = asyncio.run(ensure_projects(shop.rows, seam, order.id))
    assert seam.made == [
        (order.number, None),
        (item_project_name(order, shop.get_item(first)), 101),
        (item_project_name(order, shop.get_item(second)), 101),
    ]
    assert [(p.subject, p.project_id, p.item_id) for p in projects] == [
        (order_subject(order.id), 101, None),
        (item_subject(first.id), 102, first.id),
        (item_subject(second.id), 103, second.id),
    ]
    assert item_project_name(order, shop.get_item(first)) == f"{order.number} · 1 Demo"
    # Ensured again (a print's), nothing new is made.
    assert asyncio.run(item_project(shop.rows, seam, order.id, first.id)) == 102
    assert len(seam.made) == 3


def test_two_ensures_at_once_make_each_project_once(shop: Shop) -> None:
    order, item = shop.accepted()
    seam = Projects()

    async def both() -> None:
        await asyncio.gather(
            ensure_projects(shop.rows, seam, order.id),
            item_project(shop.rows, seam, order.id, item.id),
        )

    asyncio.run(both())
    assert [name for name, _ in seam.made] == [order.number, item_project_name(order, item)]


def test_a_failed_ensure_keeps_what_it_made_and_the_print_makes_the_rest(shop: Shop) -> None:
    order, item = shop.accepted()
    with pytest.raises(RuntimeError):
        asyncio.run(ensure_projects(shop.rows, Projects(fail_after=1), order.id))
    assert [p.subject for p in shop.rows.projects(order.id)] == [order_subject(order.id)]
    seam = Projects()
    assert asyncio.run(item_project(shop.rows, seam, order.id, item.id)) == 101
    # The order's project was kept: only the item's is made, under it.
    assert seam.made == [(item_project_name(order, item), 101)]


@pytest.fixture(name="expire")
def _expire(pool: PgPool) -> Callable[[], None]:
    """Every project recorded taken as seen in Bambuddy longer ago than
    ``EXISTS_TTL_SECONDS``."""

    def expire() -> None:
        with pool.connection() as conn:
            conn.execute(
                "UPDATE order_projects SET seen_at = seen_at - make_interval(secs => %s)",
                (projects_module.EXISTS_TTL_SECONDS + 1,),
            )

    return expire


def test_a_project_deleted_in_bambuddy_is_made_again(
    shop: Shop, expire: Callable[[], None]
) -> None:
    """#5263: the mapping names a project Bambuddy no longer has; the next ensure past
    the time a project is known to stand (#5273) makes it again and records the new
    one, the order's and the item's alike."""
    order, item = shop.accepted()
    seam = Projects()
    assert asyncio.run(item_project(shop.rows, seam, order.id, item.id)) == 102
    seam.deleted.add(102)
    expire()
    assert asyncio.run(item_project(shop.rows, seam, order.id, item.id)) == 103
    assert seam.made[-1] == (item_project_name(order, item), 101)
    seam.deleted.update({101, 103})
    expire()
    assert asyncio.run(item_project(shop.rows, seam, order.id, item.id)) == 105
    assert seam.made[-2:] == [(order.number, None), (item_project_name(order, item), 104)]
    assert [(p.subject, p.project_id) for p in shop.rows.projects(order.id)] == [
        (order_subject(order.id), 104),
        (item_subject(item.id), 105),
    ]


def test_a_project_known_to_stand_is_not_asked_again(
    shop: Shop, expire: Callable[[], None]
) -> None:
    """#5273: an accept, then each print, asked Bambuddy for every project it already
    had; one made or found there just now is taken as standing."""
    order, item = shop.accepted()
    seam = Projects()
    asyncio.run(ensure_projects(shop.rows, seam, order.id))
    # Made just now: nothing to ask.
    assert asyncio.run(item_project(shop.rows, seam, order.id, item.id)) == 102
    assert seam.asked == []
    expire()
    assert asyncio.run(item_project(shop.rows, seam, order.id, item.id)) == 102
    assert seam.asked == [101, 102]
    assert asyncio.run(item_project(shop.rows, seam, order.id, item.id)) == 102
    assert seam.asked == [101, 102]


def test_every_replica_takes_a_project_seen_by_one_as_standing(shop: Shop) -> None:
    """#5276: the time a project was seen is the shop's record, not a replica's memory
    (which needed a lock, and which a failed print on another replica could not
    forget): an ensure on another replica, its own seam, asks nothing either."""
    order, item = shop.accepted()
    asyncio.run(ensure_projects(shop.rows, Projects(), order.id))
    other = Projects()
    assert asyncio.run(item_project(shop.rows, other, order.id, item.id)) == 102
    assert other.asked == [] and other.made == []


def test_a_failed_print_run_asks_bambuddy_for_its_projects_again(shop: Shop, pool: PgPool) -> None:
    """#5276: the item's project was deleted in Bambuddy just after it was seen, so its
    print failed; the retry within ``EXISTS_TTL_SECONDS`` asks again and files under the
    project made again, not the stale id."""
    order, item = shop.accepted()
    seam = Projects()
    assert asyncio.run(item_project(shop.rows, seam, order.id, item.id)) == 102
    run_id = run_row(pool)
    started(shop, order, item, run_id)
    seam.deleted.add(102)
    set_run(pool, run_id, "failed", "no project 102")
    assert shop.lifecycle.follow_print(run_id) is not None
    assert asyncio.run(item_project(shop.rows, seam, order.id, item.id)) == 103
    assert seam.asked == [101, 102]


def test_a_refused_print_start_asks_bambuddy_for_its_projects_again(
    shop: Shop, pool: PgPool
) -> None:
    """#5276: as a failed run, a start the print path refused (a project it cannot
    file under among the reasons) is not taken to stand."""
    order, item = shop.accepted()
    seam = Projects()
    assert asyncio.run(item_project(shop.rows, seam, order.id, item.id)) == 102
    seam.deleted.add(102)
    start = Starts(pool, refuse=ApiError(404, "no project 102"))
    with pytest.raises(ApiError):
        asyncio.run(
            print_item(
                shop.lifecycle,
                start,
                start.existing,
                order.id,
                item.id,
                BY,
                request_id="req-refused",
                sliced_sha256="a" * 64,
                sliced_blob_key="quote-a",
                project_id=102,
            )
        )
    assert asyncio.run(item_project(shop.rows, seam, order.id, item.id)) == 103


def test_a_refused_start_releases_its_claim_when_the_forget_fails(
    shop: Shop, pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5277 review: forgetting the projects is best-effort, after the release, so a
    database error there neither keeps the claim nor hides the refusal."""
    order, item = shop.accepted()

    def fails(order_id: str, *, conn: object = None) -> None:
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(shop.rows, "forget_projects_seen", fails)
    start = Starts(pool, refuse=ApiError(422, "no spool"))
    with pytest.raises(ApiError) as refused:
        asyncio.run(
            print_item(
                shop.lifecycle,
                start,
                start.existing,
                order.id,
                item.id,
                BY,
                request_id="req-forget-fails",
                sliced_sha256="a" * 64,
                sliced_blob_key="quote-a",
            )
        )
    assert refused.value.status == 422
    assert shop.rows.fulfilments(item.id) == []
    assert shop.get_item(item).status == "approved"


def test_without_bambuddy_nothing_is_asked_or_recorded(shop: Shop) -> None:
    order, item = shop.accepted()
    assert asyncio.run(ensure_projects(shop.rows, NOT_LINKED, order.id)) == []
    assert asyncio.run(item_project(shop.rows, NOT_LINKED, order.id, item.id)) is None


def test_a_cancel_keeps_the_projects_for_a_reopen(shop: Shop) -> None:
    order, item = shop.accepted()
    seam = Projects()
    asyncio.run(ensure_projects(shop.rows, seam, order.id))
    shop.lifecycle.cancel_order(order.id, BY)
    shop.lifecycle.reopen_order(order.id, BY)
    assert asyncio.run(item_project(shop.rows, seam, order.id, item.id)) == 102
    assert len(seam.made) == 2


def _advisory_locks(pool: PgPool) -> int:
    """Granted advisory locks in this database only: `pg_locks` spans the cluster, and
    under xdist every other worker's database holds locks of its own."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND granted"
            " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        ).fetchone()
    assert row is not None
    return int(row["n"])


def _settled(pool: PgPool) -> int:
    """The advisory locks held once a late thread has had time to let its own go."""
    deadline = time.monotonic() + 10
    while _advisory_locks(pool) and time.monotonic() < deadline:
        time.sleep(0.05)
    return _advisory_locks(pool)


def test_a_cancel_while_waiting_for_the_lock_leaves_it_free(shop: Shop, pool: PgPool) -> None:
    """The ensure is cancelled while its thread waits for the lock another holds: once
    that holder lets go, the thread's late acquisition is released, not leaked."""
    order, _ = shop.accepted()
    holder = shop.rows.project_lock(order.id)
    holder.__enter__()
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            holder.__exit__(None, None, None)

    try:

        async def cancelled() -> None:
            task = asyncio.create_task(ensure_projects(shop.rows, Projects(), order.id))
            await asyncio.sleep(0.3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Only now may the waiting thread take the lock: after its task is gone.
            release()

        asyncio.run(cancelled())
        assert _settled(pool) == 0
    finally:
        release()


def test_a_bambuddy_that_never_answers_lets_the_lock_go(
    shop: Shop, pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(projects_module, "ENSURE_TIMEOUT_SECONDS", 0.2)
    order, _ = shop.accepted()

    class Hangs(Projects):
        async def ensure_project(self, name: str, parent_id: int | None) -> int:
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    with pytest.raises(TimeoutError):
        asyncio.run(ensure_projects(shop.rows, Hangs(), order.id))
    assert _settled(pool) == 0


def test_more_ensures_than_the_pool_holds_all_finish(pg_conninfo: str, pool: PgPool) -> None:
    """The lock is waited for and held on a connection of its own: ensures queued on one
    order, and others of other orders, never take the row pool (`ROW_POOL_SIZE`) the
    holder needs for its reads and writes."""
    small: PgPool = ConnectionPool(
        pg_conninfo,
        min_size=1,
        max_size=ROW_POOL_SIZE,
        timeout=3,
        open=False,
        connection_class=Connection[DictRow],
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    small.open(wait=True, timeout=30)
    try:
        shop = Shop(OrderStore(small), BlobRefs(pool))
        busy = shop.order()
        items = [shop.item(busy) for _ in range(5)]
        for item in items:
            shop.quoted(busy, item)
        busy = shop.lifecycle.accept_order(busy.id, BY)
        others = [shop.accepted()[0] for _ in range(3)]
        seam = Projects()

        async def all_at_once() -> None:
            await asyncio.gather(
                *(item_project(shop.rows, seam, busy.id, item.id) for item in items),
                *(ensure_projects(shop.rows, seam, order.id) for order in others),
            )

        asyncio.run(all_at_once())
        names = [name for name, _ in seam.made]
        assert len(names) == len(set(names)) == 1 + len(items) + 2 * len(others)
        assert len(shop.rows.projects(busy.id)) == 1 + len(items)
    finally:
        small.close()
