"""Starting a native slice and reading its progress (plan
2026-10-16-native-slicer-rust-worker Task 11): what the dialog's ``preview_run`` and the
run's ``slice_run`` both call, so the two name one `SliceWorkflow` execution (Ruling
C2)."""

from __future__ import annotations

from pydantic import BaseModel
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.core.settings import Settings
from scadbuddy.slicing.models import SliceProgress
from scadbuddy.slicing.presets import build_presets, native_preset_key
from scadbuddy.slicing.profiles import Profiles
from scadbuddy.slicing.store import NativeSlice, NativeSliceStore, native_workflow_id
from scadbuddy.workflows.slicing import SliceWorkflow
from scadbuddy.workflows.slicing_models import SLICE_PLATE, SliceWorkflowInput


class NativeSliceStarter(BaseModel):
    #: `Settings.temporal_task_queue_library`: where `SliceWorkflow` runs.
    library_queue: str
    #: `Settings.temporal_task_queue_slicer`: where the slicer worker serves `slice_plate`.
    slicer_queue: str
    #: `Profiles.version`: the image's ``BAMBU_STUDIO_VERSION`` (Ruling C7).
    version: str


def native_slice_starter(settings: Settings) -> NativeSliceStarter | None:
    """None without Bambu Studio's profiles (a run outside the image)."""
    profiles = Profiles.installed(settings.slicer_profiles_dir)
    if profiles is None:
        return None
    return NativeSliceStarter(
        library_queue=settings.temporal_task_queue_library,
        slicer_queue=settings.temporal_task_queue_slicer,
        version=profiles.version,
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
    profiles: Profiles,
    client: BambuddyClient | None,
) -> NativeSlice:
    """The row of copy ``library_file_id`` sliced with ``plan``, its execution started
    unless it completed. The presets are built here only for their key: a process
    preset Bambuddy's values built is keyed by what it held
    (`PresetFiles.bambuddy_process`), and the key names the execution, so it is known
    before the start; `slice_prepare` builds them again, where they are stored. A
    `ProfileError` propagates: no row is written."""
    files = await build_presets(plan, profiles, client)
    preset_key = native_preset_key(plan, plate_id, starter.version, files.bambuddy_process)
    row = await store.start(
        workflow_id=native_workflow_id(library_file_id, preset_key),
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
            ),
            id=row.workflow_id,
            task_queue=starter.library_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    return row


async def slice_progress(temporal: Client, workflow_id: str) -> SliceProgress | None:
    """The last heartbeat of the execution's pending ``slice_plate``, None when there is
    none (preparing, recording, done) or it has not beaten yet. The worker's first beat
    is all zeros (`SliceProgress::default`), so a slice that started reads 0, not None."""
    description = await temporal.get_workflow_handle(workflow_id).describe()
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
