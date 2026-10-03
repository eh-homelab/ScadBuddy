"""What crosses ``PrintRun``'s history (#1052, spec 2026-10-01 §5).

Nothing here holds a secret: the stored settings, which hold the Bambuddy key, are
loaded inside each activity and never passed in or out of one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.dispatch import QueueOutcome, SlicePlan
from scadbuddy.bambuddy.filaments import QueueFilaments
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_run import PlannedRun, PreparedPlates, PrintRunRequest
from scadbuddy.bambuddy.runs import PrintRun, PrintRunError
from scadbuddy.library.outputs import PlateSend

#: The workflow type every print route starts.
PRINT_RUN_WORKFLOW = "PrintRun"
#: The Update the route waits on: the record, or the refusal (§5.1).
ACCEPTED_UPDATE = "accepted"
#: An activity's `ApplicationError.type` for a refusal before the record exists: the
#: workflow fails and the route answers with the problem.
REFUSED = "PrintRefused"
#: The same after the record exists: the run is recorded as failed with the problem.
FAILED = "PrintFailed"


class SourceSpec(BaseModel):
    """What a run prints, as an activity rebuilds it (``OutputSource``/``LibrarySource``)."""

    kind: Literal["output", "library"]
    output_id: str | None = None
    file_id: int | None = None
    #: Computed by ``print_accept`` before the record, so a model.json that refuses
    #: them refuses the run (#770).
    stem: str | None = None
    print_settings: dict[str, str] = Field(default_factory=dict)


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


class Checked(BaseModel):
    """``print_check``'s answer: Bambuddy's checks passed; nothing written yet."""

    source: SourceSpec
    prepared: PreparedPlates


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


class EnqueueInput(BaseModel):
    sliced: int
    printer_id: int
    plate_id: int
    copies: int
    project_id: int | None = None
    options: PrintOptions
    filaments: QueueFilaments | None = None


class RecordInput(BaseModel):
    source: SourceSpec
    library_file_id: int
    plate_id: int
    outcome: QueueOutcome
    project_id: int | None = None
    sent: list[PlateSend] = Field(default_factory=list)


class FinishInput(BaseModel):
    input: PrintRunInput
    run_id: str
    planned: PlannedRun
    outcomes: list[QueueOutcome]


class FailInput(BaseModel):
    run_id: str
    slug: str
    error: PrintRunError
