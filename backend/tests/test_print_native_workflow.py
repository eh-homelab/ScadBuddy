"""``PrintRun``'s native LAN branch (#3823, plan 2026-10-16 Task 9): a plate planned on
the native path is uploaded to the printer and started with ``print.project_file``,
never queued in Bambuddy. ``PrintRun`` runs on a dev server with the fake activities of
`tests/test_print_workflow.py` and the real LAN activities, against the fake printer
(`tests/support/fake_lan.py`), which moves a started upload to ``/cache`` as the H2C
does (Task 7)."""

from __future__ import annotations

import asyncio
import io
import json
import uuid
import zipfile
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError
from temporalio.worker import Worker

from scadbuddy.bambuddy.dispatch import QueueOutcome
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import RackAlgorithm, SlotChoice
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_run import PlannedRun, PlatePlan, PrintRunRequest, PrintRunResult
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices
from scadbuddy.bambuddy.runs import PrintRun
from scadbuddy.bambuddy.trays import tray_spool_id
from scadbuddy.library.outputs import PlateSend
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.print_library.ids import NATIVE_ID_BASE
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.print_activities import LanActivities, LanDeps
from scadbuddy.printers.store import PrinterStore
from scadbuddy.workflows.follow import FollowPrint, follow_queue
from scadbuddy.workflows.print_models import (
    LAN_PRINT_ACTIVITY,
    LAN_UNSURE,
    PLATE_CLEARED_SIGNAL,
    PRINT_LAN_UPDATE_ACTIVITY,
    SLICE_RUN_ACTIVITY,
    SLICED_UPLOAD_ACTIVITY,
    FinishInput,
    LanPrintInput,
    LanUpdateInput,
    LanUploaded,
    NativeSliced,
    PlanInput,
    PrintRunInput,
    RecordInput,
    SlicedUploadInput,
    SliceRunInput,
    SourceSpec,
    lan_sequence_id,
)
from scadbuddy.workflows.printing import LanSequenceWorkflow, PrintRunWorkflow
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
        self.copies = 1
        #: Each result `print_lan_update` recorded, in order (#4541).
        self.updates: list[PrintRunResult] = []
        #: The plates whose `print_record` fails every attempt.
        self.unrecorded: set[int] = set()
        #: The blob each `slice_run` answers, in turn; `BLOB` once they run out.
        self.slice_keys: list[str] = []
        #: The plan's `lan_require_plate_clear` (#4939).
        self.plate_clear = False

    @activity.defn(name="print_record")
    async def record(self, input: RecordInput) -> list[PlateSend]:
        if input.plate_id in self.unrecorded:
            self.calls.append("record:failed")
            raise RuntimeError("the database is gone")
        return await super().record(input)

    @activity.defn(name="print_plan")
    async def plan(self, input: PlanInput) -> PlannedRun:
        self.calls.append("plan")
        return PlannedRun(
            library_file_id=41,
            printer_id=1,
            nozzle_size="0.4",
            copies=self.copies,
            options=PrintOptions(),
            plates=[PlatePlan(plate_id=plate, plan=PLAN) for plate in self.plates],
            slicer="native" if self.native_slicer else "bambuddy",
            library_queue=self.queue,
            printer=self.printer,
            path="native",
            lan_start=self.lan_start,
            rack_algorithm=self.rack_algorithm,
            rack_position=self.rack_position,
            has_rack=True,
            plate_clear=self.plate_clear,
        )

    @activity.defn(name=SLICE_RUN_ACTIVITY)
    async def slice_run(self, input: SliceRunInput) -> NativeSliced:
        self.calls.append(f"slice_run:{input.plate_id}")
        # A slice swept since the last `slice_run` is sliced again, into a new blob.
        key = self.slice_keys.pop(0) if self.slice_keys else BLOB
        return NativeSliced(slice_id=5, preset_key="k", sliced_blob_key=key)

    @activity.defn(name="print_finish")
    async def finish(self, input: FinishInput) -> PrintRunResult:
        # As `finish_run` reports a LAN run: its starts, and no Bambuddy link.
        self.calls.append("finish")
        lan = [outcome.lan for outcome in input.outcomes if outcome.lan is not None]
        return PrintRunResult(library_file_id=41, copies=self.copies, path="native", lan=lan)

    @activity.defn(name=PRINT_LAN_UPDATE_ACTIVITY)
    async def lan_update(self, input: LanUpdateInput) -> PrintRun:
        self.calls.append("lan_update")
        self.updates.append(input.result)
        return self._run("succeeded", result=input.result)

    @activity.defn(name=SLICED_UPLOAD_ACTIVITY)
    async def sliced_upload(self, input: SlicedUploadInput) -> int:
        self.calls.append("sliced_upload")
        return 77

    @activity.defn(name="print_slice_wait")
    async def slice_wait(self, job_id: int) -> int:
        self.calls.append(f"slice_wait:{job_id}")
        return NATIVE_SLICED

    def all(self) -> list:  # type: ignore[type-arg]
        return [*super().all(), self.slice_run, self.sliced_upload, self.lan_update]


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


class FakeReports:
    """The printer's latest report as its `PrinterWatch` keeps it: the fake printer's
    state, seen now."""

    def __init__(self, fake_lan: FakeLan) -> None:
        self.fake_lan = fake_lan

    def get(self, printer_id: str) -> tuple[dict[str, Any], datetime]:
        return dict(self.fake_lan.printer.state), datetime.now(UTC)


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
    plates: list[int] | None = None,
    copies: int = 1,
    unrecorded: set[int] | None = None,
    reports: FakeReports | None = None,
    during: Callable[[Client, PrintRunInput, NativeFake], Awaitable[None]] | None = None,
    gates: dict[str, asyncio.Event] | None = None,
    slice_keys: list[str] | None = None,
    plate_clear: bool = False,
) -> tuple[NativeFake, PrintRun]:
    queue = f"print-{uuid.uuid4().hex[:8]}"
    fake = NativeFake(printer, queue)
    fake.gates = gates or {}
    fake.slice_keys = list(slice_keys or [])
    fake.plates = plates or [1]
    fake.copies = copies
    fake.unrecorded = unrecorded or set()
    fake.native_slicer = native_slicer
    fake.lan_start = lan_start
    fake.rack_position = rack_position
    fake.rack_algorithm = rack_algorithm
    fake.plate_clear = plate_clear
    deps = LanDeps(
        printers=PrinterStore(pg_pool, KEK),
        # Never read: a native file and a blob need no Bambuddy client.
        settings_store=cast(SettingsStore, None),
        library=library,  # type: ignore[arg-type]
        blobs=blobs,
        start_wait=10.0,
        rack=rack,  # type: ignore[arg-type]
        reports=reports,  # type: ignore[arg-type]
        wait_poll=0.2,
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
            workflows=[PrintRunWorkflow, FollowPrint, LanSequenceWorkflow],
            activities=[*fake.all(), lan.lan_upload, lan.lan_print, lan.lan_wait],
        ),
        Worker(client, task_queue=follow_queue(queue), activities=[fake.follow_print]),
    ):
        await start(client, queue, arg)
        if during is not None:
            await during(client, arg, fake)
        run = await ended(client, arg)
        stopped = run.result is not None and run.result.lan_stopped is not None
        if len(fake.plates) * copies > 1 and run.status == "succeeded" and not stopped:
            # The later copies are the run's `PrintLanSequence`, which outlives it: its
            # last row is the run's as it ends.
            sequence = client.get_workflow_handle(lan_sequence_id(run.id), result_type=PrintRun)
            run = await sequence.result()
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
    [sent] = fake_lan.project_files()
    assert "nozzle_mapping" in sent
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


async def test_copies_start_one_after_another(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """#4541: the printer has no queue, so the run is one. The second copy starts once
    the printer has finished the first, under a name of its own; the run succeeds at the
    first start, and its result is rewritten as the second is made."""
    fake, run = await _run(
        client, pg_pool, registry_native, blobs=Blobs(), copies=2, reports=FakeReports(fake_lan)
    )
    assert run.status == "succeeded"
    stem = "scadbuddy-run-1-p1"
    assert [sent["subtask_name"] for sent in fake_lan.project_files()] == [stem, f"{stem}-c2"]
    names = [entry["name"] for entry in fake_lan.received()["ftps"]]
    assert names == [f"/{stem}.3mf", f"/{stem}-c2.3mf"]
    # Succeeded with the second copy still to come, then each step recorded.
    assert fake.calls.index("succeed") < fake.calls.index("lan_update")
    first, second = fake.updates[0], fake.updates[-1]
    assert first.lan_waiting == [1] and first.lan[0].ended == "done"
    assert [start.subtask_name for start in second.lan] == [stem, f"{stem}-c2"]
    assert [start.plate_id for start in second.lan] == [1, 1]
    assert second.lan_waiting == [] and second.lan_stopped is None
    assert run.result == second
    # One plate, recorded once, as Bambuddy's one queue item stands for its copies.
    assert len(fake.outcomes) == 1


async def test_a_later_copy_whose_slice_was_swept_is_sliced_again(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """Review of #4940: days may pass before a later copy, and the slice's row (with its
    hold on the blob) is swept hours after it finished. Each later copy joins its plate's
    slice again, which slices it again once it is gone, and uploads that blob."""
    swept = "slice-" + "b" * 64
    blobs = Blobs()
    fake, run = await _run(
        client,
        pg_pool,
        registry_native,
        blobs=blobs,
        copies=2,
        reports=FakeReports(fake_lan),
        slice_keys=[BLOB, swept],
    )
    assert run.status == "succeeded"
    result = run.result
    assert result is not None and result.lan_stopped is None and len(result.lan) == 2
    assert fake.calls.count("slice_run:1") == 2
    assert blobs.read[-1] == swept
    assert len(fake_lan.project_files()) == 2


async def test_every_plate_is_sliced_before_the_first_starts(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    fake, run = await _run(
        client,
        pg_pool,
        registry_native,
        blobs=Blobs(),
        plates=[1, 2],
        reports=FakeReports(fake_lan),
    )
    assert run.status == "succeeded"
    assert fake.calls.index("slice_run:2") < fake.calls.index("record")
    params = [sent["param"] for sent in fake_lan.project_files()]
    assert params == ["Metadata/plate_1.gcode", "Metadata/plate_2.gcode"]
    assert [outcome.lan.subtask_name for outcome in fake.outcomes if outcome.lan] == [
        "scadbuddy-run-1-p1",
        "scadbuddy-run-1-p2",
    ]
    assert run.result is not None and [s.plate_id for s in run.result.lan] == [1, 2]


async def test_a_later_start_that_fails_stops_the_rest(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """The run has succeeded by then, so it stays so; the starts it never made say why."""
    printer = fake_lan.printer
    missing = printer._missing
    printer._missing = lambda path: "the file is gone" if "-c2" in str(path) else missing(path)
    _, run = await _run(
        client, pg_pool, registry_native, blobs=Blobs(), copies=3, reports=FakeReports(fake_lan)
    )
    assert run.status == "succeeded"
    assert len(fake_lan.project_files()) == 2
    result = run.result
    assert result is not None and result.lan_waiting == [1, 1]
    stopped = result.lan_stopped
    assert stopped is not None and stopped.stage == "failed"
    assert stopped.message.startswith("Plate 1 was not started, nor the plates after it:")
    assert "refused the print" in stopped.message


async def _cancel_while_it_waits(client: Client, arg: PrintRunInput, fake: NativeFake) -> None:
    while "succeed" not in fake.calls:
        await asyncio.sleep(0.1)
    await asyncio.sleep(1.0)
    await client.get_workflow_handle(lan_sequence_id("run-1")).cancel()


async def test_a_cancel_while_it_waits_stops_the_rest(
    client: Client, pg_pool: PgPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_fake_lan(tmp_path, monkeypatch, print_seconds=60.0) as slow:
        printers = PrinterStore(pg_pool, KEK)
        row = printers.create(PrinterCreate(serial=slow.serial, name="Fake H2C", model="H2C"))
        printers.link_lan(
            row.id,
            LanLink(host=slow.host, access_code=slow.access_code),
            cert_sha256=slow.cert_sha256,
        )
        _, run = await _run(
            client,
            pg_pool,
            row.id,
            blobs=Blobs(),
            copies=2,
            reports=FakeReports(slow),
            during=_cancel_while_it_waits,
        )
        assert len(slow.project_files()) == 1
    assert run.status == "succeeded"
    result = run.result
    assert result is not None and result.lan_waiting == [1]
    assert result.lan_stopped is not None and result.lan_stopped.stage == "cancelled"


async def test_a_cancel_while_it_succeeds_stops_the_rest(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """Review of #4940: a cancel ``print_succeed`` absorbs starts no sequence, and the
    run's result says the waiting copies were stopped, so they never read as queued."""
    gate = asyncio.Event()

    async def cancel_while_it_succeeds(
        client: Client, arg: PrintRunInput, fake: NativeFake
    ) -> None:
        while "succeed" not in fake.calls:
            await asyncio.sleep(0.1)
        await client.get_workflow_handle(f"print-{arg.key}").cancel()
        await asyncio.sleep(1.0)
        gate.set()

    fake, run = await _run(
        client,
        pg_pool,
        registry_native,
        blobs=Blobs(),
        copies=2,
        reports=FakeReports(fake_lan),
        during=cancel_while_it_succeeds,
        gates={"succeed": gate},
    )
    assert run.status == "succeeded" and len(fake_lan.project_files()) == 1
    result = run.result
    assert result is not None and result.lan_waiting == [1]
    assert result.lan_stopped is not None and result.lan_stopped.stage == "cancelled"
    assert fake.updates[-1].lan_stopped == result.lan_stopped
    with pytest.raises(RPCError):
        await client.get_workflow_handle(lan_sequence_id(run.id)).describe()


async def test_a_started_copy_whose_record_fails_is_still_followed(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """Review of #4940: the start is on the run before its record, so a record that fails
    stops only the plates after it, and the one printing is still read as printing."""
    fake, run = await _run(
        client,
        pg_pool,
        registry_native,
        blobs=Blobs(),
        plates=[1, 2, 3],
        unrecorded={2},
        reports=FakeReports(fake_lan),
    )
    assert run.status == "succeeded" and "record:failed" in fake.calls
    result = run.result
    assert result is not None
    assert [start.plate_id for start in result.lan] == [1, 2] and result.lan_waiting == [3]
    stopped = result.lan_stopped
    assert stopped is not None and stopped.stage == "failed"
    assert stopped.message.startswith(
        "Plate 2 is printing, but it could not be recorded, so plate 3 and the plates after"
    )
    assert len(fake_lan.project_files()) == 2


async def _awaiting_clear(fake: NativeFake, count: int = 1) -> None:
    """Until the run has recorded its ``count``-th wait for a cleared plate."""
    async with asyncio.timeout(60):
        while sum(update.lan_awaiting_clear for update in fake.updates) < count:
            await asyncio.sleep(0.1)


async def test_a_plate_clear_waits_for_the_confirmation(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """#4939: with ``lan_require_plate_clear`` on, the run waits after each print until
    someone says the plate is clear. A confirmation sent for an earlier wait (fewer
    starts than the run has made) starts nothing."""
    sent_early: list[int] = []

    async def confirm(client: Client, arg: PrintRunInput, fake: NativeFake) -> None:
        sequence = client.get_workflow_handle(lan_sequence_id("run-1"))
        await _awaiting_clear(fake)
        # A stale confirmation, for no print started yet.
        await sequence.signal(PLATE_CLEARED_SIGNAL, 0)
        await asyncio.sleep(1.5)
        sent_early.append(len(fake_lan.project_files()))
        await sequence.signal(PLATE_CLEARED_SIGNAL, 1)
        await _awaiting_clear(fake, 2)
        await sequence.signal(PLATE_CLEARED_SIGNAL, 2)

    fake, run = await _run(
        client,
        pg_pool,
        registry_native,
        blobs=Blobs(),
        copies=3,
        reports=FakeReports(fake_lan),
        plate_clear=True,
        during=confirm,
    )
    assert sent_early == [1]
    assert len(fake_lan.project_files()) == 3
    result = run.result
    assert result is not None and result.lan_waiting == [] and result.lan_stopped is None
    assert not result.lan_awaiting_clear
    waits = [update for update in fake.updates if update.lan_awaiting_clear]
    # Each wait names the print that ended, and the next still to start.
    assert [len(update.lan) for update in waits] == [1, 2]
    assert waits[0].lan[0].ended == "done" and waits[0].lan_waiting == [1, 1]


async def test_a_run_without_the_setting_never_waits_for_a_clear(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    fake, run = await _run(
        client, pg_pool, registry_native, blobs=Blobs(), copies=2, reports=FakeReports(fake_lan)
    )
    assert run.status == "succeeded" and len(fake_lan.project_files()) == 2
    assert not any(update.lan_awaiting_clear for update in fake.updates)


async def test_a_stop_while_it_waits_for_a_clear_stops_the_rest(
    client: Client, pg_pool: PgPool, fake_lan: FakeLan, registry_native: str
) -> None:
    """#4942: a stop (the sequence's cancel) while the run waits for a cleared plate
    starts none of the rest, and the run no longer reads as waiting for one."""

    async def stop(client: Client, arg: PrintRunInput, fake: NativeFake) -> None:
        await _awaiting_clear(fake)
        await client.get_workflow_handle(lan_sequence_id("run-1")).cancel()

    _, run = await _run(
        client,
        pg_pool,
        registry_native,
        blobs=Blobs(),
        copies=2,
        reports=FakeReports(fake_lan),
        plate_clear=True,
        during=stop,
    )
    assert len(fake_lan.project_files()) == 1
    result = run.result
    assert result is not None and result.lan_waiting == [1]
    assert result.lan_stopped is not None and result.lan_stopped.stage == "cancelled"
    assert not result.lan_awaiting_clear
