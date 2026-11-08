"""The print worker's printer row through its CRUD route (#5298 Task 29's
`POST /printers/ensure-bambuddy`, taken early by #4625): a run's first check makes or
merges a Bambuddy printer's row on the API, so a merge rewrites every table
`PRINTER_REFERENCES` names (the machine rate's among them) with the API's privileges,
never the worker's. The worker's store sends the call and runs no SQL for it."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import pytest

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudRefusedError
from scadbuddy.crud.print import RemotePrinterStore
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.orders.machine_rate import CostProfile, MachineRateInputs, MachineRateStore
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import threaded_print_crud

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000001"


class _NoPool:
    """The worker's pool in these tests: any SQL is a failure."""

    def connection(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("the worker ran SQL for a printer row")


@pytest.fixture
def store(tmp_path: Path, pg_conninfo: str) -> Iterator[SettingsStore]:
    opened = SettingsStore(
        Settings(
            data_dir=tmp_path,
            database_url=pg_conninfo,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            auth_mode="enforce",
        )
    )
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def printers(store: SettingsStore) -> PrinterStore:
    """The API's registry."""
    return PrinterStore(store.pool, None)


def test_the_area_is_served_and_awaited() -> None:
    assert "printer-ensure" in CRUD_AREAS["print"]
    assert "printer-ensure" in PRINT_CRUD_AREAS


def test_the_workers_merge_runs_on_the_api(store: SettingsStore, printers: PrinterStore) -> None:
    """A synthetic row with a machine rate merges into the row holding the serial: the
    inputs move with it, and the worker's store ran no SQL."""
    rates = MachineRateStore(store.pool)
    gone = printers.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    inputs = MachineRateInputs(profile=CostProfile(watts="300"))
    rates.replace(gone.id, inputs)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    with threaded_print_crud(store, printers=printers) as client:
        worker = RemotePrinterStore(client, _NoPool(), None)
        row, written = worker.ensure_bambuddy_written(
            4, serial=SERIAL, name="H2C", model="H2C", listed={4}
        )
        assert row.id == mine.id and row.bambuddy_printer_id == 4 and written
        assert printers.get(gone.id) is None
        assert rates.stored(mine.id).profile == inputs.profile
        # Sent again, it finds the row and writes nothing.
        again = worker.ensure_bambuddy(4, serial=SERIAL, name="H2C", model="H2C")
        assert again.id == mine.id


def test_the_route_is_the_print_callers_alone(store: SettingsStore, printers: PrinterStore) -> None:
    with threaded_print_crud(store, caller="render", printers=printers) as client:
        worker = RemotePrinterStore(client, _NoPool(), None)
        with pytest.raises(CrudRefusedError) as refused:
            worker.ensure_bambuddy(4, serial=None, name="H2C", model="H2C")
    assert refused.value.status == 403
    assert refused.value.problem.get("reason") == "wrong_caller"


def test_the_worker_store_overrides_every_method_that_merges_rows() -> None:
    """A row merge moves another service's rows (#4625): a worker reaching one through the
    SQL store fails on the role. Each PrinterStore method that merges is the API's, so the
    worker's store overrides it."""
    merging = {
        name
        for name, method in inspect.getmembers(PrinterStore, inspect.isfunction)
        if not name.startswith("_")
        and (
            "self._merge(" in inspect.getsource(method)
            or "self._ensure_locked(" in inspect.getsource(method)
        )
    }
    assert merging >= {"ensure_bambuddy_written", "link_bambuddy"}
    assert merging <= set(vars(RemotePrinterStore))
