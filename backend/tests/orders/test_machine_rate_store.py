"""A printer's machine rate inputs and the rate read from them (#4625, order operations
spec 2026-11-07 §6.1, §6.2): the three tables, Bambuddy's intervals kept for when it does
not answer, and the printer a quote is priced for."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from scadbuddy.orders.integrations import MaintenanceTask, OrderIntegrations
from scadbuddy.orders.machine_rate import (
    CostProfile,
    EventFieldError,
    ImportedEventError,
    MachineRateEvent,
    MachineRateEventCreate,
    MachineRateEventPatch,
    MachineRateInputs,
    MachineRates,
    MachineRateStore,
    MaintenanceCost,
    Stored,
    WearPart,
)
from scadbuddy.printers.models import PrinterCreate, PrinterRow
from scadbuddy.printers.store import PrinterStore
from tests.conftest import UNUSED_TEMPORAL_ADDRESS, PgPool

pytestmark = pytest.mark.requires_postgres

INPUTS = MachineRateInputs(
    profile=CostProfile(
        purchase_price="1440",
        life_hours="3000",
        watts="120",
        kwh_price="0.25",
        labour_rate_per_hour="30",
        # As a read gives them back (#5774: a `PUT` that leaves them out keeps them).
        actuals_window=3,
        lan_hours_offset="0",
    ),
    wear_parts=[WearPart(name="Nozzle", cost="14", life_hours="200", priced_by="estimate")],
    maintenance=[
        MaintenanceCost(
            task="Lubricate rods",
            minutes="20",
            consumables="2",
            interval_hours="100",
            priced_by="estimate",
        )
    ],
)


class FakeBambuddy:
    def __init__(
        self,
        tasks: list[MaintenanceTask] | None,
        *,
        garbled: bool = False,
        not_json: bool = False,
        not_utf8: bool = False,
        bug: bool = False,
    ) -> None:
        self.tasks = tasks
        self.garbled = garbled
        self.not_json = not_json
        self.not_utf8 = not_utf8
        self.bug = bug
        self.asked: list[int] = []

    @property
    def linked(self) -> bool:
        return True

    async def maintenance_tasks(self, printer_id: int) -> list[MaintenanceTask]:
        self.asked.append(printer_id)
        if self.tasks is None:
            raise ApiError(502, "Bambuddy did not answer")
        if self.garbled:
            # A reply that does not validate, as the client's model_validate raises.
            MaintenanceTask.model_validate({})
        if self.not_json:
            # A 200 whose body is not JSON, as ``response.json()`` raises.
            json.loads("<html>")
        if self.not_utf8:
            # A 200 whose body is not UTF-8, as ``response.json()`` decodes it.
            json.loads(b"\xff\xfe\xfa")
        if self.bug:
            # A bug in the read, not a reply from Bambuddy.
            int("not a number")
        return self.tasks


@pytest.fixture
def settings_store(tmp_path: Path, pg_conninfo: str) -> Iterator[SettingsStore]:
    opened = SettingsStore(
        Settings(
            data_dir=tmp_path, database_url=pg_conninfo, temporal_address=UNUSED_TEMPORAL_ADDRESS
        )
    )
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def printers(pg_pool: PgPool) -> PrinterStore:
    return PrinterStore(pg_pool, None)


@pytest.fixture
def store(pg_pool: PgPool) -> MachineRateStore:
    return MachineRateStore(pg_pool)


def lan_printer(printers: PrinterStore) -> PrinterRow:
    return printers.create(PrinterCreate(serial="0938AB1234567", name="P1S"))


def linked_printer(printers: PrinterStore) -> PrinterRow:
    return printers.ensure_bambuddy(7, serial="31B598000000", name="H2C", model="H2C")


def rates(
    store: MachineRateStore,
    printers: PrinterStore,
    settings: SettingsStore,
    bambuddy: FakeBambuddy | None = None,
) -> MachineRates:
    return MachineRates(
        store, printers, cast(OrderIntegrations, bambuddy or FakeBambuddy([])), settings
    )


def test_inputs_round_trip_and_are_replaced_whole(
    store: MachineRateStore, printers: PrinterStore
) -> None:
    printer = lan_printer(printers)
    assert store.stored(printer.id).profile is None
    store.replace(printer.id, INPUTS)
    stored = store.stored(printer.id)
    assert stored.profile == INPUTS.profile
    assert stored.wear == INPUTS.wear_parts
    assert stored.costs == INPUTS.maintenance
    store.replace(printer.id, MachineRateInputs(profile=CostProfile(watts="300")))
    stored = store.stored(printer.id)
    assert stored.profile == CostProfile(watts="300", actuals_window=3, lan_hours_offset="0")
    assert (stored.wear, stored.costs) == ([], [])
    assert store.delete(printer.id) is True
    assert store.stored(printer.id).profile is None
    assert store.delete(printer.id) is False


def test_the_rows_go_with_their_printer(store: MachineRateStore, printers: PrinterStore) -> None:
    printer = lan_printer(printers)
    store.replace(printer.id, INPUTS)
    printers.delete(printer.id)
    stored = store.stored(printer.id)
    assert (stored.profile, stored.wear, stored.costs) == (None, [], [])


async def test_a_lan_printer_uses_its_typed_intervals(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = lan_printer(printers)
    store.replace(printer.id, INPUTS)
    bambuddy = FakeBambuddy([])
    rate = await rates(store, printers, settings_store, bambuddy).breakdown(printer)
    assert bambuddy.asked == []
    assert rate.intervals == "typed"
    # 0.12 maintenance + 0.07 nozzle + 0.03 power + 0.48 depreciation
    assert rate.rate_per_hour == "0.7"
    assert (rate.printer, rate.printer_name) == (printer.id, "P1S")


async def test_a_linked_printer_reads_bambuddys_intervals_and_keeps_them(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    live = FakeBambuddy(
        [MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours")]
    )
    rate = await rates(store, printers, settings_store, live).breakdown(printer)
    assert live.asked == [7]
    assert rate.intervals == "bambuddy"
    assert rate.maintenance_per_hour == "0.24"
    assert store.stored(printer.id).costs[0].interval_hours == "50"

    # A sent interval for a task Bambuddy gave one is ignored: the read one stays.
    store.replace(printer.id, INPUTS)
    assert store.stored(printer.id).costs[0].interval_hours == "50"

    down = FakeBambuddy(None)
    rate = await rates(store, printers, settings_store, down).breakdown(printer)
    assert rate.intervals == "stored"
    assert rate.intervals_read_at is not None
    assert rate.maintenance_per_hour == "0.24"


async def test_no_profile_is_the_books_rate(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    settings_store.save(SettingsPatch(pricing_machine_rate_per_hour="0.9"))
    printer = lan_printer(printers)
    rate = await rates(store, printers, settings_store).breakdown(printer)
    assert (rate.source, rate.rate_per_hour, rate.printer) == ("price_book", "0.9", printer.id)


async def test_a_quote_is_priced_for_the_printer_it_names(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    lan = lan_printer(printers)
    linked = linked_printer(printers)
    machine = rates(store, printers, settings_store)
    assert machine.resolve(lan.id, None) == lan
    assert machine.resolve(None, 7) == linked
    # A named row gone since falls back to the Bambuddy printer's, as a run does.
    assert machine.resolve("00000000-0000-0000-0000-000000000000", 7) == linked
    assert machine.resolve(None, None) is None
    assert await machine.for_quote(None, None) is None
    settings_store.save(SettingsPatch(default_printer=lan.id))
    assert machine.resolve(None, None) == lan
    settings_store.save(SettingsPatch(default_printer=linked.id))
    assert machine.resolve(None, None) == linked
    # A named row gone, with no Bambuddy printer to fall back to, is no printer: the
    # book's rate, never the default printer's in its place.
    assert machine.resolve("00000000-0000-0000-0000-000000000000", None) is None


async def test_stored_rates_need_no_bambuddy(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    lan = lan_printer(printers)
    linked = linked_printer(printers)
    store.replace(lan.id, INPUTS)
    bambuddy = FakeBambuddy(None)
    assert rates(store, printers, settings_store, bambuddy).stored_rates() == {lan.id: "0.7"}
    assert bambuddy.asked == []
    assert linked.id not in rates(store, printers, settings_store).stored_rates()


def test_the_money_views_fallback_is_the_default_printers_rate(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    settings_store.save(SettingsPatch(pricing_machine_rate_per_hour="0.9"))
    lan = lan_printer(printers)
    store.replace(lan.id, INPUTS)
    machine = rates(store, printers, settings_store, FakeBambuddy(None))
    by_serial = {lan.serial: "0.7"}
    assert machine.money_rates() == ("0.9", {lan.id: "0.7"}, by_serial)
    settings_store.save(SettingsPatch(default_printer=lan.id))
    assert machine.money_rates() == ("0.7", {lan.id: "0.7"}, by_serial)


def test_a_printer_with_no_profile_is_the_books_rate_in_the_money_view(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    """Spec §6.2: a registry printer with no profile is the book's rate, never the
    default printer's; only an attempt naming no known printer takes the default's."""
    settings_store.save(SettingsPatch(pricing_machine_rate_per_hour="0.9"))
    lan = lan_printer(printers)
    bare = linked_printer(printers)
    store.replace(lan.id, INPUTS)
    settings_store.save(SettingsPatch(default_printer=lan.id))
    fallback, by_id, by_serial = rates(store, printers, settings_store).money_rates()
    assert fallback == "0.7"
    assert by_id == {lan.id: "0.7", bare.id: "0.9"}
    assert by_serial == {lan.serial: "0.7", bare.serial: "0.9"}


async def test_a_reply_that_does_not_validate_falls_back_to_the_stored_intervals(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    live = FakeBambuddy(
        [MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours")]
    )
    await rates(store, printers, settings_store, live).breakdown(printer)
    garbled = FakeBambuddy([], garbled=True)
    rate = await rates(store, printers, settings_store, garbled).breakdown(printer)
    assert garbled.asked == [7]
    assert rate.intervals == "stored"
    assert rate.maintenance_per_hour == "0.24"


async def test_a_reply_that_is_not_json_falls_back_to_the_stored_intervals(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    live = FakeBambuddy(
        [MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours")]
    )
    await rates(store, printers, settings_store, live).breakdown(printer)
    broken = FakeBambuddy([], not_json=True)
    rate = await rates(store, printers, settings_store, broken).breakdown(printer)
    assert broken.asked == [7]
    assert rate.intervals == "stored"
    assert rate.maintenance_per_hour == "0.24"


async def test_a_reply_that_is_not_utf8_falls_back_to_the_stored_intervals(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    live = FakeBambuddy(
        [MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours")]
    )
    await rates(store, printers, settings_store, live).breakdown(printer)
    broken = FakeBambuddy([], not_utf8=True)
    rate = await rates(store, printers, settings_store, broken).breakdown(printer)
    assert broken.asked == [7]
    assert rate.intervals == "stored"
    assert rate.maintenance_per_hour == "0.24"


async def test_an_unlinked_printers_typed_intervals_count(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    live = FakeBambuddy(
        [MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours")]
    )
    await rates(store, printers, settings_store, live).breakdown(printer)
    assert store.stored(printer.id).costs[0].interval_hours == "50"
    unlinked = printers.unlink_bambuddy(printer.id)
    store.replace(printer.id, INPUTS)
    assert store.stored(printer.id).costs[0].interval_hours == "100"
    rate = await rates(store, printers, settings_store, live).breakdown(unlinked)
    assert rate.intervals == "typed"
    assert rate.maintenance_per_hour == "0.12"


async def test_a_relinked_printer_counts_no_interval_until_it_is_read_again(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    live = FakeBambuddy(
        [MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours")]
    )
    await rates(store, printers, settings_store, live).breakdown(printer)
    printers.unlink_bambuddy(printer.id)
    relinked = printers.link_bambuddy(printer.id, 8)
    # The old link's interval is not taken for one read from the new link.
    rate = await rates(store, printers, settings_store, live).breakdown(relinked, live=False)
    assert rate.intervals == "stored"
    assert rate.maintenance_per_hour == "0"
    assert [part.why for part in rate.maintenance] == ["not_read"]


async def test_a_read_that_ends_after_an_unlink_keeps_nothing(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    printers.unlink_bambuddy(printer.id)
    # A live read of Bambuddy printer 7 that began before the unlink lands after it.
    store.remember_intervals(
        printer.id,
        7,
        [MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours")],
    )
    relinked = printers.link_bambuddy(printer.id, 7)
    rate = await rates(store, printers, settings_store).breakdown(relinked, live=False)
    assert [part.why for part in rate.maintenance] == ["not_read"]


async def test_a_printer_the_sync_links_to_another_bambuddy_printer_counts_no_old_interval(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    live = FakeBambuddy(
        [MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours")]
    )
    await rates(store, printers, settings_store, live).breakdown(printer)
    read = await rates(store, printers, settings_store).breakdown(printer, live=False)
    assert [part.why for part in read.maintenance] == [None]
    # Bambuddy printer 7 is gone from the strip; its serial now reports as printer 8.
    relinked = printers.ensure_bambuddy(
        8, serial=printer.serial, name="H2C", model="H2C", listed={8}
    )
    assert relinked.id == printer.id and relinked.bambuddy_printer_id == 8
    rate = await rates(store, printers, settings_store, live).breakdown(relinked, live=False)
    assert [part.why for part in rate.maintenance] == ["not_read"]


async def test_intervals_read_on_the_synthetic_row_survive_its_merge_into_a_relinked_printer(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    # Bambuddy printer 8 is listed before its serial is known: a synthetic row, whose
    # intervals are read from printer 8.
    synthetic = printers.ensure_bambuddy(8, serial=None, name="H2C", model="H2C")
    assert synthetic.id != printer.id
    store.replace(synthetic.id, INPUTS)
    live = FakeBambuddy(
        [MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours")]
    )
    await rates(store, printers, settings_store, live).breakdown(synthetic)
    # Printer 8 reports the serial of printer 7's row: the synthetic row merges into it.
    relinked = printers.ensure_bambuddy(
        8, serial=printer.serial, name="H2C", model="H2C", listed={8}
    )
    assert relinked.id == printer.id and printers.get(synthetic.id) is None
    rate = await rates(store, printers, settings_store).breakdown(relinked, live=False)
    assert rate.intervals == "stored"
    assert [part.why for part in rate.maintenance] == [None]
    assert rate.maintenance_per_hour != "0"


def test_an_unlink_of_a_printer_never_linked_leaves_its_rows_alone(
    store: MachineRateStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    printer = lan_printer(printers)
    store.replace(printer.id, INPUTS)
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE printer_maintenance_costs SET interval_read_at = now() WHERE printer = %s",
            (uuid.UUID(printer.id),),
        )
    printers.unlink_bambuddy(printer.id)
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT interval_read_at FROM printer_maintenance_costs WHERE printer = %s",
            (uuid.UUID(printer.id),),
        ).fetchone()
    assert row is not None and row["interval_read_at"] is not None


async def test_bambuddys_intervals_are_kept_to_a_tenth_of_an_hour(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    store.replace(
        printer.id,
        MachineRateInputs(
            profile=CostProfile(labour_rate_per_hour="30"),
            maintenance=[
                MaintenanceCost(task="Rods", minutes="0", consumables="1"),
                MaintenanceCost(task="Tiny", minutes="0", consumables="1"),
            ],
        ),
    )
    live = FakeBambuddy(
        [
            MaintenanceTask(task="Rods", interval_hours=0.25),
            MaintenanceTask(task="Tiny", interval_hours=0.04),
        ]
    )
    machine = rates(store, printers, settings_store, live)
    rate = await machine.breakdown(printer)
    stored = {cost.task: cost.interval_hours for cost in store.stored(printer.id).costs}
    # 0.25 h is kept as 0.3 h, and one that rounds to nothing is not counted.
    assert stored == {"Rods": "0.3", "Tiny": None}
    assert (
        rate.maintenance_per_hour
        == (await machine.breakdown(printer, live=False)).maintenance_per_hour
    )


async def test_a_bug_in_the_read_is_not_taken_for_bambuddy_not_answering(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    with pytest.raises(ValueError, match="invalid literal"):
        await rates(store, printers, settings_store, FakeBambuddy([], bug=True)).breakdown(printer)


async def test_the_views_inputs_are_from_the_same_read_as_its_breakdown(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    live = FakeBambuddy(
        [MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours")]
    )
    view = await rates(store, printers, settings_store, live).view(printer.id)
    assert view is not None and view.inputs is not None
    assert view.breakdown.intervals == "bambuddy"
    assert view.inputs.maintenance[0].interval_hours == "50"
    assert view.bambuddy_tasks == ["Lubricate rods"]
    assert live.asked == [7]  # one read serves the breakdown and the names


async def test_a_profile_deleted_during_the_view_takes_bambuddys_names_from_the_one_read(
    store: MachineRateStore,
    printers: PrinterStore,
    settings_store: SettingsStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    real = store.stored

    def stored_then_deleted(key: str) -> Stored:
        # The breakdown's read sees the profile; a delete lands before the view's own.
        seen = real(key)
        store.delete(key)
        return seen

    monkeypatch.setattr(store, "stored", stored_then_deleted)
    live = FakeBambuddy(
        [MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours")]
    )
    view = await rates(store, printers, settings_store, live).view(printer.id)
    assert view is not None and view.inputs is None
    assert view.breakdown.intervals == "bambuddy"
    assert view.bambuddy_tasks == ["Lubricate rods"]
    assert live.asked == [7]


async def test_the_view_of_a_linked_printer_with_no_profile_names_bambuddys_tasks(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    # The first person to type its tasks gets Bambuddy's names; the rate is the book's.
    printer = linked_printer(printers)
    live = FakeBambuddy(
        [
            MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours"),
            MaintenanceTask(task="Check belts", interval_hours=30, interval_type="days"),
        ]
    )
    view = await rates(store, printers, settings_store, live).view(printer.id)
    assert view is not None and view.inputs is None
    assert view.breakdown.source == "price_book"
    assert view.bambuddy_tasks == ["Lubricate rods", "Check belts"]
    assert live.asked == [7]
    silent = await rates(store, printers, settings_store, FakeBambuddy(None)).view(printer.id)
    assert silent is not None and silent.bambuddy_tasks == []
    lan = await rates(store, printers, settings_store, live).view(lan_printer(printers).id)
    assert lan is not None and lan.bambuddy_tasks == []


def test_keeping_intervals_waits_for_a_replace_of_the_same_printer(
    store: MachineRateStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    done = threading.Event()

    def remember() -> None:
        store.remember_intervals(
            printer.id,
            7,
            [MaintenanceTask(task="Lubricate rods", interval_hours=50, interval_type="hours")],
        )
        done.set()

    with pg_pool.connection() as conn, conn.transaction():
        # The lock a replace holds on the profile row while it rewrites the tasks.
        conn.execute(
            "SELECT 1 FROM printer_cost_profiles WHERE printer = %s FOR UPDATE",
            (uuid.UUID(printer.id),),
        )
        thread = threading.Thread(target=remember)
        thread.start()
        assert not done.wait(1.0)
    thread.join(10)
    assert done.is_set()
    assert store.stored(printer.id).costs[0].interval_hours == "50"


LINKED_INPUTS = MachineRateInputs(
    profile=CostProfile(labour_rate_per_hour="30"),
    maintenance=[
        MaintenanceCost(task="Rods", minutes="20", consumables="2", interval_hours="100"),
        MaintenanceCost(task="Belts", minutes="10", consumables="1", interval_hours="100"),
        MaintenanceCost(task="Filter", minutes="5", consumables="9", interval_hours="100"),
        MaintenanceCost(task="Typed", minutes="5", consumables="9", interval_hours="100"),
        MaintenanceCost(task="Later", minutes="5", consumables="9", interval_hours="100"),
    ],
)


def _tasks(belts_enabled: bool) -> list[MaintenanceTask]:
    return [
        MaintenanceTask(task="Rods", interval_hours=50),
        MaintenanceTask(task="Belts", interval_hours=200, enabled=belts_enabled),
        MaintenanceTask(task="Filter", interval_hours=30, interval_type="days"),
        MaintenanceTask(task="Later", interval_hours=0),
    ]


async def test_a_linked_printers_stored_rate_is_its_live_rate(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    """The stored path (Bambuddy down, the money view, the quote's check) counts what
    the live read counts: a task never read, a disabled, calendar or gone one is not
    counted, and a task Bambuddy disables or drops later stops counting."""
    printer = linked_printer(printers)
    store.replace(printer.id, LINKED_INPUTS)
    before = await rates(store, printers, settings_store, FakeBambuddy(None)).breakdown(printer)
    assert before.maintenance_per_hour == "0"
    assert {p.task: p.why for p in before.maintenance} == dict.fromkeys(
        ["Rods", "Belts", "Filter", "Typed", "Later"], "not_read"
    )

    for belts in (True, False):
        live = await rates(store, printers, settings_store, FakeBambuddy(_tasks(belts))).breakdown(
            printer
        )
        machine = rates(store, printers, settings_store, FakeBambuddy(None))
        down = await machine.breakdown(printer)
        assert down.intervals == "stored"
        assert down.rate_per_hour == live.rate_per_hour
        assert machine.stored_rates()[printer.id] == live.rate_per_hour
        assert machine.check_rate(printer.id, None) == live.rate_per_hour
        counted = {p.task for p in down.maintenance if p.counted}
        assert counted == ({"Rods", "Belts"} if belts else {"Rods"})
    # Rods (20 min x 30 / 60 + 2) / 50 = 0.24; Belts was (5 + 1) / 200 = 0.03 until disabled.
    assert live.maintenance_per_hour == "0.24"


def test_the_check_reads_the_quotes_printers_stored_rate(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    """The quote's check prices nothing only when the printer's rate is 0 too, read with
    no Bambuddy call."""
    lan = lan_printer(printers)
    bambuddy = FakeBambuddy(None)
    machine = rates(store, printers, settings_store, bambuddy)
    assert machine.check_rate(lan.id, None) is None
    assert machine.check_rate(None, None) is None
    store.replace(lan.id, INPUTS)
    assert machine.check_rate(lan.id, None) == "0.7"
    assert bambuddy.asked == []


def _merge(printers: PrinterStore, store: MachineRateStore, kept: MachineRateInputs | None) -> str:
    """A synthetic Bambuddy row with inputs merged into the row holding the serial, which
    has ``kept``; the kept row's id."""
    gone = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    store.replace(gone.id, INPUTS)
    mine = printers.create(PrinterCreate(serial="01P00A000000001", name="Mine"))
    if kept is not None:
        store.replace(mine.id, kept)
    merged = printers.ensure_bambuddy(4, serial="01P00A000000001", name="H2C", model="H2C")
    assert merged.id == mine.id and printers.get(gone.id) is None
    return mine.id


def test_a_merge_keeps_the_kept_printers_inputs_whole(
    store: MachineRateStore, printers: PrinterStore
) -> None:
    """The kept printer has a profile and no parts or tasks: the gone printer's parts and
    tasks are dropped with its profile, never mixed in."""
    own = MachineRateInputs(
        profile=CostProfile(watts="300", actuals_window=3, lan_hours_offset="0")
    )
    kept = _merge(printers, store, own)
    stored = store.stored(kept)
    assert (stored.profile, stored.wear, stored.costs) == (own.profile, [], [])


def test_a_merge_moves_every_input_to_a_printer_with_none(
    store: MachineRateStore, printers: PrinterStore
) -> None:
    kept = _merge(printers, store, None)
    stored = store.stored(kept)
    assert (stored.profile, stored.wear, stored.costs) == (
        INPUTS.profile,
        INPUTS.wear_parts,
        INPUTS.maintenance,
    )


# -- #5774: actuals (spec 2026-11-17-machine-rate-actuals §4, §5) --------------------------


def part_event(cost: str = "35", **fields: object) -> MachineRateEventCreate:
    body = {"kind": "part", "name": "Nozzle", "cost": cost, **fields}
    return MachineRateEventCreate.model_validate(body)


def log_part(
    store: MachineRateStore, printer: str, hours: str, cost: str = "35", **fields: object
) -> MachineRateEvent:
    event, _ = store.add_event(
        printer,
        part_event(cost, **fields),
        hours_at=hours,
        hours_source="lan_watch",
        created_by="browser",
    )
    return event


def test_the_window_offset_and_priced_by_round_trip(
    store: MachineRateStore, printers: PrinterStore
) -> None:
    printer = lan_printer(printers)
    inputs = MachineRateInputs(
        profile=CostProfile(actuals_window=5, lan_hours_offset="12.5"),
        wear_parts=[WearPart(name="Nozzle", cost="35", life_hours="1000", priced_by="actual")],
        maintenance=[MaintenanceCost(task="Lubricate rods", priced_by="actual")],
    )
    store.replace(printer.id, inputs)
    stored = store.stored(printer.id)
    assert stored.profile == inputs.profile
    assert stored.wear == inputs.wear_parts
    assert stored.costs == inputs.maintenance


def test_rows_from_before_read_the_new_defaults(
    store: MachineRateStore, printers: PrinterStore
) -> None:
    """#4625's rows read with the new columns' defaults: window 3, offset 0, estimate."""
    printer = lan_printer(printers)
    store.replace(printer.id, INPUTS)
    stored = store.stored(printer.id)
    assert stored.profile is not None
    assert (stored.profile.actuals_window, stored.profile.lan_hours_offset) == (3, "0")
    assert [p.priced_by for p in stored.wear] + [c.priced_by for c in stored.costs] == [
        "estimate",
        "estimate",
    ]


def test_a_retry_of_a_source_writes_once(store: MachineRateStore, printers: PrinterStore) -> None:
    printer = lan_printer(printers)
    store.replace(printer.id, INPUTS)
    body = part_event(source="form:1")
    first, wrote = store.add_event(
        printer.id, body, hours_at="10", hours_source="lan_watch", created_by="browser"
    )
    again, wrote_again = store.add_event(
        printer.id, body, hours_at="99", hours_source="lan_watch", created_by="browser"
    )
    assert (wrote, wrote_again) == (True, False)
    assert again == first
    assert first.hours_at == "10"
    assert len(store.stored(printer.id).logged) == 1


def test_logged_replacements_price_a_part_set_to_actual(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = lan_printer(printers)
    actual = INPUTS.model_copy(
        update={
            "profile": INPUTS.profile.model_copy(update={"actuals_window": 1}),
            "wear_parts": [
                WearPart(name="Nozzle", cost="14", life_hours="200", priced_by="actual")
            ],
        }
    )
    store.replace(printer.id, actual)
    machine = rates(store, printers, settings_store)
    assert machine.stored_rates()[printer.id] == "0.7"
    log_part(store, printer.id, "0", "20")
    log_part(store, printer.id, "400", "20")
    # Nozzle 20 / 400 = 0.05 in place of 14 / 200 = 0.07.
    assert machine.stored_rates()[printer.id] == "0.68"


async def test_a_rename_moves_the_events_and_a_deleted_row_leaves_them_orphaned(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = lan_printer(printers)
    store.replace(printer.id, INPUTS)
    log_part(store, printer.id, "0")
    log_part(store, printer.id, "300")
    renamed = INPUTS.model_copy(
        update={
            "wear_parts": [
                WearPart(name="Hotend", cost="14", life_hours="200", renamed_from="Nozzle")
            ]
        }
    )
    store.replace(printer.id, renamed)
    assert {e.name for e in store.stored(printer.id).logged} == {"Hotend"}
    view = await rates(store, printers, settings_store).view(printer.id)
    assert view is not None
    hotend = next(r for r in view.actuals.rows if r.name == "Hotend")
    assert (hotend.have, [e.hours_at for e in hotend.events]) == (1, ["300", "0"])
    assert view.actuals.orphaned == []

    store.replace(printer.id, INPUTS.model_copy(update={"wear_parts": []}))
    view = await rates(store, printers, settings_store).view(printer.id)
    assert view is not None
    assert [(o.kind, o.name, o.events) for o in view.actuals.orphaned] == [("part", "Hotend", 2)]


def test_a_merge_moves_events_and_skips_the_kept_printers_own(
    store: MachineRateStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    gone = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    mine = printers.create(PrinterCreate(serial="01P00A000000001", name="Mine"))
    for printer in (gone, mine):
        store.replace(printer.id, INPUTS)
    log_part(store, gone.id, "10", source="form:shared")
    log_part(store, gone.id, "20", source="form:gone")
    log_part(store, mine.id, "30", source="form:shared")
    with pg_pool.connection() as conn:
        for owner, history in ((gone.id, 7), (gone.id, 8), (mine.id, 7)):
            conn.execute(
                "INSERT INTO printer_maintenance_events (id, printer, task, performed_at,"
                " hours_at, hours_source, origin, bambuddy_history_id, source, created_by)"
                " VALUES (%s, %s, 'Lubricate rods', now(), 1, 'bambuddy', 'bambuddy', %s,"
                " %s, 'import')",
                (uuid.uuid4(), owner, history, f"bambuddy:{history}:{owner}"),
            )
    merged = printers.ensure_bambuddy(4, serial="01P00A000000001", name="H2C", model="H2C")
    assert merged.id == mine.id
    logged = store.stored(mine.id).logged
    assert sorted(e.source for e in logged if e.kind == "part") == ["form:gone", "form:shared"]
    assert next(e for e in logged if e.source == "form:shared").hours_at == "30"
    assert sorted(e.bambuddy_history_id or 0 for e in logged if e.kind == "task") == [7, 8]


def test_events_are_patched_deleted_and_paged(
    store: MachineRateStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    printer = lan_printer(printers)
    store.replace(printer.id, INPUTS)
    events = [
        log_part(store, printer.id, str(h * 100), at=f"2026-11-0{h + 1}T00:00:00Z")
        for h in range(3)
    ]
    page = store.list_events(printer.id, limit=2)
    assert [e.id for e in page.events] == [events[2].id, events[1].id]
    assert page.next is not None
    rest = store.list_events(printer.id, cursor=page.next, limit=2)
    assert ([e.id for e in rest.events], rest.next) == ([events[0].id], None)

    patched = store.patch_event(
        printer.id,
        events[0].id,
        MachineRateEventPatch(cost="40", hours_used="640", notes="fitted before"),
        counter="lan_watch",
    )
    assert patched is not None
    assert (patched.cost, patched.hours_used, patched.notes) == ("40", "640", "fitted before")
    with pytest.raises(EventFieldError):
        store.patch_event(
            printer.id, events[0].id, MachineRateEventPatch(minutes="5"), counter="lan_watch"
        )
    cleared = store.patch_event(
        printer.id, events[0].id, MachineRateEventPatch(hours_at=None), counter="lan_watch"
    )
    assert cleared is not None and (cleared.hours_at, cleared.hours_source) == (None, None)
    assert store.delete_event(printer.id, events[0].id) is True
    assert store.delete_event(printer.id, events[0].id) is False

    imported = uuid.uuid4()
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO printer_maintenance_events (id, printer, task, performed_at, origin,"
            " bambuddy_history_id, source, created_by) VALUES (%s, %s, 'Lubricate rods',"
            " now(), 'bambuddy', 3, 'bambuddy:3', 'import')",
            (imported, printer.id),
        )
    with pytest.raises(ImportedEventError):
        store.delete_event(printer.id, str(imported))


def test_scadbuddys_counter_adds_the_offset_and_rounds_half_up(
    store: MachineRateStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    printer = lan_printer(printers)
    assert store.counter(printer.id) is None
    with pg_pool.connection() as conn:
        # 4.25 h run (rounds half up to 4.3), 0.04 h unseen (rounds to 0).
        conn.execute(
            "INSERT INTO printer_run_hours (printer, run_seconds, unseen_seconds, counted_to)"
            " VALUES (%s, 15300, 144, now())",
            (printer.id,),
        )
    count = store.counter(printer.id)
    assert count is not None
    assert (count.hours, count.run_hours, count.offset_hours, count.unseen_hours) == (
        "4.3",
        "4.3",
        "0",
        "0",
    )
    store.replace(printer.id, MachineRateInputs(profile=CostProfile(lan_hours_offset="100.5")))
    count = store.counter(printer.id)
    assert count is not None and (count.hours, count.offset_hours) == ("104.8", "100.5")
