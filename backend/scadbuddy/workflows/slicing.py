"""``SliceWorkflow``: one native slice of a laid-out copy (plan
2026-10-16-native-slicer-rust-worker Task 11, spec 2026-10-16 §5.2, §5.5), on the
``library`` queue (the API process: the content store, the Bambuddy client and the
native library; Ruling C1).

1. ``slice_prepare`` reads the copy, stores it and the presets, holds them and mints the
   transfer URLs, or finds a completed slice of the same bytes and presets to reuse.
2. Unless reused, ``slice_plate`` on the slicer queue, retried three times (Ruling C4).
   URLs the worker refuses (``TransferRefused``: they expired in a long queue wait) are
   prepared once more and sliced once more.
3. ``slice_record`` holds the sliced blob and finishes the row, completed or failed.

No bytes and no preset document cross the history: ``SlicePrepared`` carries URLs and
the record carries keys. On the versioned ``library`` worker it runs as
``VersionedSliceWorkflow``, AUTO_UPGRADE, so a change to the commands ``run`` issues goes
behind ``workflow.patched`` (``tests/test_slice_replay.py``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, VersioningBehavior
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from scadbuddy.slicing.models import (
        SLICER_ERROR,
        TRANSFER_REFUSED,
        VALIDATION_EXIT,
        SlicePlateOutput,
    )
    from scadbuddy.workflows.slicing_models import (
        PREPARE_ACTIVITY,
        RECORD_ACTIVITY,
        SLICE_PLATE,
        SLICE_REFUSED,
        SLICE_WORKFLOW,
        SlicePrepared,
        SliceRecordInput,
        SliceWorkflowInput,
        SliceWorkflowResult,
    )

#: One ``slice_plate`` attempt's start-to-close.
SLICE_TIMEOUT = timedelta(minutes=30)
#: Spec §5.2; the worker beats every 10 s.
SLICE_HEARTBEAT = timedelta(seconds=60)
#: "Retries three times" (spec §5.5, Ruling C4): a worker shutdown and a heartbeat
#: timeout take attempts too, and a shutdown's retry starts about at once.
SLICE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=4)
#: The prepare downloads a whole copy from Bambuddy: its upload timeout (180 s,
#: ``DEFAULT_UPLOAD_TIMEOUT``) and a margin, as ``printing.py`` ``PLAN_TIMEOUT``.
PREPARE_TIMEOUT = timedelta(minutes=5)
SHORT = timedelta(seconds=60)
READ_RETRY = RetryPolicy(
    maximum_attempts=3, initial_interval=timedelta(seconds=1), backoff_coefficient=2.0
)
#: As ``printing.py``'s: a Postgres blip must not lose an outcome.
RECORD_RETRY = RetryPolicy(
    maximum_attempts=0,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)


def _type(cause: BaseException | None) -> str:
    if isinstance(cause, ApplicationError):
        return cause.type or "ApplicationError"
    return type(cause).__name__


def prepare_failure(cause: BaseException | None) -> str:
    """A refusal of ``slice_prepare``'s own says why; anything else names its type only."""
    if isinstance(cause, ApplicationError) and cause.type == SLICE_REFUSED:
        return cause.message
    return f"Preparing the slice failed ({_type(cause)})."


def slice_failure(cause: BaseException | None) -> str:
    """Ruling C4: a ``SlicerError``'s ``stderr_tail``, the only details decoded (the
    worker's README, "Slicer worker"); any other type is named, never its message."""
    if isinstance(cause, ApplicationError) and cause.type == SLICER_ERROR:
        details: Any = cause.details[0] if cause.details else {}
        if not isinstance(details, dict):
            details = {}
        refused = details.get("exit_code") == VALIDATION_EXIT
        prefix = "The slicer refused the plate:" if refused else "The slicer failed:"
        return f"{prefix} {details.get('stderr_tail', '')}".rstrip()
    return f"The slicer worker failed ({_type(cause)})."


@workflow.defn(name=SLICE_WORKFLOW)
class SliceWorkflow:
    @workflow.run
    async def run(self, input: SliceWorkflowInput) -> SliceWorkflowResult:
        prepared, failure = await self._prepare(input)
        output: SlicePlateOutput | None = None
        again = True
        while prepared is not None and prepared.reused_blob_key is None:
            assert prepared.plate_input is not None
            try:
                output = await workflow.execute_activity(
                    SLICE_PLATE,
                    prepared.plate_input,
                    task_queue=input.slicer_queue,
                    start_to_close_timeout=SLICE_TIMEOUT,
                    heartbeat_timeout=SLICE_HEARTBEAT,
                    retry_policy=SLICE_RETRY,
                    result_type=SlicePlateOutput,
                )
                break
            except ActivityError as error:
                cause = error.cause
                if again and isinstance(cause, ApplicationError) and cause.type == TRANSFER_REFUSED:
                    again = False
                    prepared, failure = await self._prepare(input)
                    continue
                failure = slice_failure(cause)
                break
        reused = prepared.reused_blob_key if prepared is not None and failure is None else None
        record = SliceRecordInput(
            slice_id=input.slice_id,
            put=prepared.put if prepared is not None and output is not None else None,
            output=output if failure is None else None,
            reused_blob_key=reused,
            failure=failure,
            reused_warnings=prepared.reused_warnings if reused is not None and prepared else [],
        )
        result: SliceWorkflowResult = await workflow.execute_activity(
            RECORD_ACTIVITY,
            record,
            start_to_close_timeout=SHORT,
            retry_policy=RECORD_RETRY,
            result_type=SliceWorkflowResult,
        )
        return result

    async def _prepare(self, input: SliceWorkflowInput) -> tuple[SlicePrepared | None, str | None]:
        try:
            prepared: SlicePrepared = await workflow.execute_activity(
                PREPARE_ACTIVITY,
                input,
                start_to_close_timeout=PREPARE_TIMEOUT,
                retry_policy=READ_RETRY,
                result_type=SlicePrepared,
            )
        except ActivityError as error:
            return None, prepare_failure(error.cause)
        return prepared, None


@workflow.defn(name=SLICE_WORKFLOW, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedSliceWorkflow(SliceWorkflow):
    """``SliceWorkflow`` on the versioned ``library`` worker (#4090). AUTO_UPGRADE: a
    slice may wait in the slicer queue and run four attempts of `SLICE_TIMEOUT`, longer
    than a rollout; Pinned, the old build's last replica would strand its record. Its own
    class because an unversioned worker cannot complete a workflow that declares a
    behavior (the tests' workers). Changes still go behind ``workflow.patched``, in
    ``SliceWorkflow``."""

    @workflow.run
    async def run(self, input: SliceWorkflowInput) -> SliceWorkflowResult:
        return await super().run(input)
