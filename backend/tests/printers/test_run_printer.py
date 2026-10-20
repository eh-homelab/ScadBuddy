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
from scadbuddy.bambuddy.print_run import (
    LAN_NEEDS_KEK,
    PreparedRun,
    PrintRunRequest,
    check_print,
    prepare_run,
)
from scadbuddy.bambuddy.print_source import PrintSource
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.models import LanLink, PrinterCreate, PrinterPatch
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


class SlicedPlate(OnePlate):
    """A sliced file: the check judges it from `prepare_run` alone, status included."""

    is_sliced = True
    sliced_profile = None


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
    """The named row links another Bambuddy printer now: the one conflict refused, and it
    says which printer the registry links (#4442 I1)."""
    bambuddy_routes()
    row = printers.ensure_bambuddy(1, serial=SERIAL, name="Garage", model="H2C")

    with pytest.raises(ApiError) as refused:
        await prepare(printers, reports, run_request(printer=row.id, printer_id=2))

    assert refused.value.status == 422
    assert "disagreeing printer" in refused.value.detail
    assert "Garage is Bambuddy printer 1 now, not 2" in refused.value.detail


@respx.mock
async def test_a_printer_merged_away_falls_back_to_the_bambuddy_printers_row(
    printers: PrinterStore, reports: PrinterReportStore
) -> None:
    """#4442 I1: the strip's synthetic row was merged into the row added by serial; a
    body still naming it prints on the kept row, as ``printer_id`` says."""
    status = bambuddy_routes()
    synthetic = printers.ensure_bambuddy(1, serial=None, name="H2C", model="H2C")
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    assert printers.ensure_bambuddy(1, serial=SERIAL, name="H2C", model="H2C").id == mine.id
    assert printers.get(synthetic.id) is None

    prepared = await prepare(printers, reports, run_request(printer=synthetic.id, printer_id=1))

    assert (prepared.printer, prepared.printer_id, prepared.path) == (mine.id, 1, "bambuddy")
    assert status.called


@respx.mock
async def test_a_printer_unlinked_since_falls_back_to_the_bambuddy_printers_row(
    printers: PrinterStore, reports: PrinterReportStore
) -> None:
    bambuddy_routes()
    row = printers.ensure_bambuddy(1, serial=SERIAL, name="Garage", model="H2C")
    printers.unlink_bambuddy(row.id)

    prepared = await prepare(printers, reports, run_request(printer=row.id, printer_id=1))

    now = printers.by_bambuddy_id(1)
    assert now is not None and prepared.printer == now.id and prepared.printer_id == 1


@respx.mock
async def test_a_printer_re_linked_elsewhere_is_a_disagreement(
    printers: PrinterStore, reports: PrinterReportStore
) -> None:
    bambuddy_routes()
    row = printers.ensure_bambuddy(1, serial=SERIAL, name="Garage", model="H2C")
    printers.unlink_bambuddy(row.id)
    printers.link_bambuddy(row.id, 2)

    with pytest.raises(ApiError) as refused:
        await prepare(printers, reports, run_request(printer=row.id, printer_id=1))

    assert refused.value.status == 422
    assert "Garage is Bambuddy printer 2 now, not 1" in refused.value.detail


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
async def test_an_auto_printer_prints_through_bambuddy_without_kek(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    """Task 8 queues a linked row through Bambuddy and never opens the access code, so a
    worker without the key takes Bambuddy's path for an ``auto`` row, not the 503."""
    status = bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports)
    printers_with_kek.kek = None

    prepared = await prepare(printers_with_kek, reports, run_request(printer=pid))

    assert (prepared.printer, prepared.path, prepared.printer_id) == (pid, "bambuddy", 1)
    assert status.called


@respx.mock
async def test_a_native_printer_that_never_reported_is_refused(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    status = bambuddy_routes()
    row = printers_with_kek.ensure_bambuddy(1, serial=SERIAL, name="Garage", model="H2C")
    printers_with_kek.patch(row.id, PrinterPatch.model_validate({"preferred_path": "native"}))

    with pytest.raises(ApiError) as refused:
        await prepare(printers_with_kek, reports, run_request(printer=row.id))

    assert refused.value.status == 422
    assert "has not reported over the LAN yet" in refused.value.detail
    assert not status.called


@respx.mock
async def test_a_native_printer_whose_report_is_stale_is_refused(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    """#4442 I3: past the window `/printers/now` calls it offline; its trays are not known."""
    status = bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports, preferred_path="native")
    reports.put(pid, REPORT["print"], datetime.now(UTC) - timedelta(minutes=5))

    with pytest.raises(ApiError) as refused:
        await prepare(printers_with_kek, reports, run_request(printer=pid))

    assert refused.value.status == 422
    assert "has not reported over the LAN for 5 min" in refused.value.detail
    assert not status.called


@respx.mock
async def test_a_row_this_run_links_takes_its_own_path(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    """#4442 M2: no row links Bambuddy printer 1 yet, but a LAN-linked one holds its
    serial; the run links it and takes its LAN path from this first run."""
    status = bambuddy_routes()
    row = printers_with_kek.create(PrinterCreate(serial=SERIAL, name="Garage"))
    printers_with_kek.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256=CERT
    )
    printers_with_kek.patch(row.id, PrinterPatch.model_validate({"preferred_path": "auto"}))
    reports.put(row.id, REPORT["print"], datetime.now(UTC))

    prepared = await prepare(printers_with_kek, reports, run_request(printer_id=1))

    assert (prepared.printer, prepared.path, prepared.printer_id) == (row.id, "native", 1)
    assert not status.called


@respx.mock
async def test_a_lan_only_printer_is_told_to_link_bambuddy_before_the_key(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    """#4442 M4: linking Bambuddy is what prints it now, so that is the refusal it gets."""
    bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports, preferred_path="native")
    printers_with_kek.unlink_bambuddy(pid)
    printers_with_kek.kek = None

    with pytest.raises(ApiError) as refused:
        await prepare(printers_with_kek, reports, run_request(printer=pid))

    assert refused.value.status == 422
    assert "only over the LAN" in refused.value.detail


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


# ── #4442 I2: the check refuses as the run does, and reads the same status ──────────


def refusal(scenario: str, printers: PrinterStore, reports: PrinterReportStore) -> PrintRunRequest:
    """A request the run refuses, set up as ``scenario`` says."""
    if scenario == "unknown":
        return run_request(printer="00000000-0000-4000-8000-000000000009")
    if scenario == "disagreeing":
        row = printers.ensure_bambuddy(1, serial=SERIAL, name="Garage", model="H2C")
        return run_request(printer=row.id, printer_id=2)
    if scenario == "not-linked":
        row = printers.ensure_bambuddy(1, serial=SERIAL, name="Garage", model="H2C")
        printers.unlink_bambuddy(row.id)
        return run_request(printer=row.id)
    pid = lan_linked(printers, reports, preferred_path="native")
    if scenario == "lan-only":
        printers.unlink_bambuddy(pid)
    elif scenario == "stale":
        reports.put(pid, REPORT["print"], datetime.now(UTC) - timedelta(minutes=5))
    else:
        assert scenario == "no-key"
        printers.kek = None
    return run_request(printer=pid)


@respx.mock
@pytest.mark.parametrize(
    "scenario", ["unknown", "disagreeing", "not-linked", "lan-only", "stale", "no-key"]
)
async def test_the_check_refuses_what_the_run_refuses_in_its_words(
    printers_with_kek: PrinterStore, reports: PrinterReportStore, scenario: str
) -> None:
    bambuddy_routes()
    request = refusal(scenario, printers_with_kek, reports)

    with pytest.raises(ApiError) as run:
        await prepare(printers_with_kek, reports, request)
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        if run.value.status == 503:
            with pytest.raises(ApiError) as checked:
                await check_print(
                    client,
                    cast(PrintSource, OnePlate()),
                    StoredSettings(),
                    request,
                    printers=printers_with_kek,
                    reports=reports,
                )
            assert (checked.value.status, checked.value.detail) == (503, LAN_NEEDS_KEK)
            return
        check = await check_print(
            client,
            cast(PrintSource, OnePlate()),
            StoredSettings(),
            request,
            printers=printers_with_kek,
            reports=reports,
        )

    assert check.errors == [run.value.detail]


@respx.mock
async def test_the_check_reads_a_native_printers_status_from_its_report(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    """D4 in the check too: on the LAN path Bambuddy's status is never read."""
    status = bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports)

    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        check = await check_print(
            client,
            cast(PrintSource, SlicedPlate()),
            StoredSettings(),
            run_request(printer=pid),
            printers=printers_with_kek,
            reports=reports,
        )

    assert check.errors == []
    assert not status.called
