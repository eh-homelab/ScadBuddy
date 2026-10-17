"""What crosses `StoreMigration`'s history, and its id (spec 2026-10-16 §4.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, model_validator

from scadbuddy.core.config import StoreBackend


class StoreMigrationParams(BaseModel):
    source: StoreBackend
    target: StoreBackend
    batch: int = 50

    @model_validator(mode="after")
    def _two_remote_backends(self) -> Self:
        if self.source == self.target:
            raise ValueError("source and target are the same backend")
        if "local" in (self.source, self.target):
            raise ValueError("the local store has no content backend to migrate")
        return self


class StoreMigrationInput(StoreMigrationParams):
    """A run's input. What it carries across continue-as-new is bounded: two counters
    and its start. Which rows failed, and why, is on the rows (`migrated_error`)."""

    #: The first run's start, by the database's clock (`BlobIndex.now`): a row whose
    #: copy failed before it is tried again.
    started: datetime
    batches_done: int = 0
    copied: int = 0


class StoreMigrationResult(BaseModel):
    batches_done: int
    copied: int


def migration_id(source: StoreBackend, target: StoreBackend) -> str:
    return f"store-migrate-{source}-{target}"
