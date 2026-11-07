"""Order events as trigger events (#4252, orders spec §7.1): their keys, the subjects a
trigger is narrowed to, `match`, and the facts read from the order's row at fire time."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.core.events import JobEvent, OrderChanged, OrderItemChanged
from scadbuddy.flow_triggers.events import fire_event, trigger_event
from scadbuddy.flow_triggers.models import EventTiming, Trigger
from scadbuddy.flow_triggers.store import TriggerStore
from scadbuddy.render.projection import JobProjection


def test_an_order_event_is_keyed_by_its_order_and_status() -> None:
    found = trigger_event(OrderChanged(order_id="o1", status="accepted"))
    assert found is not None
    assert found.kind == "order.changed"
    assert found.key == "order:o1:accepted"
    assert found.subjects == {"order": "o1"}
    assert found.facts == {"kind": "order.changed", "order_id": "o1", "status": "accepted"}


def test_an_item_event_is_keyed_by_its_item_status_and_attempt() -> None:
    found = trigger_event(
        OrderItemChanged(order_id="o1", item_id="i1", status="printed", attempt=2)
    )
    assert found is not None
    assert found.kind == "order_item.changed"
    assert found.key == "order_item:i1:printed:2"
    assert found.subjects == {"order": "o1", "order_item": "i1"}
    assert found.facts["attempt"] == 2
    bare = trigger_event(OrderItemChanged(order_id="o1", item_id="i1", status="approved"))
    assert bare is not None and bare.key == "order_item:i1:approved:-"


def test_an_unrelated_event_fires_nothing() -> None:
    assert trigger_event(JobEvent(kind="job.running", job_id="j", slug="box")) is None


# -- the store --------------------------------------------------------------------------

pg = pytest.mark.requires_postgres


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=4)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def triggers(jobs: JobProjection) -> TriggerStore:
    return TriggerStore(jobs.pool)


async def on(triggers: TriggerStore, event: dict[str, Any]) -> str:
    trigger = await triggers.insert(
        Trigger.model_validate(
            {
                "id": str(uuid.uuid4()),
                "name": "fulfil",
                "definition_id": str(uuid.uuid4()),
                "kind": "event",
                "event": EventTiming.model_validate(event),
                "overlap": "skip",
                "paused": False,
                "created_by": {"kind": "browser"},
                "created_at": datetime.now(UTC),
            }
        )
    )
    return trigger.id


async def heard(triggers: TriggerStore, event: OrderChanged | OrderItemChanged) -> list[str]:
    found = trigger_event(event)
    assert found is not None
    return await triggers.event_triggers(found.kind, found.subjects, found.facts)


@pg
async def test_a_trigger_fires_on_its_order_only_and_on_the_statuses_it_matches(
    triggers: TriggerStore,
) -> None:
    every = await on(triggers, {"kind": "order.changed"})
    accepted = await on(triggers, {"kind": "order.changed", "match": {"status": ["accepted"]}})
    mine = await on(triggers, {"kind": "order.changed", "subject": {"kind": "order", "id": "o1"}})
    assert sorted(await heard(triggers, OrderChanged(order_id="o1", status="accepted"))) == sorted(
        [every, accepted, mine]
    )
    assert await heard(triggers, OrderChanged(order_id="o2", status="quoted")) == [every]


@pg
async def test_an_item_trigger_fires_on_its_order_or_its_item(triggers: TriggerStore) -> None:
    of_order = await on(
        triggers, {"kind": "order_item.changed", "subject": {"kind": "order", "id": "o1"}}
    )
    of_item = await on(
        triggers,
        {
            "kind": "order_item.changed",
            "subject": {"kind": "order_item", "id": "i1"},
            "match": {"status": ["printed", "failed"]},
        },
    )
    printed = OrderItemChanged(order_id="o1", item_id="i1", status="printed", attempt=1)
    assert sorted(await heard(triggers, printed)) == sorted([of_order, of_item])
    queued = OrderItemChanged(order_id="o1", item_id="i1", status="queued", attempt=1)
    assert await heard(triggers, queued) == [of_order]
    other = OrderItemChanged(order_id="o1", item_id="i2", status="printed", attempt=1)
    assert await heard(triggers, other) == [of_order]


@pg
async def test_a_trigger_stored_with_a_slug_still_fires_on_its_template(
    triggers: TriggerStore, jobs: JobProjection
) -> None:
    trigger_id = await on(triggers, {"kind": "render.finished"})
    with jobs.pool.connection() as conn:
        conn.execute(
            "UPDATE flow_triggers SET event = %s WHERE id = %s",
            (Jsonb({"kind": "render.finished", "slug": "box"}), trigger_id),
        )
    stored = await triggers.get(trigger_id)
    assert stored is not None and stored.event is not None
    assert stored.event.subject is not None and stored.event.subject.id == "box"
    box = trigger_event(JobEvent(kind="job.done", job_id="j", slug="box"))
    other = trigger_event(JobEvent(kind="job.done", job_id="j", slug="cup"))
    assert box is not None and other is not None
    assert await triggers.event_triggers(box.kind, box.subjects, box.facts) == [trigger_id]
    assert await triggers.event_triggers(other.kind, other.subjects, other.facts) == []


@pg
async def test_a_stored_event_that_no_longer_reads_stops_only_its_own_trigger(
    triggers: TriggerStore, jobs: JobProjection
) -> None:
    broken = await on(triggers, {"kind": "order.changed"})
    fine = await on(triggers, {"kind": "order.changed"})
    with jobs.pool.connection() as conn:
        conn.execute(
            "UPDATE flow_triggers SET event = %s WHERE id = %s",
            (Jsonb({"kind": "order.changed", "match": {"status": ["gone"]}}), broken),
        )
    assert await heard(triggers, OrderChanged(order_id="o1", status="quoted")) == [fine]


class _Client:
    def __init__(self) -> None:
        self.started: list[tuple[str, list[Any]]] = []

    async def start_workflow(self, _name: str, *, args: list[Any], id: str, **_: Any) -> None:
        self.started.append((id, args))


@pg
async def test_a_fire_takes_the_customer_from_the_order_row(
    triggers: TriggerStore, jobs: JobProjection
) -> None:
    with jobs.pool.connection() as conn:
        conn.execute("INSERT INTO customers (id, display_name) VALUES ('c1', 'C')")
        conn.execute(
            "INSERT INTO orders (id, number, customer_id, currency, created_by)"
            " VALUES ('o1', 'ORD-2026-1', 'c1', 'USD', '{}')"
        )
    trigger_id = await on(triggers, {"kind": "order.changed"})
    client = _Client()
    started = await fire_event(
        client,  # type: ignore[arg-type]
        "library",
        triggers,
        OrderChanged(order_id="o1", status="accepted"),
    )
    assert len(started) == 1
    (_, args) = client.started[0]
    assert args == [
        trigger_id,
        {"kind": "order.changed", "order_id": "o1", "status": "accepted", "customer_id": "c1"},
    ]
    # An order gone since fires nothing.
    assert (
        await fire_event(
            client,  # type: ignore[arg-type]
            "library",
            triggers,
            OrderChanged(order_id="gone", status="accepted"),
        )
        == []
    )


@pg
async def test_a_return_to_a_status_fires_again_and_an_edit_in_it_does_not(
    triggers: TriggerStore, jobs: JobProjection
) -> None:
    """The key names the transition (§7.1, review of #5644): `ready`, an edit while
    `ready`, `in_production`, `ready` again is two fires on `ready`, not one, nor three."""
    with jobs.pool.connection() as conn:
        conn.execute("INSERT INTO customers (id, display_name) VALUES ('c1', 'C')")
        conn.execute(
            "INSERT INTO orders (id, number, customer_id, currency, created_by)"
            " VALUES ('o1', 'ORD-2026-1', 'c1', 'USD', '{}')"
        )
        conn.execute(
            "INSERT INTO order_items (id, order_id, position, quantity, title, kind)"
            " VALUES ('i1', 'o1', 0, 1, 'Bracket', 'custom')"
        )
    await on(triggers, {"kind": "order.changed", "match": {"status": ["ready"]}})
    await on(triggers, {"kind": "order_item.changed", "match": {"status": ["approved"]}})
    client = _Client()

    async def moved(status: str) -> list[str]:
        with jobs.pool.connection() as conn:
            conn.execute("UPDATE orders SET status = %s WHERE id = 'o1'", (status,))
        event = OrderChanged(order_id="o1", status=status)
        return await fire_event(client, "library", triggers, event)  # type: ignore[arg-type]

    first = await moved("ready")
    again = await fire_event(
        client,  # type: ignore[arg-type]
        "library",
        triggers,
        OrderChanged(order_id="o1", status="ready"),
    )
    assert await moved("in_production") == []
    second = await moved("ready")
    assert len(first) == len(again) == len(second) == 1
    assert first == again  # the edit keys as the change before it: Temporal starts it once
    assert second != first

    async def item(status: str) -> list[str]:
        with jobs.pool.connection() as conn:
            conn.execute("UPDATE order_items SET status = %s WHERE id = 'i1'", (status,))
        event = OrderItemChanged(order_id="o1", item_id="i1", status=status)
        return await fire_event(client, "library", triggers, event)  # type: ignore[arg-type]

    approved = await item("approved")
    assert await item("failed") == []
    assert (await item("approved")) != approved
