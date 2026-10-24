"""The run names a registry printer and chooses its path (#3823, plan 2026-10-16 Task 8):
`prepare_run` resolves the row (named, else the one for the Bambuddy printer, made when
there is none), refuses fields that disagree, and on a native path reads the printer's
status from its LAN report, never from Bambuddy (hot-path plan D4)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.print_run import (
    LAN_NEEDS_KEK,
    PreparedRun,
    PrintRunRequest,
    check_print,
    ensured_route,
    prepare_run,
    run_route,
)
from scadbuddy.bambuddy.print_source import PrintSource
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.print_library.ids import NATIVE_ID_BASE
from scadbuddy.printers.models import LanLink, PrinterCreate, PrinterPatch
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.workflows import print_activities
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from scadbuddy.workflows.print_models import CheckInput, SourceSpec
from tests.bambuddy.conftest import recording
from tests.conftest import PgPool
from tests.test_print_workflow import run_input

pytestmark = pytest.mark.requires_postgres

BASE = "http://bambuddy.test"
API = f"{BASE}/api/v1"
SERIAL = "01P00A0000000AA"
CERT = "cd" * 32
#: The hot path's stored backends (plan 2026-10-18 D2).
HOT_PATH: dict[str, Any] = {
    "slicer_backend": "native",
    "library_backend": "native",
    "store_backend": "s3",
}
REPORT = json.loads((Path(__file__).parent / "fixtures" / "lan-report-h2c.json").read_text())


class OnePlate:
    """The little of a `PrintSource` that `prepare_run` reads for an unsliced file."""

    is_sliced = False

    async def plate_ids(self, client: BambuddyClient) -> list[int]:
        return [1]

    async def states_nozzles(self, client: BambuddyClient) -> bool:
        """Read by the check beside `prepare_run` (#4196), and dropped when it refuses."""
        return False


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
    *,
    refuse_manual_pick: bool = True,
) -> PreparedRun:
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        return await prepare_run(
            client,
            cast(PrintSource, OnePlate()),
            settings or StoredSettings(),
            request,
            printers=printers,
            reports=reports,
            refuse_manual_pick=refuse_manual_pick,
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
async def test_a_printer_unlinked_since_is_a_disagreement(
    printers: PrinterStore, reports: PrinterReportStore
) -> None:
    """#4516: a named row that links no Bambuddy printer (one only the LAN reaches, or
    unlinked since) never prints on the ``printer_id`` sent beside it."""
    bambuddy_routes()
    row = printers.ensure_bambuddy(1, serial=SERIAL, name="Garage", model="H2C")
    printers.unlink_bambuddy(row.id)

    with pytest.raises(ApiError) as refused:
        await prepare(printers, reports, run_request(printer=row.id, printer_id=1))

    assert refused.value.status == 422
    assert "Garage is not linked to Bambuddy printer 1" in refused.value.detail


@respx.mock
async def test_a_lan_only_row_beside_a_bambuddy_id_never_prints_there(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    """#4516: on the hot path's backends too, a LAN-only row named with a stale
    ``printer_id`` is refused, not printed on the Bambuddy printer or over the LAN."""
    status = bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports, preferred_path="native")
    printers_with_kek.unlink_bambuddy(pid)

    with pytest.raises(ApiError) as refused:
        await run_route(
            StoredSettings(**HOT_PATH),
            printers_with_kek,
            reports,
            printer=pid,
            printer_id=1,
        )

    assert refused.value.status == 422
    assert "disagreeing printer" in refused.value.detail
    assert not status.called


@respx.mock
async def test_the_check_makes_a_first_runs_row_before_it_decides_the_path(
    printers_with_kek: PrinterStore,
    reports: PrinterReportStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#4442 M2 on the hot path: no row links Bambuddy printer 1 yet, but a LAN-linked
    row holds its serial. The run's check merges it first, so the first run is native."""
    bambuddy_routes()
    row = printers_with_kek.create(PrinterCreate(serial=SERIAL, name="Garage", model="H2C"))
    printers_with_kek.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256=CERT
    )
    printers_with_kek.patch(row.id, PrinterPatch.model_validate({"preferred_path": "auto"}))
    reports.put(row.id, REPORT["print"], datetime.now(UTC))
    seen: dict[str, Any] = {}

    async def stop(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise ApiError(418, "stop here")

    unused: Any = None
    settings = StoredSettings(bambuddy_url=BASE, bambuddy_api_key="k", **HOT_PATH)
    activities = PrintActivities(
        PrintDeps(
            settings_store=cast(Any, SimpleNamespace(load=lambda: settings)),
            outputs=unused,
            uploads=unused,
            prints=unused,
            store=unused,
            observer=unused,
            library=cast(Any, object()),
            native=cast(Any, SimpleNamespace(profiles=unused)),
            printers=printers_with_kek,
            reports=reports,
        )
    )

    async def source(*args: Any) -> Any:
        return object()

    monkeypatch.setattr(print_activities, "prepare_run", stop)
    monkeypatch.setattr(activities, "_source", source)
    check = CheckInput(
        input=run_input().model_copy(
            update={
                "request": run_request(printer_id=1),
                "source": SourceSpec(kind="library", file_id=NATIVE_ID_BASE + 1),
            }
        ),
        started_at=datetime.now(UTC),
    )
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(activities.check, check)

    assert raised.value.details[0].status == 418, raised.value
    route = seen["route"]
    assert (route.row.id, route.path, route.native) == (row.id, "native", True)


@respx.mock
async def test_a_first_run_on_bambuddys_path_reads_the_printer_once(
    printers: PrinterStore, reports: PrinterReportStore
) -> None:
    """#4551: on the hot path's backends, a Bambuddy printer with no row yet is read
    once, by `ensured_route`, and `prepare_run` takes that printer rather than listing
    Bambuddy's printers again."""
    bambuddy_routes()
    listing = respx.routes[0]
    settings = StoredSettings(**HOT_PATH)
    request = run_request(printer_id=1)
    route = await run_route(settings, printers, reports, printer=None, printer_id=1)
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        route = await ensured_route(client, settings, printers, reports, route)
        assert route.row is not None and not route.native
        prepared = await prepare_run(
            client,
            cast(PrintSource, OnePlate()),
            settings,
            request,
            printers=printers,
            reports=reports,
            route=route,
        )

    assert (prepared.printer, prepared.printer_id, prepared.path) == (route.row.id, 1, "bambuddy")
    assert listing.call_count == 1


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
    printers_with_kek: PrinterStore, reports: PrinterReportStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Task 8 queues a linked row through Bambuddy and never opens the access code, so a
    worker without the key takes Bambuddy's path for an ``auto`` row, not the 503."""
    status = bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports)
    printers_with_kek.kek = None

    with caplog.at_level(logging.INFO, logger="scadbuddy.bambuddy.print_run"):
        prepared = await prepare(printers_with_kek, reports, run_request(printer=pid))

    assert (prepared.printer, prepared.path, prepared.printer_id) == (pid, "bambuddy", 1)
    assert status.called
    assert "no key for the LAN" in caplog.text

    # The dialog's checks prepare the same run on every re-check, and say nothing.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="scadbuddy.bambuddy.print_run"):
        await prepare(
            printers_with_kek, reports, run_request(printer=pid), refuse_manual_pick=False
        )
    assert "no key for the LAN" not in caplog.text


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
