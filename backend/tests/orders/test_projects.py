"""An order's nested Bambuddy projects (#4253, spec 2026-10-18 §8.1): one per order,
named by its number, and one per item that prints nested under it, each made once."""

from __future__ import annotations

import asyncio

import pytest

from scadbuddy.orders.integrations import NOT_LINKED, SpoolGroup
from scadbuddy.orders.projects import ensure_projects, item_project, item_project_name
from scadbuddy.orders.store import OrderStore, item_subject, order_subject
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop

pytestmark = pytest.mark.requires_postgres


class Projects:
    """The seam with a Bambuddy that makes each project it is asked for."""

    linked = True

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.made: list[tuple[str, int | None]] = []
        self.fail_after = fail_after

    async def spool_groups(self) -> list[SpoolGroup]:
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
