"""A printer's machine rate inputs and the rate read from them (#4625, order operations
spec 2026-11-07 §6.1, §6.2): the three tables, Bambuddy's intervals kept for when it does
not answer, and the printer a quote is priced for."""

from __future__ import annotations

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
    MachineRateInputs,
    MachineRates,
    MachineRateStore,
    MaintenanceCost,
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
    ),
    wear_parts=[WearPart(name="Nozzle", cost="14", life_hours="200")],
    maintenance=[
        MaintenanceCost(task="Lubricate rods", minutes="20", consumables="2", interval_hours="100")
    ],
)


class FakeBambuddy:
    def __init__(self, tasks: list[MaintenanceTask] | None, *, garbled: bool = False) -> None:
        self.tasks = tasks
        self.garbled = garbled
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
    assert stored.profile == CostProfile(watts="300")
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
    settings_store.save(SettingsPatch(default_printer=None, printer_id=7))
    assert machine.resolve(None, None) == linked


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
    assert machine.money_rates() == ("0.9", {lan.id: "0.7"})
    settings_store.save(SettingsPatch(default_printer=lan.id))
    assert machine.money_rates() == ("0.7", {lan.id: "0.7"})


async def test_a_reply_that_does_not_validate_falls_back_to_the_stored_intervals(
    store: MachineRateStore, printers: PrinterStore, settings_store: SettingsStore
) -> None:
    printer = linked_printer(printers)
    store.replace(printer.id, INPUTS)
    garbled = FakeBambuddy([], garbled=True)
    rate = await rates(store, printers, settings_store, garbled).breakdown(printer)
    assert garbled.asked == [7]
    assert rate.intervals == "stored"
    assert rate.maintenance_per_hour == "0.12"


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
    own = MachineRateInputs(profile=CostProfile(watts="300"))
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
