"""A printer merge and the stock that names the gone printer (#6949 K1 Task 16): Go writes
the stock rows, so a merge whose gone printer they name is `MergePrinter` (Go's
`inventory.Repoint`, then the merge's own transaction), and a merge they do not name runs
in line as before. Here the workflow runs in line (`tests/support/order_stock.py`); on a real
Temporal it is `tests/workflows/test_order_stock.py`'s."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from scadbuddy.bambuddy.models import Printer
from scadbuddy.printers.bambuddy_sync import printer_view
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import (
    PRINTER_REFERENCES,
    BambuddyLinkTakenError,
    MergeChangedError,
    PrinterNotFoundError,
    PrinterReference,
    PrinterStore,
    SerialTakenError,
    VirtualPrinterRowError,
    printer_error,
    printer_problem,
)
from tests.conftest import PgPool
from tests.printers.test_bambuddy_sync import FakeClient, FakePrinters, bambuddy, run
from tests.support.order_stock import GoStock, InlineStarter
from tests.support.printers import MERGED_BY

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000006949"


@pytest.fixture
def go(pg_pool: PgPool) -> GoStock:
    return GoStock(pg_pool)


@pytest.fixture
def starter(printers: PrinterStore, go: GoStock) -> InlineStarter:
    started = InlineStarter(None, go, lambda: printers)
    printers.merges = started
    return started


def _unit(pool: PgPool, printer_id: str) -> UUID:
    """A stock unit that is the printer, written as Go writes it."""
    with pool.connection() as conn:
        item = conn.execute(
            "INSERT INTO items (kind_id, name) VALUES ('printer', 'H2C') RETURNING id"
        ).fetchone()
        assert item is not None
        unit = conn.execute(
            "INSERT INTO stock_units (item_id, printer_id, cost_minor, origin, created_by)"
            " VALUES (%s, %s, 0, 'manual', '{}') RETURNING id",
            (item["id"], printer_id),
        ).fetchone()
    assert unit is not None
    return UUID(str(unit["id"]))


def _unit_printer(pool: PgPool, unit: UUID) -> str:
    with pool.connection() as conn:
        row = conn.execute("SELECT printer_id FROM stock_units WHERE id = %s", (unit,)).fetchone()
    assert row is not None
    return str(row["printer_id"])


def _move_on(pool: PgPool, printer_id: str) -> UUID:
    """A stock move whose entity is the printer, written as Go writes it."""
    with pool.connection() as conn:
        entity = conn.execute(
            "INSERT INTO entities (kind, ref) VALUES ('printer', %s) RETURNING id", (printer_id,)
        ).fetchone()
        item = conn.execute(
            "INSERT INTO items (kind_id, name) VALUES ('hardware', 'Nozzle') RETURNING id"
        ).fetchone()
        assert entity is not None and item is not None
        lot = conn.execute(
            "INSERT INTO stock_lots (item_id, received_qty, on_hand, unit_cost_minor, origin,"
            " created_by) VALUES (%s, 1, 0, 0, 'manual', '{}') RETURNING id",
            (item["id"],),
        ).fetchone()
        assert lot is not None
        move = conn.execute(
            "INSERT INTO stock_moves (item_id, lot_id, kind, qty, unit_cost_minor, amount_minor,"
            " entity_id, actor) VALUES (%s, %s, 'use', -1, 0, 0, %s, '{}') RETURNING id",
            (item["id"], lot["id"], entity["id"]),
        ).fetchone()
    assert move is not None
    return UUID(str(move["id"]))


def _move_entity(pool: PgPool, move: UUID) -> str:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT e.ref FROM stock_moves m JOIN entities e ON e.id = m.entity_id WHERE m.id = %s",
            (move,),
        ).fetchone()
    assert row is not None
    return str(row["ref"])


def test_no_stock_table_is_a_printer_reference() -> None:
    """Go is the one writer of the stock tables: the merge's own list names none of them."""
    tables = {ref.table for ref in PRINTER_REFERENCES if isinstance(ref, PrinterReference)}
    assert not tables & {"stock_units", "stock_moves", "stock_lots"}


def test_a_merge_with_no_stock_runs_inline(printers: PrinterStore, starter: InlineStarter) -> None:
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))

    kept = printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C", by=MERGED_BY)

    assert kept.id == mine.id and printers.get(synthetic.id) is None
    assert starter.started == []


def test_a_merge_repoints_a_stock_unit_then_merges(
    printers: PrinterStore, starter: InlineStarter, go: GoStock, pg_pool: PgPool
) -> None:
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    unit = _unit(pg_pool, synthetic.id)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))

    kept = printers.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C", by=MERGED_BY)

    assert kept.id == mine.id and printers.get(synthetic.id) is None
    [started] = starter.started
    assert (started.workflow, started.id) == (
        "MergePrinter",
        f"entity_merge:printer:{synthetic.id}:{mine.id}",
    )
    assert started.arg["then"] == "ensure"
    [repoint] = go.named("inventory.Repoint")
    assert repoint.arg["body"] == {"kind": "printer", "gone": synthetic.id, "kept": mine.id}
    assert _unit_printer(pg_pool, unit) == mine.id


def test_a_link_repoints_a_stock_move_then_merges(
    printers: PrinterStore, starter: InlineStarter, pg_pool: PgPool
) -> None:
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    move = _move_on(pg_pool, synthetic.id)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))

    linked = printers.link_bambuddy(mine.id, 4, MERGED_BY)

    assert linked.id == mine.id and linked.bambuddy_printer_id == 4
    assert printers.get(synthetic.id) is None
    assert [s.arg["then"] for s in starter.started] == ["link"]
    assert _move_entity(pg_pool, move) == mine.id


def test_the_merge_step_runs_again_while_stock_names_the_gone_printer(
    printers: PrinterStore, pg_pool: PgPool
) -> None:
    """Step 2 checks under the gone printer's lock that no stock row names it: one a stock
    write added after the Repoint rolls the step back, and the workflow runs both again."""
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    _unit(pg_pool, synthetic.id)
    args = {"bambuddy_printer_id": 4, "serial": SERIAL, "name": "H2C", "model": "H2C"}

    step = printers.merge_commit(synthetic.id, mine.id, "ensure", args, MERGED_BY)

    assert step == {"again": True}
    assert printers.get(synthetic.id) is not None
    assert printers.require(mine.id).bambuddy_printer_id is None


def test_the_merge_step_refuses_another_pair(printers: PrinterStore) -> None:
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    args = {"bambuddy_printer_id": 4, "serial": SERIAL, "name": "H2C", "model": "H2C"}

    with pytest.raises(MergeChangedError):
        printers.merge_commit(synthetic.id, "another", "ensure", args, MERGED_BY)
    assert printers.get(synthetic.id) is not None and printers.get(mine.id) is not None


class ConflictingGo(GoStock):
    """Go's Repoint, after which the kept row is linked to another Bambuddy printer: the
    link's own check refuses in step 2."""

    def __init__(self, pool: Any, kept: str) -> None:
        super().__init__(pool)
        self.kept = kept

    def _Repoint(self, arg: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        answer = super()._Repoint(arg)
        if "only" not in arg["body"]:
            with self.pool.connection() as conn:
                conn.execute(
                    "UPDATE printers SET bambuddy_printer_id = 9 WHERE id = %s", (self.kept,)
                )
        return answer


def test_a_refused_merge_moves_stock_back(printers: PrinterStore, pg_pool: PgPool) -> None:
    synthetic = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C", by=MERGED_BY)
    unit = _unit(pg_pool, synthetic.id)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    go = ConflictingGo(pg_pool, mine.id)
    printers.merges = InlineStarter(None, go, lambda: printers)

    with pytest.raises(BambuddyLinkTakenError):
        printers.link_bambuddy(mine.id, 4, MERGED_BY)

    forward, back = go.named("inventory.Repoint")
    assert back.arg["body"] == {
        "kind": "printer",
        "gone": mine.id,
        "kept": synthetic.id,
        "only": {"moves": [], "units": [str(unit)]},
    }
    assert back.arg["key"] == forward.arg["key"] + ":back"
    assert _unit_printer(pg_pool, unit) == synthetic.id
    assert printers.get(synthetic.id) is not None


async def test_the_sync_reports_merging(pg_pool: PgPool, go: GoStock) -> None:
    """A sync's merge whose workflow still runs after the wait: the entry's action is
    `merging`, with the workflow; it ends by itself."""
    store = PrinterStore(pg_pool, None)
    store.merges = InlineStarter(None, go, lambda: store, accept=True)
    synthetic = store.ensure_bambuddy(1, serial=None, name="P1", model="H2C", by=MERGED_BY)
    printer = Printer.model_validate(bambuddy(1, SERIAL, ip=None))
    store.record_bambuddy_view(synthetic.id, printer_view(printer, datetime.now(UTC)))
    _unit(pg_pool, synthetic.id)
    holder = store.create(PrinterCreate(serial=SERIAL, name="Mine", model="H2C"))

    result = await run(FakePrinters(store), FakeClient([bambuddy(1, SERIAL, ip=None)]))

    (entry,) = result.entries
    assert entry.action == "merging"
    assert entry.workflow_id == f"entity_merge:printer:{synthetic.id}:{holder.id}"
    assert store.get(synthetic.id) is not None


@pytest.mark.parametrize(
    "error",
    [
        PrinterNotFoundError("p1"),
        SerialTakenError("p2"),
        BambuddyLinkTakenError("p3", 7),
        VirtualPrinterRowError("p4"),
        MergeChangedError("p5", "p6"),
    ],
)
def test_a_merge_refusal_crosses_the_workflow_whole(error: Exception) -> None:
    problem = printer_problem(error)
    assert problem is not None
    again = printer_error(problem)
    assert type(again) is type(error) and str(again) == str(error)
