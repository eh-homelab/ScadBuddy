"""What a library copy (#5355) takes, records and answers: the request, the rows'
shapes, and what crosses `LibraryCopy`'s history."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

LibraryCopyDirection = Literal["bb_to_sb", "sb_to_bb"]
#: How a walked file ended: still to do, copied, skipped because the target already
#: holds its bytes, or failed with ``reason``.
LibraryCopyFileStatus = Literal["pending", "copied", "skipped_same_hash", "failed"]
LibraryCopyRunStatus = Literal["running", "done", "failed"]

#: The most source files one copy walks: a whole library of more goes folder by folder.
MAX_COPY_FILES = 5000
#: Files copied per `library_copy_batch` activity, so its heartbeat timeout bounds one
#: file's transfer, not the whole job's.
BATCH_FILES = 20
#: The most file rows `GET /print/library-copies/{id}` lists; the counts cover them all.
MAX_LISTED_FILES = 500


class LibraryCopyRequest(BaseModel):
    """Which files and folders to copy, which way, and the folder they go under."""

    direction: LibraryCopyDirection
    file_ids: list[int] = Field(default_factory=list, max_length=MAX_COPY_FILES)
    folder_ids: list[int] = Field(default_factory=list, max_length=500)
    #: The target's folder the copies go under; ``None`` is its top level.
    target_folder_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _something_to_copy(self) -> Self:
        if not self.file_ids and not self.folder_ids:
            raise ValueError("name at least one file or folder to copy")
        self.file_ids = sorted(set(self.file_ids))
        self.folder_ids = sorted(set(self.folder_ids))
        return self


def copy_id(request: LibraryCopyRequest) -> str:
    """The job's id, and its workflow's: one per distinct request, so a second start of
    the same request joins the running one, and a later one runs it again (a no-op for
    every file whose bytes already landed)."""
    canonical = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return f"library-copy-{hashlib.sha256(canonical.encode()).hexdigest()[:24]}"


class LibraryCopyStarted(BaseModel):
    copy_id: str


class LibraryCopyFileResult(BaseModel):
    source_id: int
    #: The folders between the selected root and the file, joined by ``/``; ``""`` for a
    #: file selected on its own.
    path: str
    name: str
    size: int | None = None
    status: LibraryCopyFileStatus
    reason: str | None = None
    #: The copy's id at the target, once copied.
    target_id: int | None = None
    target_folder_id: int | None = None


class LibraryCopyCounts(BaseModel):
    total: int = 0
    pending: int = 0
    copied: int = 0
    skipped_same_hash: int = 0
    failed: int = 0


class LibraryCopySummary(LibraryCopyCounts):
    copy_id: str
    direction: LibraryCopyDirection
    request: LibraryCopyRequest
    status: LibraryCopyRunStatus
    #: Whether its workflow still runs (`status` is the rows' word; this is Temporal's).
    running: bool = False
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class LibraryCopyStatus(LibraryCopySummary):
    #: At most `MAX_LISTED_FILES` of them, failed and pending first.
    files: list[LibraryCopyFileResult] = Field(default_factory=list)


# What crosses the workflow's history.


class LibraryCopyInput(BaseModel):
    copy_id: str
    request: LibraryCopyRequest
    #: Set once the plan activity ran, so a continue-as-new does not walk again.
    planned: bool = False
    batches_done: int = 0


class LibraryCopyPlan(BaseModel):
    total: int


class LibraryCopyBatchInput(BaseModel):
    copy_id: str
    request: LibraryCopyRequest
    limit: int = BATCH_FILES


class LibraryCopyBatch(BaseModel):
    copied: int
    skipped: int
    failed: int
    remaining: int


class LibraryCopyFinishInput(BaseModel):
    copy_id: str
    error: str | None = None


class LibraryCopyResult(BaseModel):
    batches_done: int
    counts: LibraryCopyCounts
