"""The inventory's operations on the Activities tab (#6949 K1, R-K1-22): Go writes an
`inventory_operations` row and sends `inventory_operation.changed`; the activity component
ingests it as the agent's operations, and the housekeeping repair fills a notice it
missed (C13)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from scadbuddy.activity.component import ActivityComponent, _run
from scadbuddy.activity.index import ActivityIndex
from scadbuddy.activity.kinds import discover_kinds
from scadbuddy.core.events import Event, InventoryOperationEvent
from scadbuddy.inventory.activity import ACTIVITY_KINDS
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

#: Task 12's activities, by name.
ACTIVITIES = (
    "KindCreate",
    "KindUpdate",
    "KindArchive",
    "KindRestore",
    "ItemCreate",
    "ItemUpdate",
    "ItemArchive",
    "ItemRestore",
    "ReceiveLot",
    "ReceiveUnits",
    "LotUpdate",
    "LotAdjust",
    "Take",
    "Count",
    "UnitUpdate",
    "UnitStatus",
    "GiveBack",
    "Recost",
    "Repoint",
)
ITEM = "00000000-0000-4000-8000-000000000001"


class Bus:
    """The listeners part of the event bus: what `_run` uses."""

    def __init__(self) -> None:
        self.listeners: list[Callable[[Event], None]] = []

    def add_listener(self, listener: Callable[[Event], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def publish(self, event: Event) -> None:
        for listener in list(self.listeners):
            listener(event)


def _operation(pool: PgPool, op_id: str, **row: Any) -> None:
    fields = {
        "kind": "inventory.Take",
        "subject": f"item:{ITEM}",
        "status": "running",
        "actor": {"kind": "browser", "principal": "p1"},
        **row,
    }
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO inventory_operations (id, kind, subject, status, actor, problem,"
            " finished_at) VALUES (%s, %s, %s, %s, %s, %s,"
            " CASE WHEN %s = 'running' THEN NULL ELSE now() END)"
            " ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status,"
            " problem = EXCLUDED.problem, finished_at = EXCLUDED.finished_at",
            (
                op_id,
                fields["kind"],
                fields["subject"],
                fields["status"],
                Jsonb(fields["actor"]),
                Jsonb(fields.get("problem")) if fields.get("problem") else None,
                fields["status"],
            ),
        )


async def _settled(index: ActivityIndex, activity_id: str, status: str) -> Any:
    for _ in range(100):
        row = await index.get(activity_id)
        if row is not None and row.status == status:
            return row
        await asyncio.sleep(0.02)
    raise AssertionError(f"{activity_id} never read {status}")


async def test_an_operation_notice_writes_one_activity_row(pg_pool: PgPool) -> None:
    index = ActivityIndex(pg_pool, events=None)
    bus = Bus()
    component = ActivityComponent(index=index, kinds=lambda: discover_kinds([], []), bus=bus)  # type: ignore[arg-type]
    _operation(pg_pool, "browser:p1:k")

    async with _run(component):
        bus.publish(InventoryOperationEvent(operation_id="browser:p1:k"))
        running = await _settled(index, "inventory_operation:browser:p1:k", "running")
        assert (running.kind, running.subject, running.source, running.source_id) == (
            "inventory.Take",
            f"item:{ITEM}",
            "inventory_operations",
            "browser:p1:k",
        )
        assert running.title == f"Take stock item:{ITEM}"
        assert running.finished_at is None

        _operation(pg_pool, "browser:p1:k", status="succeeded")
        bus.publish(InventoryOperationEvent(operation_id="browser:p1:k"))
        done = await _settled(index, "inventory_operation:browser:p1:k", "succeeded")

    assert done.finished_at is not None
    assert done.principal == "p1"
    with pg_pool.connection() as conn:
        rows = conn.execute(
            "SELECT count(*) AS n FROM activity WHERE source = 'inventory_operations'"
        ).fetchone()
    assert rows is not None and rows["n"] == 1


async def test_a_failed_operation_keeps_its_problem(pg_pool: PgPool) -> None:
    index = ActivityIndex(pg_pool, events=None)
    problem = {"type": "https://scadbuddy.dev/problems/inventory/lot_empty", "code": "lot_empty"}
    _operation(pg_pool, "req:1", status="failed", problem=problem)

    await index.ingest_inventory_operation("req:1")

    row = await index.get("inventory_operation:req:1")
    assert row is not None
    assert row.status == "failed"
    assert row.detail["problem"] == problem


async def test_an_unknown_operation_is_a_no_op(pg_pool: PgPool) -> None:
    index = ActivityIndex(pg_pool, events=None)

    await index.ingest_inventory_operation("nope")

    assert await index.get("inventory_operation:nope") is None


async def test_a_missed_notice_is_repaired(pg_pool: PgPool) -> None:
    index = ActivityIndex(pg_pool, events=None)
    _operation(pg_pool, "browser:p1:missed", status="succeeded", kind="inventory.ReceiveLot")

    await index.repair(3600)

    row = await index.get("inventory_operation:browser:p1:missed")
    assert row is not None
    assert (row.kind, row.status, row.title) == (
        "inventory.ReceiveLot",
        "succeeded",
        f"Receive a lot item:{ITEM}",
    )


def test_every_activity_has_a_kind() -> None:
    registry = discover_kinds([], [])
    keys = {kind.key for kind in ACTIVITY_KINDS}

    assert keys == {f"inventory.{name}" for name in ACTIVITIES}
    for key in keys:
        assert registry.get(key) is not None, key
