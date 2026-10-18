"""`StoreMigration` (spec 2026-10-16 §4.3): every recorded blob copied from one backend
to another, a batch per `store_migrate_batch` activity on the ``library`` queue, until
none is left but those whose copy failed (each row says why). One run per pair of
backends (`migration_id`); `GET /store/migrate` reads the rows and whether it runs.

On the versioned ``library`` worker (#4090) it is AUTO_UPGRADE
(`VersionedStoreMigration`), so ``StoreMigration.run`` must stay replay-compatible: a
change to its command sequence goes behind ``workflow.patched``."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import VersioningBehavior

with workflow.unsafe.imports_passed_through():
    from scadbuddy.store.migrate import MIGRATE_BATCH, MigrateBatchInput, MigrationBatch
    from scadbuddy.workflows.store_migration_models import (
        StoreMigrationInput,
        StoreMigrationResult,
    )

STORE_MIGRATION_WORKFLOW = "StoreMigration"
#: Batches per run before it continues as new, which keeps its history short.
BATCHES_PER_RUN = 200
HEARTBEAT_TIMEOUT = timedelta(seconds=60)
BATCH_TIMEOUT = timedelta(minutes=30)


@workflow.defn(name=STORE_MIGRATION_WORKFLOW)
class StoreMigration:
    @workflow.run
    async def run(self, input: StoreMigrationInput) -> StoreMigrationResult:
        batches_done, copied = input.batches_done, input.copied
        for _ in range(BATCHES_PER_RUN):
            batch = await workflow.execute_activity(
                MIGRATE_BATCH,
                MigrateBatchInput(
                    source=input.source,
                    target=input.target,
                    limit=input.batch,
                    started=input.started,
                ),
                result_type=MigrationBatch,
                start_to_close_timeout=BATCH_TIMEOUT,
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
            )
            batches_done += 1
            copied += batch.moved
            if batch.remaining == 0:
                return StoreMigrationResult(batches_done=batches_done, copied=copied)
        workflow.continue_as_new(
            input.model_copy(update={"batches_done": batches_done, "copied": copied})
        )


@workflow.defn(name=STORE_MIGRATION_WORKFLOW, versioning_behavior=VersioningBehavior.AUTO_UPGRADE)
class VersionedStoreMigration(StoreMigration):
    """``StoreMigration`` on the versioned ``library`` worker (#4090). AUTO_UPGRADE: a
    migration copies every blob, for hours, across any number of rollouts; Pinned, the
    old build's last replica would strand it. Its own class because an unversioned
    worker cannot complete a workflow that declares a behavior."""

    @workflow.run
    async def run(self, input: StoreMigrationInput) -> StoreMigrationResult:
        return await super().run(input)
