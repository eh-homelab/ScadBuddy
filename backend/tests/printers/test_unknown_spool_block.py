"""ScadBuddy's own prints refuse a printer with an unknown tray (#7143): `prepare_run`
and `check_print` on both paths, and `lan_print` before its command. The reads are fakes
here: the block's rows and the Go route that serves them to the print worker are other
tasks' tests."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any, cast

import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.models import SpoolAssignment
from scadbuddy.bambuddy.print_run import (
    PreparedRun,
    PrintRunRequest,
    UnknownSpoolLoadedError,
    check_print,
    prepare_run,
    run_route,
)
from scadbuddy.bambuddy.print_source import PrintSource
from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.filament.inventory import SbSpoolReads
from scadbuddy.filament.models import FilamentInventorySpool, TrayPlace
from scadbuddy.filament.unknown_tray_rows import UNKNOWN_SPOOL_LOADED, UnknownSpoolRef
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.printers.lan import LanPrinter
from scadbuddy.printers.lan_models import LanStarted
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.print_activities import LanActivities, LanDeps
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool
from tests.printers.conftest import KEK
from tests.printers.test_lan_start import LAST, NAME, Scripted, _input, state
from tests.printers.test_lan_start import SERIAL as LAN_SERIAL
from tests.printers.test_run_printer import (
    API,
    HOT_PATH,
    OnePlate,
    SlicedPlate,
    bambuddy_routes,
    lan_linked,
    run_request,
)

pytestmark = pytest.mark.requires_postgres

REF = UnknownSpoolRef(id=uuid.UUID("5d0c8f1e-0000-4000-8000-000000007143"), slot="AMS-A slot 2")
DETAIL = "Garage has an unknown spool in AMS-A slot 2: link it on the Filament page first."


class FakeReads:
    """`UnknownSpoolReads` with the answer it is given, or the error."""

    def __init__(self, refs: list[UnknownSpoolRef], fail: Exception | None = None) -> None:
        self.refs = refs
        self.fail = fail
        self.calls = 0

    async def blocking(self, printer_id: str) -> list[UnknownSpoolRef]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return self.refs


class FakeSb:
    """The SB spools the Bambuddy path reads: ``held`` are the trays an SB spool is in."""

    def __init__(self, printer_id: str, held: Sequence[tuple[int, int]] = ()) -> None:
        self.rows_of = [
            FilamentInventorySpool.model_construct(
                loaded=TrayPlace(
                    printer_id=printer_id,
                    bambuddy_printer_id=1,
                    printer_name="Garage",
                    ams_id=ams,
                    tray_id=tray,
                )
            )
            for ams, tray in held
        ]

    async def rows(self, bb_assignments: Sequence[SpoolAssignment]) -> list[FilamentInventorySpool]:
        return self.rows_of

    async def bb_to_sb(self, ids: Sequence[int]) -> dict[int, int]:
        return {}


def assignments() -> respx.Route:
    return respx.get(f"{API}/inventory/assignments").mock(return_value=httpx.Response(200, json=[]))


@pytest.fixture
def reports(pg_pool: PgPool) -> PrinterReportStore:
    return PrinterReportStore(pg_pool)


async def prepare(
    printers: PrinterStore,
    reports: PrinterReportStore,
    request: PrintRunRequest,
    reads: FakeReads,
    *,
    source: Any = None,
    sb: FakeSb | None = None,
) -> PreparedRun:
    async with BambuddyClient(BambuddyConfig(base_url="http://bambuddy.test", api_key="k")) as c:
        return await prepare_run(
            c,
            cast(PrintSource, source or OnePlate()),
            StoredSettings(),
            request,
            printers=printers,
            reports=reports,
            unknown_trays=reads,
            sb_reads=cast(SbSpoolReads, sb),
        )


async def check(
    printers: PrinterStore,
    reports: PrinterReportStore,
    request: PrintRunRequest,
    reads: FakeReads,
    sb: FakeSb | None = None,
) -> Any:
    async with BambuddyClient(BambuddyConfig(base_url="http://bambuddy.test", api_key="k")) as c:
        return await check_print(
            c,
            cast(PrintSource, SlicedPlate()),
            StoredSettings(),
            request,
            printers=printers,
            reports=reports,
            sb_reads=cast(SbSpoolReads, sb),
            unknown_trays=reads,
        )


@respx.mock
async def test_prepare_run_refuses_a_printer_with_an_unknown_tray(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports)

    with pytest.raises(UnknownSpoolLoadedError) as refused:
        await prepare(printers_with_kek, reports, run_request(printer=pid), FakeReads([REF]))

    assert refused.value.status == 422
    assert refused.value.detail == DETAIL
    assert refused.value.extensions["code"] == UNKNOWN_SPOOL_LOADED
    assert refused.value.refs == [REF]
    assert not [c for c in respx.calls if c.request.method in ("POST", "PUT")]


@respx.mock
async def test_prepare_run_without_unknown_trays_prepares(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    bambuddy_routes()
    inventory = assignments()
    pid = lan_linked(printers_with_kek, reports)

    prepared = await prepare(printers_with_kek, reports, run_request(printer=pid), FakeReads([]))

    assert isinstance(prepared, PreparedRun)
    assert not inventory.called  # the Bambuddy read is lazy


@respx.mock
async def test_a_sliced_file_is_refused_too(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports)

    with pytest.raises(UnknownSpoolLoadedError):
        await prepare(
            printers_with_kek,
            reports,
            run_request(printer=pid),
            FakeReads([REF]),
            source=SlicedPlate(),
        )


@respx.mock
async def test_the_check_says_it_as_an_error_with_the_tray_ids(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports)

    result = await check(printers_with_kek, reports, run_request(printer=pid), FakeReads([REF]))

    assert result.errors == [DETAIL]
    assert result.unknown_trays == [REF]


@respx.mock
async def test_prepare_run_goes_on_when_the_area_is_not_served(
    printers_with_kek: PrinterStore, reports: PrinterReportStore, caplog: pytest.LogCaptureFixture
) -> None:
    """G3: the API is older than the worker, so the route is missing."""
    bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports)
    missing = CrudUnavailableError("404 without X-ScadBuddy-Crud", "route_missing")

    with caplog.at_level(logging.WARNING):
        prepared = await prepare(
            printers_with_kek, reports, run_request(printer=pid), FakeReads([], fail=missing)
        )

    assert isinstance(prepared, PreparedRun)
    [record] = [r for r in caplog.records if getattr(r, "area", None) == "unknown-trays"]
    assert record.levelno == logging.WARNING
    assert record.__dict__["error"] == "CrudUnavailableError"


@respx.mock
async def test_prepare_run_fails_on_a_refused_credential(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    bambuddy_routes()
    pid = lan_linked(printers_with_kek, reports)
    refused = CrudRefusedError(403, {"detail": "wrong caller"})

    with pytest.raises(CrudRefusedError):
        await prepare(
            printers_with_kek, reports, run_request(printer=pid), FakeReads([], fail=refused)
        )


@respx.mock
async def test_bambuddy_path_does_not_block_an_assigned_tray(
    printers_with_kek: PrinterStore, reports: PrinterReportStore
) -> None:
    """D9: an SB spool is in the tray the ref names, so the tray is not unknown. The check
    agrees with the run; the native path has no such read and still refuses."""
    bambuddy_routes()
    inventory = assignments()
    pid = lan_linked(printers_with_kek, reports)
    request = run_request(printer=pid)
    sb = FakeSb(pid, held=[(0, 1)])

    prepared = await prepare(printers_with_kek, reports, request, FakeReads([REF]), sb=sb)
    result = await check(printers_with_kek, reports, request, FakeReads([REF]), sb)

    assert isinstance(prepared, PreparedRun)
    assert inventory.called
    assert result.errors == [] and result.unknown_trays == []

    # Another tray holds the SB spool: this one is still unknown.
    with pytest.raises(UnknownSpoolLoadedError):
        await prepare(
            printers_with_kek,
            reports,
            request,
            FakeReads([REF]),
            sb=FakeSb(pid, held=[(0, 2)]),
        )

    settings = StoredSettings(**HOT_PATH)
    route = await run_route(settings, printers_with_kek, reports, printer=pid, printer_id=None)
    assert route.native
    with pytest.raises(UnknownSpoolLoadedError):
        await prepare_run(
            None,
            cast(PrintSource, OnePlate()),
            settings,
            request,
            printers=printers_with_kek,
            reports=reports,
            route=route,
            unknown_trays=FakeReads([REF]),
            sb_reads=cast(SbSpoolReads, sb),
        )


# ── The LAN start ───────────────────────────────────────────────────────────────────


async def _lan(pg_pool: PgPool, lan: Scripted, reads: FakeReads) -> LanStarted | ApplicationError:
    printers = PrinterStore(pg_pool, KEK)
    row = printers.create(PrinterCreate(serial=LAN_SERIAL, name="Garage"))
    printers.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    deps = LanDeps(
        printers=printers,
        settings_store=cast(SettingsStore, None),
        connect=lambda *_: cast(LanPrinter, lan),
        start_wait=2,
        unknown_trays=reads,
    )
    try:
        return await ActivityEnvironment().run(
            LanActivities(deps).lan_print, _input().model_copy(update={"printer": row.id})
        )
    except ApplicationError as error:
        return error


async def test_lan_print_refuses_before_the_command(pg_pool: PgPool) -> None:
    lan = Scripted(state(gcode_state="FINISH", **LAST), [])

    outcome = await _lan(pg_pool, lan, FakeReads([REF]))

    assert isinstance(outcome, ApplicationError)
    assert outcome.details[0].status == 422
    assert outcome.details[0].detail == DETAIL
    assert lan.commands == []


async def test_lan_print_starts_when_the_read_fails(
    pg_pool: PgPool, caplog: pytest.LogCaptureFixture
) -> None:
    running = state(gcode_state="RUNNING", subtask_name=NAME)
    lan = Scripted(state(gcode_state="FINISH", **LAST), [running])

    with caplog.at_level(logging.WARNING):
        outcome = await _lan(pg_pool, lan, FakeReads([], fail=RuntimeError("down")))

    assert isinstance(outcome, LanStarted)
    [record] = [r for r in caplog.records if "unknown trays" in r.getMessage()]
    assert record.__dict__["error"] == "RuntimeError"


async def test_a_start_already_taken_is_not_refused(pg_pool: PgPool) -> None:
    reads = FakeReads([REF])
    lan = Scripted(state(gcode_state="RUNNING", subtask_name=NAME), [])

    outcome = await _lan(pg_pool, lan, reads)

    assert isinstance(outcome, LanStarted)
    assert reads.calls == 0 and lan.commands == []
