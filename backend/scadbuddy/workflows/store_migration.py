"""`StoreMigration` (spec 2026-10-16 §4.3): every recorded blob moved from one backend
to another, a batch per `store_migrate_batch` activity on the ``library`` queue, until
none is left but those that failed. One run per pair of backends (`migration_id`); its
`progress` query is what `GET /store/migrate` answers."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from scadbuddy.store.migrate import MIGRATE_BATCH, MigrateBatchInput, MigrationBatch
    from scadbuddy.workflows.store_migration_models import (
        StoreMigrationParams,
        StoreMigrationProgress,
        StoreMigrationResult,
    )

STORE_MIGRATION_WORKFLOW = "StoreMigration"
#: Batches per run before it continues as new, which keeps its history short.
BATCHES_PER_RUN = 200
HEARTBEAT_TIMEOUT = timedelta(seconds=60)
BATCH_TIMEOUT = timedelta(minutes=30)


@workflow.defn(name=STORE_MIGRATION_WORKFLOW)
class StoreMigration:
    def __init__(self) -> None:
        self._progress = StoreMigrationProgress()

    @workflow.run
    async def run(
        self, params: StoreMigrationParams, carried: StoreMigrationProgress | None = None
    ) -> StoreMigrationResult:
        if carried is not None:
            self._progress = carried
        for _ in range(BATCHES_PER_RUN):
            batch = await workflow.execute_activity(
                MIGRATE_BATCH,
                MigrateBatchInput(
                    source=params.source,
                    target=params.target,
                    limit=params.batch,
                    skip=self._progress.failed,
                ),
                result_type=MigrationBatch,
                start_to_close_timeout=BATCH_TIMEOUT,
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
            )
            self._progress = StoreMigrationProgress(
                moved=self._progress.moved + batch.moved,
                remaining=batch.remaining,
                failed=[*self._progress.failed, *batch.failed],
            )
            if batch.remaining == 0:
                return StoreMigrationResult(
                    moved=self._progress.moved, failed=self._progress.failed
                )
        workflow.continue_as_new(args=[params, self._progress])

    @workflow.query
    def progress(self) -> StoreMigrationProgress:
        return self._progress
