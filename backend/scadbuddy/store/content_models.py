"""The store's value types (spec 2026-09-27 §6.2). `Scope` is `BlobScope` here:
`bambuddy.errors.Scope` already names Bambuddy's API-key scopes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

BlobKind = Literal["piece", "snapshot", "asset", "font"]
#: Kinds whose life the sweep decides: pieces by `blob_refs` and grace, snapshots (a
#: cache of git) by grace alone. Assets have `AssetStore.sweep`; fonts are never swept.
SWEPT_KINDS: tuple[BlobKind, ...] = ("piece", "snapshot")


class BlobScope(BaseModel):
    #: None for what belongs to no template (fonts).
    slug: str | None = None
    #: The template's display name, which names its Bambuddy folder.
    title: str | None = None
    folder: Literal["work", "output"] = "work"
    #: With `folder="output"`: the project whose folder receives the file (#317).
    project_id: int | None = None


class BlobRef(BaseModel):
    sha256: str
    kind: BlobKind
    backend: str
    backend_id: str
    size: int


class BlobStat(BaseModel):
    key: str
    ref: BlobRef
    slug: str | None
    meta: dict[str, Any]
    touched_at: datetime


class StoreUsage(BaseModel):
    """What the store holds against its caps; Settings shows it (spec §10)."""

    backend: str
    count: int
    bytes: int
    max_count: int
    max_total_bytes: int
    #: Bytes per kind.
    by_kind: dict[str, int]


class StoreFullError(Exception):
    """A put would take the store past SCADBUDDY_STORE_MAX_TOTAL_BYTES / _MAX_COUNT.

    The caps are checked, not reserved: they are soft by one blob per concurrent put."""


class BlobCorruptError(Exception):
    """The backend returned bytes whose sha256 is not the one recorded."""


class BlobMissingError(KeyError):
    """The backend no longer has the object (deleted behind ScadBuddy's back)."""


class RefusedDeleteError(RuntimeError):
    """A delete aimed at a file outside a ScadBuddy `Work/` folder (spec §6.3). Raised
    by the Bambuddy backend (Task 4); here so `sweep_content` can catch it per key."""
