"""AC1 and AC2 of the hot path without Bambuddy (#3818 tier A, plan 2026-10-18 Tasks 9
and 10): with Bambuddy configured, a native run makes no Bambuddy call from render to
follow; with no ``bambuddy_url`` at all, the same run and the dialog's reads still
answer, and only the routes that are Bambuddy's by nature say Settings has none.

The app runs on the hot path's backends (D2: the S3 store, the native library and the
native slicer, the fake slicer in the Rust worker's place); its one printer is a
registry row linked over the LAN to the fake printer (`tests/support/fake_lan.py`),
whose watch keeps its report fresh, and set to print natively. The spool providers'
overlay is written once before the window opens, as its refresh would (outside AC1).
Inside `forbid_bambuddy`, the dialog's reads, the background slice, the run and the
follow each answer; a call to Bambuddy fails the test, naming it."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
import trimesh
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.bambuddy import print_run
from scadbuddy.bambuddy.filaments import gather_plate_options
from scadbuddy.bambuddy.print_run import run_route
from scadbuddy.bambuddy.trays import tray_spool_id
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.printers.component import PRINTERS, Printers
from scadbuddy.printers.models import LanLink, PrinterCreate, PrinterPatch
from scadbuddy.render.bambu3mf import PlateParts, write_plates_3mf
from scadbuddy.render.split import ColourPart
from scadbuddy.spools.component import SPOOLS, Spools
from scadbuddy.spools.models import Overlay, SpoolSnapshot
from scadbuddy.workflows import print_activities
from tests.api.conftest import follow_operation, set_fake_env
from tests.api.test_customers import create as create_customer
from tests.api.test_library_native import _one_colour, _upload
from tests.api.test_order_quote import price_book, sliced_plate
from tests.api.test_orders import create_order
from tests.api.test_orders_lifecycle import add_item
from tests.api.test_print_preview_native import _settings, _state, dialog_profiles
from tests.api.test_print_preview_native import kek_file as kek_file  # the fixture, shared
from tests.api.test_print_run_choices import follow_run
from tests.api.test_send import BASE, configure, make_output
from tests.conftest import write_openscad_3mf
from tests.support.fake_lan import FakeLan, needs_fake_lan, running_fake_lan
from tests.support.fake_slicer import FakeSlicer, fake_slicer_worker
from tests.support.no_bambuddy import BambuddyCalledError, forbid_bambuddy
from tests.support.operations import press

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal, *needs_fake_lan]

#: The fake printer's tagged tray (AMS 0 tray 0, its ``report.json``) and untagged one.
TAGGED_UUID = "5CADB0DDF1A7E5000000000000000001"
TAGGED = tray_spool_id(0, 0)
UNTAGGED = tray_spool_id(0, 1)


@pytest.fixture
def two_colours(fake_openscad: str) -> None:
    """The fake openscad renders two colours, so the plan prints from two trays."""
    directory = Path(fake_openscad).parent
    shifted = trimesh.creation.box(extents=(4, 4, 4))
    shifted.apply_translation((0, 0, 5))
    drawn = write_openscad_3mf(
        directory / "drawn-two.3mf",
        [
            ("Color 1", "#F5547CFF", trimesh.creation.box(extents=(10, 10, 5))),
            ("Color 2", "#FFFFFFFF", shifted),
        ],
    )
    set_fake_env(directory, "FAKE_3MF", str(drawn))


def tray_profiles(root: Path) -> Path:
    """The dialog's profile tree (`dialog_profiles`), plus a generic PLA for the H2C's
    0.4 nozzle under the untagged tray's ``tray_info_idx`` (``GFL99``), as the image's
    tree has one (Bambu Studio's ``Generic PLA @BBL H2C``)."""
    dialog_profiles(root)
    name = "Generic PLA @BBL H2C"
    doc = {
        "type": "filament",
        "name": name,
        "inherits": "fdm_filament_root",
        "from": "system",
        "instantiation": "true",
        "setting_id": "GFSL99_00",
        "filament_id": "GFL99",
        "compatible_printers": ["Bambu Lab H2C 0.4 nozzle"],
    }
    (root / "BBL" / "filament" / f"{name}.json").write_text(json.dumps(doc))
    vendor_file = root / "BBL.json"
    vendor = json.loads(vendor_file.read_text())
    vendor["filament_list"].append({"name": name, "sub_path": f"filament/{name}.json"})
    vendor_file.write_text(json.dumps(vendor))
    return root


@pytest.fixture
def native_app(
    request: pytest.FixtureRequest,
    model: str,
    settings: Settings,
    s3: S3Settings,
    kek_file: Path,
    tmp_path: Path,
    two_colours: None,
) -> Iterator[TestClient]:
    """The app on the hot path's backends, with Bambuddy configured (`configure`), or,
    parametrised ``False`` (``indirect``), with no ``bambuddy_url`` at all (AC2)."""
    profiles = tray_profiles(tmp_path / "profiles")
    app_settings = _settings(settings, s3, kek_file, profiles, library_backend="native")
    with respx.mock(assert_all_called=False) as router:
        # Loopback (moto's S3, the fake slicer's transfers) passes through; Bambuddy is
        # answered by nothing outside the window, so a call there fails too.
        router.route(host="127.0.0.1").pass_through()
        with TestClient(create_app(app_settings)) as client:
            if getattr(request, "param", True):
                configure(client)
            else:
                assert _state(client).settings_store.load().bambuddy_url is None
            yield client


@pytest.fixture
def slicer(native_app: TestClient, settings: Settings) -> Iterator[FakeSlicer]:
    with fake_slicer_worker(native_app, settings.temporal_task_queue_slicer) as fake:
        yield fake


@pytest.fixture
def fake_lan(
    native_app: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FakeLan]:
    """The fake printer, run in the app's own loop."""
    assert native_app.portal is not None
    with native_app.portal.wrap_async_context_manager(
        running_fake_lan(tmp_path / "printer", monkeypatch, print_seconds=2.0)
    ) as lan:
        yield lan


def _printers(client: TestClient) -> Printers:
    printers: Printers = _state(client).components.get(PRINTERS)
    return printers


def _spools(client: TestClient) -> Spools:
    spools: Spools = _state(client).components.get(SPOOLS)
    return spools


def _call(client: TestClient, fn: Any, *args: Any, **kwargs: Any) -> Any:
    assert client.portal is not None

    async def run() -> Any:
        return await fn(*args, **kwargs)

    return client.portal.call(run)


def lan_row(
    client: TestClient,
    lan: FakeLan,
    *,
    preferred_path: str = "native",
    bambuddy_printer_id: int | None = None,
) -> str:
    """A registry row linked to the fake printer over the LAN (and to a Bambuddy printer
    with ``bambuddy_printer_id``), its watch started, and its first report in."""
    printers = _printers(client)
    row = printers.store.create(PrinterCreate(serial=lan.serial, name="Fake H2C", model="H2C"))
    printers.store.link_lan(
        row.id,
        LanLink(host=lan.host, access_code=lan.access_code),
        cert_sha256=lan.cert_sha256,
    )
    if bambuddy_printer_id is not None:
        printers.store.link_bambuddy(row.id, bambuddy_printer_id)
    printers.store.patch(row.id, PrinterPatch(preferred_path=preferred_path))  # type: ignore[arg-type]
    _call(client, printers.watches.ensure, row.id)
    deadline = time.monotonic() + 30
    while printers.reports.get(row.id) is None:
        assert time.monotonic() < deadline, "the watch wrote no report"
        time.sleep(0.1)
    return row.id


def overlay(client: TestClient, *, at: datetime | None = None, remaining_g: float = 640.0) -> None:
    """Bambuddy's record of the tagged spool, as the refresh writes it (outside AC1)."""
    snapshot = SpoolSnapshot(
        record_id="16",
        material="PLA",
        colour="#F5547C",
        brand="Bambu",
        remaining_g=remaining_g,
        tray_uuid=TAGGED_UUID,
    )
    _call(
        client,
        _spools(client).store.replace_records,
        "bambuddy",
        [snapshot],
        at or datetime.now(UTC),
    )


def run_body(row: str, filaments: dict[str, Any]) -> dict[str, Any]:
    """The dialog's request on the native path: the row, its trays and a 0.4 nozzle."""
    slots = {slot["slot_id"] for slot in filaments["slots"]}
    trays = {1: TAGGED, 2: UNTAGGED}
    return {
        "printer": row,
        "copies": 1,
        "filament_plan": {
            "slots": [{"slot_id": slot, "spool_id": trays[slot]} for slot in sorted(slots)]
        },
        "choices": {"nozzles": [{"size": "0.4"}], "tier": "standard"},
    }


def wait_preview(client: TestClient, output: str, job_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while True:
        response = client.get(
            f"/api/v1/print/outputs/{output}/preview-slices/{job_id}",
            params={"slicer": "native"},
        )
        assert response.status_code == 200, response.text
        answer: dict[str, Any] = response.json()
        if answer["status"] != "running":
            return answer
        assert time.monotonic() < deadline, answer
        time.sleep(0.2)


def accept_and_wait(client: TestClient, output: str, body: dict[str, Any]) -> dict[str, Any]:
    started = client.post(f"/api/v1/print/outputs/{output}/run", json=body, headers=press())
    assert started.status_code == 202, started.text
    return follow_run(client, started.json()["id"], timeout=120)


def follow_until_settled(client: TestClient, output: str) -> dict[str, Any]:
    """The output's progress, read as the dialog's send bar reads it, until it settles:
    the LAN print's (``route: "lan"``), from the fake printer's reports."""
    deadline = time.monotonic() + 60
    while True:
        response = client.get(f"/api/v1/print/outputs/{output}/progress")
        assert response.status_code == 200, response.text
        progress: dict[str, Any] = response.json()
        if progress is not None and progress["settled"]:
            return progress
        assert time.monotonic() < deadline, progress
        time.sleep(0.3)


def _dialog_and_run(
    client: TestClient, model: str, row: str, *, copies: int = 1
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    output = make_output(client, model)
    choices = client.get(f"/api/v1/print/outputs/{output}/choices", params={"printer": row})
    assert choices.status_code == 200, choices.text
    filaments = client.get(f"/api/v1/print/outputs/{output}/filaments", params={"printer": row})
    assert filaments.status_code == 200, filaments.text
    body = {**run_body(row, filaments.json()), "copies": copies}
    checked = client.post(f"/api/v1/print/outputs/{output}/check", json=body)
    assert checked.status_code == 200, checked.text
    assert checked.json()["errors"] == [], checked.json()
    started = client.post(
        f"/api/v1/print/outputs/{output}/preview-slice", json=body, headers=press()
    )
    assert started.status_code == 202, started.text
    op = follow_operation(client, started.json(), timeout=60, poll=0.1)
    assert op["status"] == "succeeded", op
    assert op["result"]["slicer"] == "native"
    preview = wait_preview(client, output, op["result"]["job_id"])
    assert preview["status"] == "completed", preview
    run = accept_and_wait(client, output, body)
    return output, choices.json(), filaments.json(), run


@pytest.mark.parametrize("bambuddy_printer_id", [None, 1], ids=["lan-only", "both-links"])
def test_a_native_run_makes_no_bambuddy_call(
    native_app: TestClient,
    model: str,
    spans: InMemorySpanExporter,
    fake_lan: FakeLan,
    slicer: FakeSlicer,
    bambuddy_printer_id: int | None,
) -> None:
    """A row Bambuddy does not reach prints natively too: it was refused before Task 9
    (`_not_on_bambuddy`)."""
    row = lan_row(native_app, fake_lan, bambuddy_printer_id=bambuddy_printer_id)
    overlay(native_app)

    with forbid_bambuddy(spans, BASE):
        output, choices, filaments, run = _dialog_and_run(native_app, model, row)
        assert run["status"] == "succeeded", run
        progress = follow_until_settled(native_app, output)
        # Now printing (AC6): every printer is native, so the strip asks Bambuddy nothing.
        now = native_app.get("/api/v1/print/printers/now")
        assert now.status_code == 200, now.text

    assert (progress["route"], progress["stage"]) == ("lan", "done"), progress
    card = next(card for card in now.json()["printers"] if card["id"] == row)
    assert card["path"] == "native"
    assert row in choices["lan_printers"]
    assert choices["printer_id"] == bambuddy_printer_id
    assert filaments["source"] == "trays"
    tagged = next(spool for spool in filaments["spools"] if spool["spool_id"] == TAGGED)
    assert tagged["records"]["bambuddy"]["record_id"] == "16"
    assert is_native_id(run["result"]["library_file_id"])
    assert run["result"]["path"] == "native"
    # The chosen trays reach the printer (AC4). No ``ams_mapping2``: the measured start
    # sends none (`printers/lan.py` `project_file_command`, Task 7).
    sent = fake_lan.project_files()[-1]
    assert sent["ams_mapping"] == [0, 1]
    assert sent["use_ams"] is True


def test_a_native_run_of_two_copies_prints_both_and_asks_bambuddy_nothing(
    native_app: TestClient,
    model: str,
    spans: InMemorySpanExporter,
    fake_lan: FakeLan,
    slicer: FakeSlicer,
) -> None:
    """#4541: refused before; the run starts the second copy once the printer reports
    the first finished, and the print settles once both have."""
    row = lan_row(native_app, fake_lan, bambuddy_printer_id=1)
    overlay(native_app)
    with forbid_bambuddy(spans, BASE):
        output, _, _, run = _dialog_and_run(native_app, model, row, copies=2)
        assert run["status"] == "succeeded", run
        progress = follow_until_settled(native_app, output)
    assert (progress["route"], progress["stage"]) == ("lan", "done"), progress
    assert (progress["copies"], progress["copies_completed"]) == (2, 2), progress
    names = [sent["subtask_name"] for sent in fake_lan.project_files()]
    assert len(names) == 2 and names[1] == f"{names[0]}-c2"


def _a_colour_a_plate(paths: DataPaths, output: str) -> None:
    """The output's 3MF laid out as an arranged one is (#4710): plate 1 prints only slot
    1 and plate 2 only slot 2, both of the file's two filaments."""
    [output_3mf] = paths.outputs.glob(f"*/{output}/model.3mf")
    colours = ["#F5547C", "#FFFFFF"]
    parts = [
        ColourPart(index, f"Color {index + 1}", colour, trimesh.creation.box(extents=(10, 10, 5)))
        for index, colour in enumerate(colours)
    ]
    plates = [PlateParts((parts[0],), (1,)), PlateParts((parts[1],), (2,))]
    write_plates_3mf(plates, colours, output_3mf, thumbnails=None)


def test_one_plate_of_several_slices_with_only_its_own_slots(
    native_app: TestClient,
    model: str,
    paths: DataPaths,
    spans: InMemorySpanExporter,
    fake_lan: FakeLan,
    slicer: FakeSlicer,
) -> None:
    """#4710: for one plate the dialog sends a spool for that plate's slots only, and
    `/check` judges the request so (#1050). The background slice and the run judged every
    slot of the file instead: nothing read the output's own slots in their process, and
    the native library's copy answers none, so every colour of the file counted as used."""
    row = lan_row(native_app, fake_lan)
    overlay(native_app)
    output = make_output(native_app, model)
    _a_colour_a_plate(paths, output)
    body = {
        "printer": row,
        "copies": 1,
        "plate_id": 2,
        "filament_plan": {"slots": [{"slot_id": 2, "spool_id": UNTAGGED}]},
        "choices": {"nozzles": [{"size": "0.4"}], "tier": "standard"},
    }

    with forbid_bambuddy(spans, BASE):
        filaments = native_app.get(
            f"/api/v1/print/outputs/{output}/filaments",
            params={"printer": row, "plate_id": 2},
        )
        assert filaments.status_code == 200, filaments.text
        assert [slot["slot_id"] for slot in filaments.json()["slots"]] == [2]
        checked = native_app.post(f"/api/v1/print/outputs/{output}/check", json=body)
        assert checked.status_code == 200, checked.text
        assert checked.json()["errors"] == [], checked.json()
        started = native_app.post(
            f"/api/v1/print/outputs/{output}/preview-slice", json=body, headers=press()
        )
        assert started.status_code == 202, started.text
        op = follow_operation(native_app, started.json(), timeout=60, poll=0.1)
        assert op["status"] == "succeeded", op.get("error")
        assert op["result"]["plate_id"] == 2
        preview = wait_preview(native_app, output, op["result"]["job_id"])
        assert preview["status"] == "completed", preview
        run = accept_and_wait(native_app, output, body)

    assert run["status"] == "succeeded", run


def test_a_native_runs_plan_lays_the_overlay_over_its_trays(
    native_app: TestClient,
    model: str,
    spans: InMemorySpanExporter,
    fake_lan: FakeLan,
    slicer: FakeSlicer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#4551: the run's plan reads its trays with the spool overlay the dialog lays over
    them, so its low-filament warnings judge the provider's grams (`filaments.check`),
    and it still asks Bambuddy nothing. The fake model's slots weigh nothing, so what is
    checked is the overlay each plate's trays are gathered with."""
    planning = False
    gathered: list[Overlay | None] = []
    plan = print_run.plan_run
    gather = gather_plate_options

    async def plan_spy(*args: Any, **kwargs: Any) -> Any:
        nonlocal planning
        planning = True
        try:
            return await plan(*args, **kwargs)
        finally:
            planning = False

    async def gather_spy(*args: Any, **kwargs: Any) -> Any:
        if planning:
            gathered.append(kwargs.get("overlay"))
        return await gather(*args, **kwargs)

    monkeypatch.setattr(print_activities, "plan_run", plan_spy)
    monkeypatch.setattr(print_run, "gather_plate_options", gather_spy)
    row = lan_row(native_app, fake_lan)
    overlay(native_app, remaining_g=0.5)

    with forbid_bambuddy(spans, BASE):
        _, _, filaments, run = _dialog_and_run(native_app, model, row)

    tagged = next(spool for spool in filaments["spools"] if spool["spool_id"] == TAGGED)
    assert tagged["remaining_g"] == 0.5
    assert run["status"] == "succeeded", run
    (laid,) = gathered
    assert laid is not None
    records = [record for tray in laid.trays for record in tray.records.values()]
    assert [(r.record_id, r.remaining_g) for r in records] == [("16", 0.5)]


def test_a_stale_overlay_still_prints(
    native_app: TestClient,
    model: str,
    spans: InMemorySpanExporter,
    fake_lan: FakeLan,
    slicer: FakeSlicer,
) -> None:
    """AC4b: an overlay older than `SPOOL_OVERLAY_STALE` is trays only, with a note."""
    row = lan_row(native_app, fake_lan)
    overlay(native_app, at=datetime.now(UTC) - timedelta(minutes=31))

    with forbid_bambuddy(spans, BASE):
        _, _, filaments, run = _dialog_and_run(native_app, model, row)

    assert filaments["source"] == "trays"
    assert filaments["overlay_notes"], filaments
    assert all(not spool.get("records") for spool in filaments["spools"])
    assert run["status"] == "succeeded", run
    assert is_native_id(run["result"]["library_file_id"])


def test_a_stale_report_runs_the_bambuddy_path(
    native_app: TestClient, model: str, spans: InMemorySpanExporter
) -> None:
    """Review Focus 5: a printer linked both ways, set to ``auto``, whose LAN report is
    two minutes old, prints through Bambuddy (plan 2026-10-16 Task 8's `run_path`): the
    run is not native, and the dialog's reads go to Bambuddy as they always have."""
    printers = _printers(native_app)
    row = printers.store.create(PrinterCreate(serial="01P00FAKE000002", name="Old H2C"))
    printers.store.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    printers.store.link_bambuddy(row.id, 1)
    printers.reports.put(row.id, {"gcode_state": "IDLE"}, datetime.now(UTC) - timedelta(minutes=2))
    settings = _state(native_app).settings_store.load()

    route = _call(
        native_app,
        run_route,
        settings,
        printers.store,
        printers.reports,
        printer=row.id,
        printer_id=None,
    )

    assert (route.path, route.native, route.printer_id) == ("bambuddy", False, 1)
    output = make_output(native_app, model)
    with pytest.raises(BambuddyCalledError) as called, forbid_bambuddy(spans, BASE):
        native_app.get(f"/api/v1/print/outputs/{output}/choices", params={"printer": row.id})
    assert "GET /api/v1/printers/" in str(called.value)


def test_the_first_reads_open_on_the_native_row_with_bambuddy_configured(
    native_app: TestClient, model: str, spans: InMemorySpanExporter, fake_lan: FakeLan
) -> None:
    """AC1 in the browser (#4601): the dialog's first reads name no printer, and with
    ``bambuddy_url`` set (Bambuddy answering nothing) and no Settings printer they open
    on the registry row a run reaches natively, as they do with no URL, and ask
    Bambuddy nothing. A Settings printer, the configured default, still wins; when
    Bambuddy then fails, its problem names the native row to offer instead."""
    row = lan_row(native_app, fake_lan)
    output = make_output(native_app, model)
    assert _state(native_app).settings_store.load().printer_id is None

    with forbid_bambuddy(spans, BASE):
        choices = native_app.get(f"/api/v1/print/outputs/{output}/choices")
        assert choices.status_code == 200, choices.text
        filaments = native_app.get(f"/api/v1/print/outputs/{output}/filaments")
        assert filaments.status_code == 200, filaments.text

    assert (choices.json()["printer_id"], choices.json()["lan_printers"]) == (None, [row])
    assert filaments.json()["source"] == "trays"

    # Settings' printer, which no row links, is Bambuddy's: its reads go there. Bambuddy
    # failing, the problem names the row a run reaches natively, which the dialog then
    # offers; asked for, it opens with no Bambuddy call.
    configure(native_app, printer_id=7)
    with pytest.raises(BambuddyCalledError), forbid_bambuddy(spans, BASE):
        failed = native_app.get(f"/api/v1/print/outputs/{output}/choices")
    assert failed.status_code == 502, failed.text
    assert failed.json()["lan_printers"] == [row]
    with forbid_bambuddy(spans, BASE):
        picked = native_app.get(f"/api/v1/print/outputs/{output}/choices", params={"printer": row})
    assert picked.status_code == 200, picked.text
    assert picked.json()["lan_printers"] == [row]


def test_the_dialogs_analysis_on_the_native_row_asks_bambuddy_nothing(
    native_app: TestClient, model: str, spans: InMemorySpanExporter, fake_lan: FakeLan
) -> None:
    """The dialog's analyzers are on AC1's path (#4629): with ``bambuddy_url`` set and
    Bambuddy answering nothing, a run for the native row reads its printer from the
    registry, its trays from the LAN report and its spools from the overlay."""
    row = lan_row(native_app, fake_lan)
    overlay(native_app)
    output = make_output(native_app, model)
    filaments = native_app.get(f"/api/v1/print/outputs/{output}/filaments", params={"printer": row})
    assert filaments.status_code == 200, filaments.text
    body = run_body(row, filaments.json())

    with forbid_bambuddy(spans, BASE):
        report = native_app.post(
            "/api/v1/analyzers/run",
            json={"target": {"output_id": output}, "request": body, "detail": "advanced"},
        )
    assert report.status_code == 200, report.text
    inputs = {row["name"]: row for row in report.json()["inputs"]}
    for name in ("printer", "filaments", "inventory"):
        assert inputs[name]["available"], inputs[name]


def test_a_quote_for_the_native_row_asks_bambuddy_nothing(
    native_app: TestClient,
    model: str,
    spans: InMemorySpanExporter,
    fake_lan: FakeLan,
    slicer: FakeSlicer,
) -> None:
    """#4551: a slice quote names the registry `printer`, as the dialog's run does, and
    for a printer a run reaches over the LAN its background slices build no Bambuddy
    client, with ``bambuddy_url`` set."""
    row = lan_row(native_app, fake_lan)
    overlay(native_app)
    output = make_output(native_app, model)
    filaments = native_app.get(f"/api/v1/print/outputs/{output}/filaments", params={"printer": row})
    assert filaments.status_code == 200, filaments.text
    dialog = {k: v for k, v in run_body(row, filaments.json()).items() if k != "copies"}
    pla, petg = price_book(native_app)
    order = create_order(native_app, create_customer(native_app)["id"])
    item = add_item(native_app, order["id"], output_id=output, quantity=1)
    slicer.sliced = sliced_plate(1, 3600, [12.5, 3.0])

    with forbid_bambuddy(spans, BASE):
        started = native_app.post(
            f"/api/v1/orders/{order['id']}/items/{item['id']}/quote",
            json={**dialog, "filaments": {"0": pla, "1": petg}},
            headers=press(),
        )
        assert started.status_code == 202, started.text
        op = follow_operation(native_app, started.json(), timeout=120, poll=0.2)

    assert op["status"] == "succeeded", op
    assert slicer.calls == 1


def test_a_quote_naming_a_disagreeing_printer_is_refused_before_it_starts(
    native_app: TestClient, model: str, fake_lan: FakeLan
) -> None:
    """#4551 review: a registry `printer` the run would refuse beside `printer_id` is the
    quote's check's 422, as Arrange's, never a failed operation after the 202."""
    row = lan_row(native_app, fake_lan)
    output = make_output(native_app, model)
    filaments = native_app.get(f"/api/v1/print/outputs/{output}/filaments", params={"printer": row})
    assert filaments.status_code == 200, filaments.text
    dialog = {k: v for k, v in run_body(row, filaments.json()).items() if k != "copies"}
    pla, petg = price_book(native_app)
    order = create_order(native_app, create_customer(native_app)["id"])
    item = add_item(native_app, order["id"], output_id=output, quantity=1)

    refused = native_app.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/quote",
        json={**dialog, "printer_id": 1, "filaments": {"0": pla, "1": petg}},
        headers=press(),
    )

    assert refused.status_code == 422, refused.text
    assert "disagreeing printer" in refused.json()["detail"]


def test_the_native_analysis_names_a_plan_spool_the_trays_do_not_hold(
    native_app: TestClient, model: str, spans: InMemorySpanExporter, fake_lan: FakeLan
) -> None:
    """#4634 review: a plan slot whose spool is in no tray is named, not dropped."""
    row = lan_row(native_app, fake_lan)
    overlay(native_app)
    output = make_output(native_app, model)
    filaments = native_app.get(f"/api/v1/print/outputs/{output}/filaments", params={"printer": row})
    assert filaments.status_code == 200, filaments.text
    body = run_body(row, filaments.json())
    gone = tray_spool_id(3, 3)
    body["filament_plan"]["slots"][0]["spool_id"] = gone
    slot = body["filament_plan"]["slots"][0]["slot_id"]

    with forbid_bambuddy(spans, BASE):
        report = native_app.post(
            "/api/v1/analyzers/run",
            json={"target": {"output_id": output}, "request": body, "detail": "advanced"},
        )
    assert report.status_code == 200, report.text
    inputs = {row["name"]: row for row in report.json()["inputs"]}
    assert not inputs["filaments"]["available"]
    assert inputs["filaments"]["reason"] == (
        f"slot {slot}'s spool {gone} is not in the printer's trays"
    )


@pytest.mark.parametrize("native_app", [False], indirect=True, ids=["no-url"])
def test_an_analysis_naming_a_printer_the_registry_refuses_goes_on_without_it(
    native_app: TestClient, model: str, fake_lan: FakeLan
) -> None:
    """#4634 review: a named row that is gone is the printer's reason, and the analysis
    still answers on the rest."""
    lan_row(native_app, fake_lan)
    output = make_output(native_app, model)
    report = native_app.post(
        "/api/v1/analyzers/run",
        json={
            "target": {"output_id": output},
            "request": {"printer": "no-such-row"},
            "detail": "advanced",
        },
    )
    assert report.status_code == 200, report.text
    inputs = {row["name"]: row for row in report.json()["inputs"]}
    assert not inputs["printer"]["available"]
    assert "There is no printer no-such-row in the registry" in inputs["printer"]["reason"]
    assert inputs["geometry"]["available"], inputs["geometry"]


#: The app with no ``bambuddy_url`` (AC2), for `native_app`'s indirect parametrisation.
NO_BAMBUDDY = pytest.mark.parametrize("native_app", [False], indirect=True, ids=["no-url"])


def _library_dialog_and_run(client: TestClient, row: str, file_id: int) -> dict[str, Any]:
    """The library-file dialog's reads and run on ``file_id``, a native library file,
    for the registry row ``row``; the run's end."""
    base = f"/api/v1/print/library/{file_id}"
    choices = client.get(f"{base}/choices", params={"printer": row})
    assert choices.status_code == 200, choices.text
    assert row in choices.json()["lan_printers"]
    filaments = client.get(f"{base}/filaments", params={"printer": row})
    assert filaments.status_code == 200, filaments.text
    assert filaments.json()["source"] == "trays"
    body = run_body(row, filaments.json())
    checked = client.post(f"{base}/check", json=body)
    assert checked.status_code == 200, checked.text
    assert checked.json()["errors"] == [], checked.json()
    started = client.post(f"{base}/run", json=body, headers=press())
    assert started.status_code == 202, started.text
    run = follow_run(client, started.json()["id"], timeout=120)
    progress = client.get(f"{base}/progress")
    assert progress.status_code == 200, progress.text
    return run


@NO_BAMBUDDY
@pytest.mark.parametrize("bambuddy_printer_id", [None, 1], ids=["lan-only", "both-links"])
def test_a_native_run_without_bambuddy(
    native_app: TestClient,
    model: str,
    spans: InMemorySpanExporter,
    fake_lan: FakeLan,
    slicer: FakeSlicer,
    bambuddy_printer_id: int | None,
) -> None:
    """AC2: no ``bambuddy_url``, and the output's dialog, its background slice, the run,
    the follow, the printers strip and the native destinations all answer; then the
    run's copy, a native library file, is opened and printed from the library dialog.
    A client built anywhere on the way is `client_for`'s 409, which fails the read or
    the run. A row that also links a Bambuddy printer is native all the same."""
    row = lan_row(native_app, fake_lan, bambuddy_printer_id=bambuddy_printer_id)
    overlay(native_app)

    with forbid_bambuddy(spans, BASE):
        output, choices, filaments, run = _dialog_and_run(native_app, model, row)
        assert run["status"] == "succeeded", run
        progress = follow_until_settled(native_app, output)
        now = native_app.get("/api/v1/print/printers/now")
        assert now.status_code == 200, now.text
        projects = native_app.get("/api/v1/print/projects", params={"native": "true"})
        assert projects.status_code == 200, projects.text
        library_run = _library_dialog_and_run(native_app, row, run["result"]["library_file_id"])

    assert (progress["route"], progress["stage"]) == ("lan", "done"), progress
    assert row in choices["lan_printers"]
    assert filaments["source"] == "trays"
    assert run["result"]["path"] == "native"
    assert library_run["status"] == "succeeded", library_run
    assert library_run["result"]["path"] == "native"


@NO_BAMBUDDY
def test_the_dialog_reads_answer_without_bambuddy(
    native_app: TestClient, model: str, fake_lan: FakeLan
) -> None:
    """AC2: with no ``bambuddy_url``, an output never printed has no progress (not a
    409); the dialog's first reads, which name no printer, open on the registry row a
    run reaches natively (`opening_route`); and a check naming no printer has nothing
    to judge."""
    row = lan_row(native_app, fake_lan)
    output = make_output(native_app, model)
    progress = native_app.get(f"/api/v1/print/outputs/{output}/progress")
    assert progress.status_code == 200, progress.text
    assert progress.json() is None
    choices = native_app.get(f"/api/v1/print/outputs/{output}/choices")
    assert choices.status_code == 200, choices.text
    assert (choices.json()["printer_id"], choices.json()["lan_printers"]) == (None, [row])
    filaments = native_app.get(f"/api/v1/print/outputs/{output}/filaments")
    assert filaments.status_code == 200, filaments.text
    assert filaments.json()["source"] == "trays"
    # No Bambuddy, so no note that its spool records are missing (#4601).
    assert filaments.json()["overlay_notes"] == []
    # The check judges the body alone; one naming no printer has nothing to judge.
    checked = native_app.post(
        f"/api/v1/print/outputs/{output}/check",
        json={
            "copies": 1,
            "filament_plan": {"slots": []},
            "choices": {"nozzles": [{"size": "0.4"}], "tier": "standard"},
        },
    )
    assert checked.status_code == 200, checked.text


@NO_BAMBUDDY
def test_a_library_check_naming_no_printer_answers_without_bambuddy(
    native_app: TestClient, tmp_path: Path
) -> None:
    """AC2: a native library file's check that names no printer judges nothing, and
    reads the file from the native library with no client (`post_library_check`)."""
    plug = _upload(native_app, _one_colour(tmp_path / "plug.3mf"))
    checked = native_app.post(
        f"/api/v1/print/library/{plug.id}/check",
        json={
            "copies": 1,
            "filament_plan": {"slots": []},
            "choices": {"nozzles": [{"size": "0.4"}], "tier": "standard"},
        },
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["errors"] == [], checked.json()


@NO_BAMBUDDY
@pytest.mark.parametrize(
    "path",
    ["/api/v1/print/printers/1/camera", "/api/v1/farm/inventory", "/api/v1/print/projects"],
    ids=["camera", "inventory", "projects"],
)
def test_bambuddy_only_routes_still_say_why(native_app: TestClient, path: str) -> None:
    """A route that is Bambuddy's by nature keeps `client_for`'s 409 naming Settings."""
    response = native_app.get(path)
    assert response.status_code == 409, response.text
    assert "set one in Settings" in response.json()["detail"]
