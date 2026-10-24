"""``PrintRun``'s native LAN branch (#3823, plan 2026-10-16 Task 9): a plate planned on
the native path is uploaded to the printer and started with ``print.project_file``,
never queued in Bambuddy. ``PrintRun`` runs on a dev server with the fake activities of
`tests/test_print_workflow.py` and the real LAN activities, against the fake printer
(`tests/support/fake_lan.py`), which moves a started upload to ``/cache`` as the H2C
does (Task 7)."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import AsyncGenerator, AsyncIterator, Iterable
from pathlib import Path
from typing import Any, cast

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import RackAlgorithm, SlotChoice
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_run import PlannedRun, PlatePlan, PrintRunRequest
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices
from scadbuddy.bambuddy.runs import PrintRun
from scadbuddy.bambuddy.trays import tray_spool_id
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.print_library.ids import NATIVE_ID_BASE
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.print_activities import LanActivities, LanDeps
from scadbuddy.printers.store import PrinterStore
from scadbuddy.workflows.follow import FollowPrint, follow_queue
from scadbuddy.workflows.print_models import (
    LAN_PRINT_ACTIVITY,
    LAN_UNSURE,
    SLICE_RUN_ACTIVITY,
    SLICED_UPLOAD_ACTIVITY,
    LanPrintInput,
    LanUploaded,
    NativeSliced,
    PlanInput,
    PrintRunInput,
    SlicedUploadInput,
    SliceRunInput,
    SourceSpec,
)
from scadbuddy.workflows.printing import PrintRunWorkflow
from tests.conftest import PgPool
from tests.printers.conftest import KEK
from tests.support.fake_lan import FakeLan, needs_fake_lan, running_fake_lan
from tests.support.temporal import temporal_client
from tests.test_print_workflow import PLAN, Fake, ended, start

pytestmark = [
    pytest.mark.requires_temporal,
    pytest.mark.requires_postgres,
    *needs_fake_lan,
]

BLOB = "slice-" + "a" * 64
#: A native library file: read from ScadBuddy's own library, never through Bambuddy.
NATIVE_SLICED = NATIVE_ID_BASE + 7
#: Slot 1 from AMS 0 tray 1, slot 2 from the AMS-HT (the fake's ``report.json``).
TRAYS = FilamentPlan(
    slots=[
        SlotChoice(slot_id=1, spool_id=tray_spool_id(0, 1)),
        SlotChoice(slot_id=2, spool_id=tray_spool_id(128, 0)),
    ]
)


def sliced_3mf() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(
            "Metadata/project_settings.config",
            json.dumps({"filament_colour": ["#FF0000", "#00FF00"], "filament_type": ["PLA"]}),
        )
        archive.writestr("Metadata/plate_1.gcode", "; G-code\n" * 2000)
    return out.getvalue()


SLICED = sliced_3mf()


class Blobs:
    def __init__(self, payload: bytes = SLICED) -> None:
        self.read: list[str] = []
        self.payload = payload

    async def stream(self, key: str) -> AsyncGenerator[bytes]:
        self.read.append(key)
        for at in range(0, len(self.payload), 4096):
            yield self.payload[at : at + 4096]


PRINTER_FIXTURES = Path(__file__).parent / "printers" / "fixtures"
#: Bambuddy's print_queue item 370 on the live H2C (``rack-project-file.json``).
MEASURED = json.loads((PRINTER_FIXTURES / "rack-project-file.json").read_text(encoding="utf-8"))
#: Its trays, from its ``ams_mapping`` [4, 1, 6, 12, 5, 13] (``ams_id * 4 + tray``).
RACK_TRAYS = FilamentPlan(
    slots=[
        SlotChoice(slot_id=slot, spool_id=tray_spool_id(tray // 4, tray % 4))
        for slot, tray in enumerate(MEASURED["plate"]["ams_mapping"], start=1)
    ]
)


def rack_3mf() -> bytes:
    """Item 370's plate as the slicer wrote it: its ``slice_info.config`` and the H2C
    preset's rack keys."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(
            "Metadata/project_settings.config",
            json.dumps(
                {
                    "filament_colour": [
                        "#482960",
                        "#FC97B0",
                        "#FFFFFF",
                        "#8344B0",
                        "#FEC600",
                        "#5E43B7",
                    ],
                    "filament_type": ["PLA"] * 6,
                    "extruder_max_nozzle_count": ["1", "6"],
                    "physical_extruder_map": ["1", "0"],
                }
            ),
        )
        archive.writestr(
            "Metadata/slice_info.config",
            (PRINTER_FIXTURES / "rack-slice-info-h2c.config").read_text(encoding="utf-8"),
        )
        archive.writestr("Metadata/plate_1.gcode", "; G-code\n" * 2000)
    return out.getvalue()


def as_the_h2c(fake_lan: FakeLan) -> dict[str, Any]:
    """The fake printer reporting the live H2C's AMS and rack of that day
    (``lan-report-h2c-idle.json``); its ``device`` block, to change."""
    recorded = json.loads((PRINTER_FIXTURES / "lan-report-h2c-idle.json").read_text("utf-8"))
    report = recorded["print"]
    fake_lan.printer.state.update(device=report["device"], ams=report["ams"])
    device: dict[str, Any] = report["device"]
    return device


class Usage:
    """Rack usage: no history, and what it was asked."""

    def __init__(self, *, linked: bool = False) -> None:
        self.asked: list[list[str]] = []
        self.linked = linked
        self.seen_by: list[int] = []

    async def usage(self, serials: Iterable[str]) -> dict[str, object]:
        self.asked.append(list(serials))
        return {}

    async def seen(self, printer_id: int, serials: Iterable[str]) -> None:
        if not self.linked:
            raise AssertionError("a LAN-only printer has no Bambuddy id to record it under")
        self.seen_by.append(printer_id)


class Library:
    """ScadBuddy's own library, as the LAN upload reads a native file."""

    def __init__(self) -> None:
        self.read: list[int] = []

    async def download_library_file(self, file_id: int) -> AsyncGenerator[bytes]:
        self.read.append(file_id)
        yield SLICED


class NativeFake(Fake):
    """`Fake`, planning a native run on the native slicer to the registry printer."""

    def __init__(self, printer: str, queue: str) -> None:
        super().__init__()
        self.printer = printer
        self.queue = queue
        self.native_slicer = True
        #: The check's own mark; a run checked by an older build has none.
        self.lan_start = True
        self.lan_prints = 0
        #: Fail the next real `lan_print` attempts as unsure, after the real one ran.
        self.unsure_after_start = 0
        self.rack_position: int | None = None
        self.rack_algorithm: RackAlgorithm = "least_used"

    @activity.defn(name="print_plan")
    async def plan(self, input: PlanInput) -> PlannedRun:
        self.calls.append("plan")
        return PlannedRun(
            library_file_id=41,
            printer_id=1,
            nozzle_size="0.4",
            copies=1,
            options=PrintOptions(),
            plates=[PlatePlan(plate_id=1, plan=PLAN)],
            slicer="native" if self.native_slicer else "bambuddy",
            library_queue=self.queue,
            printer=self.printer,
            path="native",
            lan_start=self.lan_start,
            rack_algorithm=self.rack_algorithm,
            rack_position=self.rack_position,
            has_rack=True,
        )

    @activity.defn(name=SLICE_RUN_ACTIVITY)
    async def slice_run(self, input: SliceRunInput) -> NativeSliced:
        self.calls.append("slice_run")
        return NativeSliced(slice_id=5, preset_key="k", sliced_blob_key=BLOB)

    @activity.defn(name=SLICED_UPLOAD_ACTIVITY)
    async def sliced_upload(self, input: SlicedUploadInput) -> int:
        self.calls.append("sliced_upload")
        return 77

    @activity.defn(name="print_slice_wait")
    async def slice_wait(self, job_id: int) -> int:
        self.calls.append(f"slice_wait:{job_id}")
        return NATIVE_SLICED

    def all(self) -> list:  # type: ignore[type-arg]
        return [*super().all(), self.slice_run, self.sliced_upload]


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.fixture
async def fake_lan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeLan]:
    async with running_fake_lan(tmp_path, monkeypatch) as lan:
        yield lan


@pytest.fixture
def registry_native(pg_pool: PgPool, fake_lan: FakeLan) -> str:
    """A registry row linked to the fake printer over the LAN."""
    printers = PrinterStore(pg_pool, KEK)
    row = printers.create(PrinterCreate(serial=fake_lan.serial, name="Fake H2C", model="H2C"))
    printers.link_lan(
        row.id,
        LanLink(host=fake_lan.host, access_code=fake_lan.access_code),
        cert_sha256=fake_lan.cert_sha256,
    )
    return row.id


class Unsure(LanActivities):
    """The real ``lan_print``, answered ``LAN_UNSURE`` for its first ``unsure`` starts
    *after* each ran: a start the printer took whose answer was lost."""

    def __init__(self, deps: LanDeps, unsure: int) -> None:
        super().__init__(deps)
        self.unsure = unsure
        self.calls = 0

    @activity.defn(name=LAN_PRINT_ACTIVITY)
    async def lan_print(self, input: LanPrintInput) -> object:  # type: ignore[override]
        self.calls += 1
        started = await super().lan_print(input)
        if self.calls <= self.unsure:
            raise ApplicationError("the answer was lost", type=LAN_UNSURE, non_retryable=True)
        return started


async def _run(
    client: Client,
    pg_pool: PgPool,
    printer: str,
    *,
    blobs: Blobs | None = None,
    library: Library | None = None,
    native_slicer: bool = True,
    unsure: int = 0,
    plan: FilamentPlan = TRAYS,
    lan_start: bool = True,
    rack: Usage | None = None,
    rack_position: int | None = None,
    rack_algorithm: RackAlgorithm = "least_used",
) -> tuple[NativeFake, PrintRun]:
    queue = f"print-{uuid.uuid4().hex[:8]}"
    fake = NativeFake(printer, queue)
    fake.native_slicer = native_slicer
    fake.lan_start = lan_start
    fake.rack_position = rack_position
    fake.rack_algorithm = rack_algorithm
    deps = LanDeps(
        printers=PrinterStore(pg_pool, KEK),
        # Never read: a native file and a blob need no Bambuddy client.
        settings_store=cast(SettingsStore, None),
        library=library,  # type: ignore[arg-type]
        blobs=blobs,
        start_wait=10.0,
        rack=rack,  # type: ignore[arg-type]
    )
    lan = Unsure(deps, unsure)
    arg = PrintRunInput(
        subject="o" * 32,
        slug="demo",
        key=uuid.uuid4().hex,
        source=SourceSpec(kind="output", output_id="o" * 32),
        repeat_window_s=0,
        request=PrintRunRequest(
            filament_plan=plan, choices=PrintChoices(nozzles=[NozzleChoice(size="0.4")])
        ),
    )
    async with (
        Worker(
            client,
            task_queue=queue,
            workflows=[PrintRunWorkflow, FollowPrint],
            activities=[*fake.all(), lan.lan_upload, lan.lan_print],
        ),
        Worker(client, task_queue=follow_queue(queue), activities=[fake.follow_print]),
    ):
        await start(client, queue, arg)
        run = await ended(client, arg)
    return fake, run


async def test_native_run_uploads_and_starts(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    blobs = Blobs()
    fake, run = await _run(client, pg_pool, registry_native, blobs=blobs)
    assert run.status == "succeeded"
    assert "enqueue:1" not in "".join(fake.calls)
    [outcome] = fake.outcomes
    assert isinstance(outcome, QueueOutcome)
    assert outcome.queue_item_ids == [] and outcome.lan is not None
    assert outcome.sliced_blob_key == BLOB and blobs.read == [BLOB]
    stem = f"scadbuddy-{'run-1'[:8]}-p1"
    assert outcome.lan.subtask_name == stem
    [upload] = fake_lan.received()["ftps"]
    assert upload["name"] == f"/{stem}.3mf" and upload["size"] == len(SLICED)
    [sent] = fake_lan.project_files()
    assert sent["url"] == f"ftp://{stem}.3mf"
    assert sent["param"] == "Metadata/plate_1.gcode"
    assert sent["subtask_name"] == stem and sent["md5"] == upload["md5"]
    assert sent["ams_mapping"] == [1, 128] and sent["use_ams"] is True
    assert "nozzle_mapping" not in sent and "ams_mapping2" not in sent
    assert [sent[k] for k in ("project_id", "profile_id", "task_id", "subtask_id")] == ["0"] * 4
    # The printer moved the started upload away, as the H2C does.
    assert (fake_lan.sdcard / "cache" / f"{stem}.3mf.gcode.3mf").is_file()


async def test_a_native_file_is_read_from_scadbuddys_library(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    library = Library()
    _, run = await _run(client, pg_pool, registry_native, library=library, native_slicer=False)
    assert run.status == "succeeded"
    assert library.read == [NATIVE_SLICED]


async def test_a_retried_start_uploads_again_under_a_new_name(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """The first start was taken but its answer lost: the retry uploads again under a
    new name (the first upload is in ``/cache`` now) and, finding the plate in the
    printer's report, sends no second ``project_file``."""
    fake, run = await _run(client, pg_pool, registry_native, blobs=Blobs(), unsure=1)
    assert run.status == "succeeded"
    stem = "scadbuddy-run-1-p1"
    names = [entry["name"] for entry in fake_lan.received()["ftps"]]
    assert names == [f"/{stem}.3mf", f"/{stem}-2.3mf"]
    assert len(fake_lan.project_files()) == 1
    [outcome] = fake.outcomes
    assert outcome.lan is not None and outcome.lan.subtask_name == stem


async def test_native_run_records_print_error(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """A start the printer refuses (here: a file it no longer has) fails the run with
    the printer's code, once: no second start."""
    fake_lan.printer._missing = lambda path: "the file is gone"
    fake, run = await _run(client, pg_pool, registry_native, blobs=Blobs())
    assert run.status == "failed"
    error = run.error
    assert error is not None and "refused the print" in error.detail
    assert len(fake_lan.project_files()) == 1
    assert fake.outcomes == []


async def test_a_plan_with_an_inventory_spool_fails_before_the_start(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    plan = FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=12)])
    _, run = await _run(client, pg_pool, registry_native, blobs=Blobs(), plan=plan)
    assert run.status == "failed"
    assert run.error is not None and "trays" in run.error.detail
    assert fake_lan.project_files() == []


async def test_a_native_path_planned_without_the_checks_mark_queues_in_bambuddy(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """#4540 review: a run checked by a build before the LAN branch could plan
    ``path="native"`` past no gate (several plates, inventory spools). Upgraded mid-run,
    it stays on Bambuddy's branch: only the check's ``lan_start`` takes the LAN."""
    fake, run = await _run(client, pg_pool, registry_native, blobs=Blobs(), lan_start=False)
    assert run.status == "succeeded"
    assert "enqueue:1" in fake.calls
    assert fake_lan.received()["ftps"] == [] and fake_lan.project_files() == []
    assert [outcome.lan for outcome in fake.outcomes] == [None]


async def test_bambuddy_path_is_unchanged(client: Client) -> None:
    """A run planned on Bambuddy's path queues there, and never asks for the patch."""
    queue = f"print-{uuid.uuid4().hex[:8]}"
    fake = Fake()
    arg = PrintRunInput(
        subject="o" * 32,
        slug="demo",
        key=uuid.uuid4().hex,
        source=SourceSpec(kind="output", output_id="o" * 32),
        repeat_window_s=0,
        request=PrintRunRequest(
            filament_plan=FilamentPlan(slots=[]),
            choices=PrintChoices(nozzles=[NozzleChoice(size="0.4")]),
        ),
    )
    async with (
        Worker(
            client,
            task_queue=queue,
            workflows=[PrintRunWorkflow, FollowPrint],
            activities=fake.all(),
        ),
        Worker(client, task_queue=follow_queue(queue), activities=[fake.follow_print]),
    ):
        await start(client, queue, arg)
        run = await ended(client, arg)
        history = await client.get_workflow_handle(f"print-{arg.key}").fetch_history()
    assert run.status == "succeeded" and "enqueue:1" in fake.calls
    assert "native-print" not in history.to_json()


async def test_a_rack_print_sends_its_nozzle_mapping(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """Item 370's plate on the H2C's rack as its report shows it: the pick, ranked from
    the report (no Bambuddy client exists here), is sent as ``nozzle_mapping``. Each
    group's colour is loaded on one eligible hotend but group 4's, so the pick is
    Bambuddy's own: ``[21, 18, 20, 17, 1, 17, -1 x26]``."""
    as_the_h2c(fake_lan)
    usage = Usage()
    _, run = await _run(
        client, pg_pool, registry_native, blobs=Blobs(rack_3mf()), plan=RACK_TRAYS, rack=usage
    )
    assert run.status == "succeeded"
    [sent] = fake_lan.project_files()
    assert sent["ams_mapping"] == MEASURED["plate"]["ams_mapping"]
    assert sent["nozzle_mapping"] == MEASURED["plate"]["nozzle_mapping"]
    # Ranked over the hotends the report lists (the carriage's fills position 4).
    assert [len(serials) for serials in usage.asked] == [6]


async def test_the_rack_pick_reads_the_printers_report(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """Swap the filaments the report says hotends 17 and 21 last ran: the two Standard
    groups swap hotends with them."""
    device = as_the_h2c(fake_lan)
    info = {entry["id"]: entry for entry in device["nozzle"]["info"]}
    info[17]["color_m"], info[21]["color_m"] = info[21]["color_m"], info[17]["color_m"]
    _, run = await _run(client, pg_pool, registry_native, blobs=Blobs(rack_3mf()), plan=RACK_TRAYS)
    assert run.status == "succeeded"
    [sent] = fake_lan.project_files()
    assert sent["nozzle_mapping"] == [17, 18, 20, 21, 1, 21] + [-1] * 26


async def test_a_rack_pick_that_cannot_be_made_lets_the_printer_pick(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """No 0.4 Standard hotend left on the rack: no ``nozzle_mapping``, never a guess."""
    device = as_the_h2c(fake_lan)
    nozzle = device["nozzle"]
    nozzle["info"] = [e for e in nozzle["info"] if e["id"] not in (17, 21)]
    fake, run = await _run(
        client, pg_pool, registry_native, blobs=Blobs(rack_3mf()), plan=RACK_TRAYS
    )
    assert run.status == "succeeded"
    [sent] = fake_lan.project_files()
    assert "nozzle_mapping" not in sent
    # The run says the printer picked, and never what the pick it dropped chose (#4545).
    [outcome] = fake.outcomes
    assert outcome.lan is not None
    assert [w.message for w in outcome.lan.warnings] == [
        "Rack pick left to the printer: ScadBuddy could not pick a hotend for every rack group."
    ]


async def test_a_hand_picked_rack_position_is_sent(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """The dialog's one rack-side position goes to the lowest rack group (``manual_for``):
    group 1 on position 6, so group 2 takes 17."""
    as_the_h2c(fake_lan)
    _, run = await _run(
        client,
        pg_pool,
        registry_native,
        blobs=Blobs(rack_3mf()),
        plan=RACK_TRAYS,
        rack_position=6,
    )
    assert run.status == "succeeded"
    [sent] = fake_lan.project_files()
    assert sent["nozzle_mapping"][:6] == [17, 18, 20, 21, 1, 21]


def test_lan_uploaded_carries_the_project_filament_count() -> None:
    assert LanUploaded(remote_name="x", md5="m", bytes=1).filament_count == 1


async def test_let_bambuddy_pick_over_the_lan_says_the_printer_picks(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """#4545: "Let Bambuddy pick" sends no ``nozzle_mapping``, and a position picked by
    hand for a plate with several rack groups is dropped with it; the run says both."""
    as_the_h2c(fake_lan)
    fake, run = await _run(
        client,
        pg_pool,
        registry_native,
        blobs=Blobs(rack_3mf()),
        plan=RACK_TRAYS,
        rack_position=6,
        rack_algorithm="bambuddy",
    )
    assert run.status == "succeeded"
    [sent] = fake_lan.project_files()
    assert "nozzle_mapping" not in sent
    [outcome] = fake.outcomes
    assert outcome.lan is not None
    messages = [w.message for w in outcome.lan.warnings]
    assert messages[0].startswith("Rack position 6 was not sent: the slice put the rack side in")
    assert not any("got position" in message for message in messages)
    assert messages[-1] == (
        "Rack pick left to the printer: Let Bambuddy pick sends no hotends over the LAN."
    )


async def test_a_sent_rack_pick_carries_no_left_to_the_printer_warning(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    as_the_h2c(fake_lan)
    fake, run = await _run(
        client, pg_pool, registry_native, blobs=Blobs(rack_3mf()), plan=RACK_TRAYS
    )
    assert run.status == "succeeded"
    [outcome] = fake.outcomes
    assert outcome.lan is not None
    assert not any(w.kind == "rack-left-to-bambuddy" for w in outcome.lan.warnings)


async def test_the_rack_is_recorded_seen_once_per_report_read(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """#4545: recorded at the report ``lan_print`` reads, not by the pick, so a plate
    that prints nothing from the rack records it too, once."""
    PrinterStore(pg_pool, KEK).link_bambuddy(registry_native, 9)
    as_the_h2c(fake_lan)
    usage = Usage(linked=True)
    _, run = await _run(client, pg_pool, registry_native, blobs=Blobs(), rack=usage)
    assert run.status == "succeeded"
    assert usage.seen_by == [9] and usage.asked == []
