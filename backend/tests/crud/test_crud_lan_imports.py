"""A LAN-only printer's print record (#4699 phase 4) through the print worker's CRUD route
(`POST /lan-imports`): the API matches a model and writes the record and its activity
row; the worker runs no SQL. The record is natural on the job (the printer, its
``subtask_name`` and start)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudRefusedError
from scadbuddy.crud.print import RemoteLanImports
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.print_watch.lan import NO_MATCH, LanCapture, decide
from scadbuddy.print_watch.lan_store import LanImportStore
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from scadbuddy.worker import PRINT_CRUD_AREAS
from scadbuddy.workflows.lan_auto_import import LanJob
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import served_print_crud, threaded_print_crud

pytestmark = pytest.mark.requires_postgres

STARTED = datetime(2026, 11, 11, 12, 0, 0, 123456, tzinfo=UTC)
CATALOGUE = SimpleNamespace(slugs=lambda: ["demo-plate", "demo"], exists=lambda slug: True)


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
def printer(store: SettingsStore) -> str:
    return (
        PrinterStore(store.pool, None).create(PrinterCreate(serial="01P00A000000001", name="P1")).id
    )


def kept(printer_id: str, subtask: str = "demo_plate") -> LanCapture:
    return LanCapture(
        job=LanJob(printer_id=printer_id, subtask_name=subtask, started_at=STARTED),
        source="printer",
        library_file_id=41,
        printer_path=f"/cache/{subtask}.gcode.3mf",
        filename=f"{subtask}.gcode.3mf",
        size=3,
        sha256="ab" * 32,
    )


def test_the_area_is_served_and_awaited() -> None:
    assert "lan-imports" in CRUD_AREAS["print"]
    assert "lan-imports" in PRINT_CRUD_AREAS


async def test_the_api_matches_and_records_the_workers_capture_once(
    store: SettingsStore, printer: str
) -> None:
    async with served_print_crud(store, catalogue=CATALOGUE) as crud:
        remote = RemoteLanImports(crud)
        first = await remote.record(kept(printer))
        assert (first.state, first.slug, first.matched_by) == ("linked", "demo-plate", "job_name")
        assert first.id is not None and first.printer_id == printer
        assert first.started_at == STARTED
        # Sent again (a retried attempt, a lost answer): the first record answers.
        again = await remote.record(kept(printer))
        assert again == first
        inbox = await remote.record(kept(printer, "benchy"))
        assert (inbox.state, inbox.reason) == ("inbox", NO_MATCH)
    with store.pool.connection() as conn:
        found = conn.execute("SELECT count(*) AS n FROM print_lan_imports").fetchone()
    assert found is not None and found["n"] == 2


def test_the_route_is_the_print_callers_alone(store: SettingsStore, printer: str) -> None:
    with (
        threaded_print_crud(store, caller="render") as client,
        pytest.raises(CrudRefusedError) as refused,
    ):
        client.request(
            "POST",
            "/api/v1/internal/crud/print/lan-imports",
            json=kept(printer).model_dump(mode="json"),
        )
    assert refused.value.status == 403


async def test_a_record_and_its_activity_row_are_one_write(
    store: SettingsStore, printer: str
) -> None:
    index = ActivityIndex(store.pool, events=None)
    lan = LanImportStore(store.pool, index)
    row = await lan.record(decide(kept(printer), slugs=["demo-plate"], earlier=[]))
    activity = await index.get(f"print_auto_import:lan:{row.id}")
    assert activity is not None
    assert activity.subject == "model:demo-plate"
    assert activity.facets == {"state": "linked", "source": "printer", "matched_by": "job_name"}
    assert activity.detail["printer"] == printer
    # A linked record names its file's hash: a later job of the same file links its model.
    assert await lan.slugs_by_hash("ab" * 32) == {"demo-plate"}


async def test_a_printer_deleted_since_the_start_leaves_a_record_naming_none(
    store: SettingsStore, printer: str
) -> None:
    PrinterStore(store.pool, None).delete(printer)
    lan = LanImportStore(store.pool)
    row = await lan.record(decide(kept(printer), slugs=[], earlier=[]))
    assert row.id is not None and row.printer_id is None
