"""Starting a native slice, reading its progress and sweeping its rows (plan
2026-10-16-native-slicer-rust-worker Task 11): what the dialog's ``preview_run`` and the
run's ``slice_run`` both call, so the two name one `SliceWorkflow` execution (Ruling
C2)."""

from __future__ import annotations

import logging
from datetime import timedelta

from pydantic import BaseModel, ConfigDict
from temporalio.api.enums.v1 import WorkflowExecutionStatus
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.core.settings import Settings
from scadbuddy.slicing.models import SliceProgress
from scadbuddy.slicing.presets import build_presets, native_preset_key
from scadbuddy.slicing.profiles import Profiles
from scadbuddy.slicing.store import NativeSlice, NativeSliceStore, native_workflow_id
from scadbuddy.workflows.slicing import SliceWorkflow
from scadbuddy.workflows.slicing_models import SLICE_PLATE, SliceWorkflowInput

logger = logging.getLogger(__name__)

#: How old a running row is before the sweep asks Temporal whether it still has an
#: execution: a start writes the row a moment before it starts the workflow.
LOST_AFTER = timedelta(minutes=15)
#: A running row whose execution is gone (a start that never reached Temporal, a
#: terminated execution).
STOPPED = "The slice stopped."


class NativeSliceStarter(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: `Settings.temporal_task_queue_library`: where `SliceWorkflow` runs.
    library_queue: str
    #: `Settings.temporal_task_queue_slicer`: where the slicer worker serves `slice_plate`.
    slicer_queue: str
    #: The image's profiles: the presets are built from them, and keyed by their
    #: `Profiles.version`, the image's ``BAMBU_STUDIO_VERSION`` (Ruling C7), so the two
    #: never come from different trees.
    profiles: Profiles


def native_slice_starter(
    settings: Settings, profiles: Profiles | None = None
) -> NativeSliceStarter | None:
    """None without Bambu Studio's profiles (a run outside the image)."""
    profiles = profiles or Profiles.installed(settings.slicer_profiles_dir)
    if profiles is None:
        return None
    return NativeSliceStarter(
        library_queue=settings.temporal_task_queue_library,
        slicer_queue=settings.temporal_task_queue_slicer,
        profiles=profiles,
    )


async def start_native_slice(
    temporal: Client,
    store: NativeSliceStore,
    starter: NativeSliceStarter,
    *,
    library_file_id: int,
    plan: SlicePlan,
    plate_id: int,
    subject: str,
    client: BambuddyClient | None,
    copy_sha256: str | None,
) -> NativeSlice:
    """The row of copy ``library_file_id`` sliced with ``plan``, its execution started
    unless it completed. The presets are built here only for their key: a process
    preset Bambuddy's values built is keyed by what it held
    (`PresetFiles.bambuddy_process`), and the key names the execution, so it is known
    before the start; `slice_prepare` builds them again, where they are stored. A
    `ProfileError` propagates: no row is written. ``client`` is None with no Bambuddy
    configured: then only the image's presets resolve. ``copy_sha256`` is the copy's
    ``file_hash`` as its library states it (Task 12 review M6): it names the execution
    with the id, and `slice_prepare` refuses other bytes.

    A failed or cancelled row is returned running (`NativeSliceStore.start`), so a read
    straight after this never sees the last outcome (Task 12 review I1)."""
    profiles = starter.profiles
    files = await build_presets(plan, profiles, client)
    preset_key = native_preset_key(plan, plate_id, profiles.version, files.bambuddy_process)
    row = await store.start(
        workflow_id=native_workflow_id(
            library_file_id, preset_key, subject=subject, copy_sha256=copy_sha256
        ),
        subject=subject,
        library_file_id=library_file_id,
        plate_id=plate_id,
        preset_key=preset_key,
    )
    if row.status != "completed":
        await temporal.start_workflow(
            SliceWorkflow.run,
            SliceWorkflowInput(
                slice_id=row.id,
                library_file_id=library_file_id,
                plan=plan,
                plate_id=plate_id,
                preset_key=preset_key,
                slicer_queue=starter.slicer_queue,
                copy_sha256=copy_sha256,
            ),
            id=row.workflow_id,
            task_queue=starter.library_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    return row


async def _has_execution(temporal: Client, workflow_id: str) -> bool:
    try:
        description = await temporal.get_workflow_handle(workflow_id).describe()
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    status = description.raw_description.workflow_execution_info.status
    return bool(status == WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_RUNNING)


async def stop_lost(temporal: Client, store: NativeSliceStore, row: NativeSlice) -> bool:
    """Fail running ``row`` if no open execution writes it (Task 11 review I2: a start
    that never reached Temporal, a terminated execution), dropping its holds; only if it
    still reads running from the same start (re-review N1). Whether it was stopped."""
    if await _has_execution(temporal, row.workflow_id):
        return False
    stopped = await store.stop_lost(row, STOPPED)
    if stopped:
        logger.warning("a native slice's execution is gone", extra={"slice_id": row.id})
    return stopped


async def stop_lost_native_slices(
    temporal: Client, store: NativeSliceStore, lost_after: timedelta = LOST_AFTER
) -> list[int]:
    """`stop_lost` for every running row started more than ``lost_after`` ago. On the
    prune Schedule, so it runs with the sweeps off (Task 12 review M1). The ids stopped."""
    return [row.id for row in await store.lost(lost_after) if await stop_lost(temporal, store, row)]


async def sweep_native_slices(
    store: NativeSliceStore, *, finished_older_than: timedelta
) -> list[int]:
    """Delete the rows finished more than ``finished_older_than`` ago, with their holds;
    the blobs go on `sweep_content`'s next pass. The ids deleted."""
    return await store.sweep(finished_older_than)


async def slice_progress(temporal: Client, workflow_id: str) -> SliceProgress | None:
    """The last heartbeat of the execution's pending ``slice_plate``, None when there is
    none (preparing, recording, done) or it has not beaten yet. The worker's first beat
    is all zeros (`SliceProgress::default`), so a slice that started reads 0, not None.
    An execution Temporal has no record of (never started, or past its retention) is
    None too (Task 12 review M1)."""
    try:
        description = await temporal.get_workflow_handle(workflow_id).describe()
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return None
        raise
    for pending in description.raw_description.pending_activities:
        if pending.activity_type.name != SLICE_PLATE:
            continue
        if not pending.heartbeat_details.payloads:
            return None
        (progress,) = await temporal.data_converter.decode(
            pending.heartbeat_details.payloads[:1], [SliceProgress]
        )
        return progress if isinstance(progress, SliceProgress) else None
    return None
