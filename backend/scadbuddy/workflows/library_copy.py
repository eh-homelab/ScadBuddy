"""`LibraryCopy` (#5355): a copy between Bambuddy's library and ScadBuddy's own, on
the ``library`` queue. The plan activity walks the selection into rows, then a batch
activity copies `BATCH_FILES` of them at a time until none is pending, and the finish
activity closes the job's row. One execution per distinct request (`copy_id`): a
second start joins it, and one after it ended runs it again, skipping what landed.

On the versioned ``library`` worker (#4090) it is AUTO_UPGRADE (`VersionedLibraryCopy`),
so ``LibraryCopy.run`` must stay replay-compatible: a change to its command sequence
goes behind ``workflow.patched``."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import VersioningBehavior

with workflow.unsafe.imports_passed_through():
    from scadbuddy.print_library.copy import (
        LIBRARY_COPY_BATCH,
        LIBRARY_COPY_FINISH,
        LIBRARY_COPY_PLAN,
    )
    from scadbuddy.print_library.copy_models import (
        LibraryCopyBatch,
        LibraryCopyBatchInput,
        LibraryCopyCounts,
        LibraryCopyFinishInput,
        LibraryCopyInput,
        LibraryCopyPlan,
        LibraryCopyResult,
    )

LIBRARY_COPY_WORKFLOW = "LibraryCopy"
#: Batches per run before it continues as new, which keeps its history short.
BATCHES_PER_RUN = 100
HEARTBEAT_TIMEOUT = timedelta(seconds=60)
#: One batch: `BATCH_FILES` transfers, each up to the download cap over the LAN.
BATCH_TIMEOUT = timedelta(minutes=30)
PLAN_TIMEOUT = timedelta(minutes=10)


@workflow.defn(name=LIBRARY_COPY_WORKFLOW)
class LibraryCopy:
    @workflow.run
    async def run(self, input: LibraryCopyInput) -> LibraryCopyResult:
        batches_done = input.batches_done
        if not input.planned:
            # A plan that refuses recorded why on the job's row first; the failure
            # then ends this execution, and the status route reads the row.
            await workflow.execute_activity(
                LIBRARY_COPY_PLAN,
                input,
                result_type=LibraryCopyPlan,
                start_to_close_timeout=PLAN_TIMEOUT,
            )
            input = input.model_copy(update={"planned": True})
        counts = LibraryCopyCounts()
        for _ in range(BATCHES_PER_RUN):
            batch = await workflow.execute_activity(
                LIBRARY_COPY_BATCH,
                LibraryCopyBatchInput(copy_id=input.copy_id, request=input.request),
                result_type=LibraryCopyBatch,
                start_to_close_timeout=BATCH_TIMEOUT,
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
            )
            batches_done += 1
            counts.copied += batch.copied
            counts.skipped_same_hash += batch.skipped
            counts.failed += batch.failed
            if batch.remaining == 0:
                await workflow.execute_activity(
                    LIBRARY_COPY_FINISH,
                    LibraryCopyFinishInput(copy_id=input.copy_id),
                    start_to_close_timeout=timedelta(minutes=1),
                )
                return LibraryCopyResult(batches_done=batches_done, counts=counts)
        workflow.continue_as_new(input.model_copy(update={"batches_done": batches_done}))


@workflow.defn(name=LIBRARY_COPY_WORKFLOW, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedLibraryCopy(LibraryCopy):
    """``LibraryCopy`` on the versioned ``library`` worker (#4090). AUTO_UPGRADE: a copy
    of a whole library runs for hours, across any number of rollouts; Pinned, the old
    build's last replica would strand it. Its own class because an unversioned worker
    cannot complete a workflow that declares a behavior."""

    @workflow.run
    async def run(self, input: LibraryCopyInput) -> LibraryCopyResult:
        return await super().run(input)
