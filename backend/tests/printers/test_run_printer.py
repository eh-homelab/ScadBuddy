"""The run names a registry printer and chooses its path (#3823, plan 2026-10-16 Task 8):
`prepare_run` resolves the row (named, else the one for the Bambuddy printer, made when
there is none), refuses fields that disagree, and on a native path reads the printer's
status from its LAN report, never from Bambuddy (hot-path plan D4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.print_run import PreparedRun, PrintRunRequest, prepare_run
from scadbuddy.bambuddy.print_source import PrintSource
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.models import LanLink, PrinterPatch
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from tests.bambuddy.conftest import recording
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

BASE = "http://bambuddy.test"
API = f"{BASE}/api/v1"
SERIAL = "01P00A0000000AA"
CERT = "cd" * 32
REPORT = json.loads((Path(__file__).parent / "fixtures" / "lan-report-h2c.json").read_text())


class OnePlate:
    """The little of a `PrintSource` that `prepare_run` reads for an unsliced file."""

    is_sliced = False

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        return [1]


def bambuddy_routes(*, serial: str | None = SERIAL) -> respx.Route:
    """Bambuddy's printer 1 (an H2C with ``serial``), its presets and its status; the
    status route, to say whether it was read."""
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(
            200, json=[{**recording("printers.json")[0], "serial_number": serial}]
        )
    )
    respx.get(f"{API}/slicer/presets").mock(
        return_value=httpx.Response(200, json=recording("slicer-presets-h2c.json"))
    )
    respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=recording("local-presets.json"))
    )
    return respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=recording("printer-status-rack.json"))
    )


def run_request(**fields: Any) -> PrintRunRequest:
    return PrintRunRequest.model_validate(
        {
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
            "choices": {"nozzles": [{"size": "0.2"}], "tier": "fine"},
            **fields,
        }
    )


@pytest.fixture
def reports(pg_pool: PgPool) -> PrinterReportStore:
    return PrinterReportStore(pg_pool)


async def prepare(
    printers: PrinterStore,
    reports: PrinterReportStore,
    request: PrintRunRequest,
    settings: StoredSettings | None = None,
) -> PreparedRun:
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        return await prepare_run(
            client,
            cast(PrintSource, OnePlate()),
            settings or StoredSettings(),
            request,
            printers=printers,
            reports=reports,
        )


def lan_linked(printers: PrinterStore, reports: PrinterReportStore, **patch: Any) -> str:
    """A row for Bambuddy printer 1, linked over the LAN, that reported just now."""
    row = printers.ensure_bambuddy(1, serial=SERIAL, name="Garage", model="H2C")
    printers.link_lan(row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256=CERT)
    printers.patch(row.id, PrinterPatch.model_validate(patch or {"preferred_path": "auto"}))
    reports.put(row.id, REPORT["print"], datetime.now(UTC))
    return row.id


@respx.mock
async def test_run_refuses_disagreeing_printer_fields(
    printers: PrinterStore, reports: PrinterReportStore
) -> None:
    bambuddy_routes()
    row = printers.ensure_bambuddy(1, serial=SERIAL, name="Garage", model="H2C")

    with pytest.raises(ApiError) as refused:
        await prepare(printers, reports, run_request(printer=row.id, printer_id=2))

    assert refused.value.status == 422
    assert "disagreeing printer" in refused.value.detail


@respx.mock
async def test_an_unknown_registry_printer_is_refused(
    printers: PrinterStore, reports: PrinterReportStore
) -> None:
    bambuddy_routes()

    with pytest.raises(ApiError) as refused:
        await prepare(
            printers, reports, run_request(printer="00000000-0000-4000-8000-000000000009")
        )

    assert refused.value.status == 422
    assert "no printer" in refused.value.detail


@respx.mock
async def test_run_with_bambuddy_id_creates_registry_row(
    printers: PrinterStore, reports: PrinterReportStore
) -> None:
    status = bambuddy_routes()

    prepared = await prepare(printers, reports, run_request(printer_id=1))

    row = printers.by_bambuddy_id(1)
    assert row is not None and row.serial == SERIAL
    assert (prepared.printer, prepared.path, prepared.printer_id) == (row.id, "bambuddy", 1)
    assert status.called


@respx.mock
async def test_the_configured_printer_gets_its_row_too(
    printers: PrinterStore, reports: PrinterReportStore
) -> None:
    bambuddy_routes()

    prepared = await prepare(
        printers, reports, run_request(), StoredSettings.model_validate({"printer_id": 1})
    )

    row = printers.by_bambuddy_id(1)
    assert row is not None and prepared.printer == row.id


@respx.mock
async def test_a_named_printer_prints_on_its_bambuddy_printer(
    printers: PrinterStore, reports: PrinterReportStore
) -> None:
    bambuddy_routes()
    row = printers.ensure_bambuddy(1, serial=SERIAL, name="Garage", model="H2C")

    prepared = await prepare(printers, reports, run_request(printer=row.id))

    assert (prepared.printer, prepared.printer_id, prepared.path) == (row.id, 1, "bambuddy")


@respx.mock
async def test_auto_path_picks_native_when_report_fresh(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    """D4: on a native run the status is the LAN report's; Bambuddy's is never read."""
    status = bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports)

    prepared = await prepare(printers_with_kek, reports, run_request(printer=pid))

    assert (prepared.printer, prepared.path, prepared.printer_id) == (pid, "native", 1)
    assert not status.called
    assert prepared.printer_status is not None
    assert prepared.printer_status.name == "Garage"
    assert prepared.printer_status.state == "RUNNING"


@respx.mock
async def test_auto_path_is_bambuddy_once_the_report_is_stale(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    status = bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports)
    reports.put(pid, REPORT["print"], datetime.now(UTC) - timedelta(minutes=5))

    prepared = await prepare(printers_with_kek, reports, run_request(printer=pid))

    assert prepared.path == "bambuddy" and status.called


@respx.mock
async def test_native_run_refused_without_kek(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    status = bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports, preferred_path="native")
    printers_with_kek.kek = None

    with pytest.raises(ApiError) as refused:
        await prepare(printers_with_kek, reports, run_request(printer=pid))

    assert refused.value.status == 503
    assert (
        refused.value.detail == "LAN printing needs SCADBUDDY_SECRET_KEY_FILE on the print worker"
    )
    assert not status.called


@respx.mock
async def test_a_printer_only_the_lan_reaches_is_refused_until_it_can_print(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    """Its options carry printer_id 0, which nothing may key on (#4336 review M7): the run
    is refused before a rack, a remembered choice or a queue reads it."""
    bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports, preferred_path="native")
    printers_with_kek.unlink_bambuddy(pid)

    with pytest.raises(ApiError) as refused:
        await prepare(printers_with_kek, reports, run_request(printer=pid))

    assert refused.value.status == 422
    assert "only over the LAN" in refused.value.detail


@respx.mock
async def test_a_printer_bambuddy_does_not_reach_is_refused_on_the_bambuddy_path(
    printers: PrinterStore, reports: PrinterReportStore
) -> None:
    bambuddy_routes()
    row = printers.ensure_bambuddy(1, serial=SERIAL, name="Garage", model="H2C")
    printers.unlink_bambuddy(row.id)

    with pytest.raises(ApiError) as refused:
        await prepare(printers, reports, run_request(printer=row.id))

    assert refused.value.status == 422
    assert "not linked to Bambuddy" in refused.value.detail


@respx.mock
async def test_without_a_registry_the_run_is_bambuddys(
    printers: PrinterStore, reports: PrinterReportStore
) -> None:
    """A process without the printers' store (an older deployment's deps) runs as before."""
    bambuddy_routes()
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        prepared = await prepare_run(
            client, cast(PrintSource, OnePlate()), StoredSettings(), run_request(printer_id=1)
        )
    assert (prepared.printer, prepared.path, prepared.printer_id) == ("", "bambuddy", 1)
