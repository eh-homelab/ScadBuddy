"""What crosses ``PrintRun``'s history (#1052, spec 2026-10-01 §5).

Nothing here holds a secret: the stored settings, which hold the Bambuddy key, are
loaded inside each activity and never passed in or out of one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_serializer, model_validator

from scadbuddy.bambuddy.dispatch import QueueOutcome, SlicePlan
from scadbuddy.bambuddy.filaments import FilamentPlan, SpoolOption
from scadbuddy.bambuddy.filaments3mf import LocalFilaments
from scadbuddy.bambuddy.models import DEFAULT_ALGORITHM, RackAlgorithm
from scadbuddy.bambuddy.print_run import (
    PlannedRun,
    PlatePlan,
    PreparedPlates,
    PrintRunRequest,
    PrintRunResult,
    QueuedPlate,
)
from scadbuddy.bambuddy.runs import PrintRun, PrintRunError
from scadbuddy.library.outputs import PlateSend
from scadbuddy.printers.lan_models import LanStarted
from scadbuddy.printers.nozzle_mapping import SlicedRack

#: The workflow type every print route starts.
PRINT_RUN_WORKFLOW = "PrintRun"
#: A LAN run's later plate copies (#4541), started by its ``PrintRun`` once the first
#: is printing, and left to run past it (`lan_sequence_id`).
LAN_SEQUENCE_WORKFLOW = "PrintLanSequence"


def lan_sequence_id(run_id: str) -> str:
    """The one ``PrintLanSequence`` of a run."""
    return f"print-lan-{run_id}"


#: The signal that says a LAN run's plate is clear (#4939). Its argument is how many
#: prints the run had started (``len(result.lan)``) when the person said so, so a
#: confirmation sent for an earlier wait never starts a later print.
PLATE_CLEARED_SIGNAL = "plate_cleared"


#: The Update the route waits on: the record, or the refusal (§5.1).
ACCEPTED_UPDATE = "accepted"
#: An activity's `ApplicationError.type` for a refusal before the record exists: the
#: workflow fails and the route answers with the problem.
REFUSED = "PrintRefused"
#: The same after the record exists: the run is recorded as failed with the problem.
FAILED = "PrintFailed"
#: A slice that left nothing behind (`SliceLostError`, #2081): the workflow starts it
#: again, a few times, before the run is recorded as failed with the problem.
SLICE_LOST = "PrintSliceLost"
#: A reused slice (ScadBuddy's own earlier slice of the copy) that Bambuddy no longer
#: has when its plate is queued (#2955): nothing was queued, so the workflow slices the
#: plate again and queues that.
SLICE_GONE = "PrintSliceGone"
#: A LAN start that may not have been taken (plan 2026-10-16 Task 9): the printer could
#: not be reached, or did not report the plate within `LAN_START_WAIT`. The workflow
#: uploads again under a new name and starts again; ``lan_print`` first looks for the
#: plate in the printer's report, so a start it did take is not sent twice.
LAN_UNSURE = "PrintLanUnsure"


class SourceSpec(BaseModel):
    """What a run prints, as an activity rebuilds it (``OutputSource``/``LibrarySource``)."""

    kind: Literal["output", "library"]
    output_id: str | None = None
    file_id: int | None = None
    #: Computed by ``print_accept`` before the record, so a model.json that refuses
    #: them refuses the run (#770).
    stem: str | None = None
    print_settings: dict[str, str] = Field(default_factory=dict)
    #: A library file's filament slots, read from the file itself by the route that
    #: starts the run (#2550): every activity reads them from here, since the print
    #: worker mounts no cache, and Bambuddy's filament-requirements outlasts a large
    #: 3MF. ``None`` when the route could not read them, and Bambuddy is asked.
    filaments: LocalFilaments | None = None


class PrintRunInput(BaseModel):
    #: An output id, or ``library:<file id>`` (#945).
    subject: str
    slug: str
    #: ``run_key(subject, request)``; the workflow ID is ``print-<key>``.
    key: str
    source: SourceSpec
    request: PrintRunRequest
    #: How long a run that succeeded or may have queued answers repeats (§5.2); the
    #: route sends ``REPEAT_WINDOW``. In the input so the workflow stays deterministic.
    repeat_window_s: float = 600.0
    #: Upsert the Scadbuddy* Search Attributes (``SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES``).
    search_attributes: bool = False

    @field_serializer("request")
    def _keep_what_was_left_out(self, request: PrintRunRequest) -> dict[str, Any]:
        # A field the client omitted reads back as omitted: an omitted `project_id` is
        # the remembered project, an explicit null is none (`chosen_project`).
        return request.model_dump(mode="json", exclude_unset=True)


class Checked(BaseModel):
    """``print_check``'s answer: Bambuddy's checks passed; nothing written yet."""

    source: SourceSpec
    prepared: PreparedPlates


class CheckInput(BaseModel):
    input: PrintRunInput
    #: The execution's start, on the server's clock: ``print_check`` refuses a run no
    #: client is waiting for any more (review #1061 1b, 3).
    started_at: datetime


class InsertInput(BaseModel):
    input: PrintRunInput
    checked: Checked


class Accepted(BaseModel):
    run: PrintRun
    source: SourceSpec
    prepared: PreparedPlates


class AcceptAnswer(BaseModel):
    """The ``accepted`` Update's answer: the run, or why it was refused."""

    run: PrintRun | None = None
    refusal: PrintRunError | None = None
    #: The Update was not the execution's first: this request repeats a run.
    repeated: bool = False


class PlanInput(BaseModel):
    input: PrintRunInput
    accepted: Accepted


class SliceStartInput(BaseModel):
    library_file_id: int
    plan: SlicePlan
    plate_id: int


#: The native slicer's run activities, on the ``library`` queue (plan 2026-10-16 Task
#: 13): the slice, started or joined, and its upload beside the copy.
SLICE_RUN_ACTIVITY = "slice_run"
SLICED_UPLOAD_ACTIVITY = "print_sliced_upload"


class SliceRunInput(BaseModel):
    library_file_id: int
    plan: SlicePlan
    plate_id: int
    #: The run's subject, which the dialog's slice of the same copy shares (Ruling C2).
    subject: str


class NativeSliced(BaseModel):
    """A completed native slice: its `native_slices` row and its blob."""

    slice_id: int
    preset_key: str
    sliced_blob_key: str


class SlicedUploadInput(BaseModel):
    planned: PlannedRun
    plate_id: int
    sliced: NativeSliced


class EnqueueInput(BaseModel):
    """One sliced plate to queue: the rack pick (#836) runs in the same activity, so a
    hotend's serial never enters the history (spec 2026-10-01 §7)."""

    planned: PlannedRun
    plate: PlatePlan
    sliced: int

    @property
    def plate_id(self) -> int:
        return self.plate.plate_id


class RecordInput(BaseModel):
    source: SourceSpec
    library_file_id: int
    plate_id: int
    outcome: QueueOutcome
    project_id: int | None = None
    sent: list[PlateSend] = Field(default_factory=list)
    #: The run that queued the plate (#1751), recorded with its sends so a library file's
    #: follow reads its own run's. ``None`` in the histories of runs before it.
    run_id: str | None = None


class FinishInput(BaseModel):
    input: PrintRunInput
    run_id: str
    planned: PlannedRun
    outcomes: list[QueueOutcome]
    queued: list[QueuedPlate] = Field(default_factory=list)


class SucceedInput(BaseModel):
    """``print_succeed``: the run's result, recorded with no limit on retries."""

    input: PrintRunInput
    run_id: str
    result: PrintRunResult


class FailInput(BaseModel):
    run_id: str
    slug: str
    error: PrintRunError
    #: The row says the run began queueing, but it stopped before any ``POST /queue/``:
    #: the record clears that (review #1316 (8) 2).
    unqueued: bool = False


#: The native LAN branch's activities (plan 2026-10-16 Task 9), on the print worker's
#: queue: the sliced 3MF onto the printer over FTPS, then ``print.project_file``.
LAN_UPLOAD_ACTIVITY = "lan_upload"
LAN_PRINT_ACTIVITY = "lan_print"
#: How long ``lan_print``'s report read before a start may take, in seconds.
LAN_PROBE_TIMEOUT = 15.0
#: How long ``lan_print`` waits for the printer to report the plate it sent: it was
#: ``RUNNING`` 1.8 s after the start on the H2C (Task 7).
LAN_START_WAIT = 30.0
#: Until the printer has finished a LAN start (#4541): the next plate copy of the run
#: waits for it, as Bambuddy's queue waits for an idle printer.
LAN_WAIT_ACTIVITY = "lan_wait"
#: Seconds between its reads of the printer's report: the watch writes one at most a
#: second, and a plate's next start a few seconds late costs nothing.
LAN_WAIT_POLL = 10.0
#: A succeeded run's result rewritten as its later LAN starts are made (#4541).
PRINT_LAN_UPDATE_ACTIVITY = "print_lan_update"


class SlicedSource(BaseModel):
    """Where a plate's sliced 3MF is read from: a library file, or the native slicer's
    blob (plan 2026-10-16-native-slicer-rust-worker), read through the API's internal
    blob route. Exactly one is set."""

    library_file_id: int | None = None
    blob_key: str | None = None

    @model_validator(mode="after")
    def _one(self) -> SlicedSource:
        if (self.library_file_id is None) == (self.blob_key is None):
            raise ValueError("a sliced source is a library file or a blob, exactly one")
        return self


class LanUploadInput(BaseModel):
    run_id: str
    #: The registry row (`PrinterRow.id`); its access code is opened in the activity.
    printer: str
    sliced: SlicedSource
    #: In `lan.UPLOAD_DIR`; a new one per start attempt, since the printer moves a
    #: started upload away (Task 7's measurement).
    remote_name: str
    #: The plate whose rack plan is read (hot-path Task 8); ``None`` reads a file of
    #: one plate only, as an input from before it.
    plate_id: int | None = None


class LanUploaded(BaseModel):
    remote_name: str
    md5: str
    bytes: int
    #: The project's filament slots, read from the 3MF (`ams_mapping`'s count).
    filament_count: int = 1
    #: The plate's rack plan, read from the 3MF (hot-path Task 8); ``None`` for a
    #: printer with no rack, or a plan that cannot be read (no ``nozzle_mapping``).
    rack: SlicedRack | None = None


class LanPrintInput(BaseModel):
    printer: str
    uploaded: LanUploaded
    plate_id: int
    subtask_name: str
    #: The trays chosen per slot; ``lan_print`` maps them (`printers/ams_mapping.py`)
    #: against the report it reads as it starts, which says whether there is an AMS and
    #: a second nozzle.
    plan: FilamentPlan
    bed_type: str = "auto"
    timelapse: bool = False
    layer_inspect: bool = True
    #: The rack pick's ranking and hand-picked position (`PlannedRun`'s), and each slot's
    #: spool for its Glow test, ranked in ``lan_print`` against the report it reads
    #: (hot-path Task 8). The defaults read an input from before it.
    rack_algorithm: RackAlgorithm = DEFAULT_ALGORITHM
    rack_position: int | None = None
    spools: dict[int, SpoolOption] = Field(default_factory=dict)


class LanWaitInput(BaseModel):
    """``lan_wait``: the registry row, and the start whose print it waits out."""

    printer: str
    started: LanStarted


class LanStart(BaseModel):
    """One plate copy a LAN run starts later (#4541): its plate, the sliced file and how
    it was sliced (the plate's record names them), and which copy it is."""

    plate: PlatePlan
    sliced: int
    slice_job_id: int | None = None
    preset_key: str | None = None
    native: NativeSliced | None = None
    #: Which copy of the plate it is, from 1.
    copy_number: int


class LanSequenceInput(BaseModel):
    """``PrintLanSequence``: the run, its plan, the starts still to make in order, its
    result as it succeeded, and what its plates' records hold so far."""

    input: PrintRunInput
    accepted: Accepted
    planned: PlannedRun
    starts: list[LanStart]
    result: PrintRunResult
    #: The plates already recorded: a plate is recorded once, by its first copy.
    recorded: list[int] = Field(default_factory=list)
    sent: list[PlateSend] = Field(default_factory=list)


class LanUpdateInput(BaseModel):
    """``print_lan_update``: the run's whole result as it stands now."""

    run_id: str
    slug: str
    result: PrintRunResult
