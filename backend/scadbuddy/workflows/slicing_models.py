"""What crosses `SliceWorkflow`'s history (plan 2026-10-16-native-slicer-rust-worker
Task 11): ids, the plan's refs, keys and transfer URLs; never the copy's bytes or a preset
document. A model holding a `TransferUrl` sets ``hide_input_in_errors`` (pydantic takes
it from the model validated), and `TransferUrl`'s repr redacts it."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.slicing.models import SlicePlateInput, SlicePlateOutput
from scadbuddy.slicing.transfer import TRANSFER_TTL, PendingPut

SLICE_WORKFLOW = "SliceWorkflow"
PREPARE_ACTIVITY = "slice_prepare"
RECORD_ACTIVITY = "slice_record"
#: The Rust worker's activity (`sb-slicer` `activity.rs` `SLICE_PLATE`).
SLICE_PLATE = "slice_plate"
#: The `ApplicationError` type of `slice_prepare`'s own refusals, whose message is the
#: slice's failure.
SLICE_REFUSED = "SliceRefused"
#: How long ``slice_plate`` waits for a slicer worker to take it (review M2): past
#: `TRANSFER_TTL` a worker would find its URLs expired anyway.
SLICER_WAIT = TRANSFER_TTL
#: Spec §5.2; the worker beats every 10 s.
SLICE_HEARTBEAT = timedelta(seconds=60)


class SliceWorkflowInput(BaseModel):
    slice_id: int
    library_file_id: int
    plan: SlicePlan
    plate_id: int
    preset_key: str
    slicer_queue: str
    #: `SLICER_WAIT`; a test names a shorter one.
    slicer_wait: timedelta = SLICER_WAIT
    #: `SLICE_HEARTBEAT`; a test names a shorter one (#4403): an attempt a Python fake ends
    #: as cancelled, with no cancel asked of it, is not taken by the server and waits it out.
    slice_heartbeat: timedelta = SLICE_HEARTBEAT
    #: The copy's ``file_hash`` the start named the execution by (Task 12 review M6):
    #: other bytes under the id are refused. None when the library stated none.
    copy_sha256: str | None = None


class SlicePrepared(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    plate_input: SlicePlateInput | None
    put: PendingPut | None
    #: A completed slice of the same bytes and presets: nothing to slice.
    reused_blob_key: str | None
    #: The reused slice's warnings.
    reused_warnings: list[str] = Field(default_factory=list)


class SliceRecordInput(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    slice_id: int
    put: PendingPut | None
    output: SlicePlateOutput | None
    reused_blob_key: str | None
    failure: str | None
    reused_warnings: list[str] = Field(default_factory=list)
    #: The workflow was cancelled: ``failure`` says so and the row reads cancelled.
    cancelled: bool = False
    #: ``failure`` is a refusal of the plate or its input, not a fault: a run reports
    #: it as the plan's 422, not a 502 (Task 13).
    refused: bool = False
    #: The status a run reports ``failure`` with (`slicing.failure_status`, Task 13
    #: review M3); None in a history from before it, read by ``refused``.
    failure_status: int | None = None


class SliceWorkflowResult(BaseModel):
    status: Literal["completed", "failed", "cancelled"]
    sliced_blob_key: str | None
    failure: str | None
    warnings: list[str]
    #: As `SliceRecordInput.refused` and `SliceRecordInput.failure_status`.
    refused: bool = False
    failure_status: int | None = None
