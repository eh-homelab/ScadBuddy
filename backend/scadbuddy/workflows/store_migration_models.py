"""What crosses `StoreMigration`'s history, and its id (spec 2026-10-16 §4.3)."""

from __future__ import annotations

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


class StoreMigrationProgress(BaseModel):
    moved: int = 0
    remaining: int = 0
    failed: list[str] = []


class StoreMigrationResult(BaseModel):
    moved: int
    failed: list[str]


def migration_id(source: StoreBackend, target: StoreBackend) -> str:
    return f"store-migrate-{source}-{target}"
