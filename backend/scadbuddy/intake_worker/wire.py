"""What the intake worker and the CRUD service share (#6133, spec 2026-11-13 §7): the
queue, the workflow's names, and the bodies of ``/api/v1/internal/crud/intake/…``. It
imports no database or app code, so the worker, which holds no database secret, can use
it."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The task queue the intake worker polls, and the CRUD service starts checks on.
DEFAULT_CHECK_QUEUE: Final = "customer-files"
CHECK_WORKFLOW: Final = "CustomerFileCheck"
CHECK_ACTIVITY: Final = "customer_file_check"
FAILED_ACTIVITY: Final = "customer_file_failed"
#: The CRUD areas the worker calls (`api/crud_areas.py` `CRUD_AREAS["intake"]`).
INTAKE_AREAS: Final = frozenset({"sources"})
#: How long the staging object's presigned GET, and the preview's presigned PUT, last.
URL_TTL: Final = timedelta(minutes=5)
#: The largest preview PNG the CRUD service presigns a PUT for.
MAX_PREVIEW_BYTES: Final = 4 << 20
#: The most plates a file's facts list; a file with more is refused.
MAX_PLATES: Final = 64
#: The longest reason a check writes.
MAX_REASON: Final = 500
SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
COLOUR_PATTERN: Final = r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$"


def check_workflow_id(source_id: str) -> str:
    """One check per source at a time; a check run again later is a new run."""
    return f"customer-file-check:{source_id}"


class SourceRead(BaseModel):
    """``POST …/intake/sources/{id}/read``: the staged file to check, and a presigned GET
    of it. The URL's query carries its signature: never log it."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    filename: str
    size: int = Field(ge=1)
    media_type: str
    url: str
    #: The frozen copy's ETag: sent as ``If-Match``, which the URL signs.
    etag: str
    #: The frozen copy's SHA-256 as S3 computed it at the claim (base64), or None where the
    #: endpoint gave none; the download must hash to it.
    sha256: str | None = None


class PreviewAsk(BaseModel):
    """``POST …/intake/sources/{id}/preview``: the size of the preview PNG to PUT."""

    model_config = ConfigDict(extra="forbid")

    size: int = Field(ge=1, le=MAX_PREVIEW_BYTES)


class PreviewSlot(BaseModel):
    """A presigned PUT of ``<prefix>/staging/<upload id>.png``, with the headers it was
    signed for. The URL's query carries its signature: never log it."""

    model_config = ConfigDict(extra="forbid")

    url: str
    headers: dict[str, str]


class PlateFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1, le=10_000)
    triangles: int = Field(ge=0)
    #: The plate's objects' box, in mm: x, y, z.
    size_mm: tuple[float, float, float]
    colours: list[Annotated[str, Field(pattern=COLOUR_PATTERN)]] = Field(
        default_factory=list, max_length=64
    )

    @model_validator(mode="after")
    def _sizes_in_range(self) -> Self:
        if any(not 0 <= axis <= 1_000_000 for axis in self.size_mm):
            raise ValueError("a plate's size is out of range")
        return self


class FileFacts(BaseModel):
    """What the check found: the format by content, each plate, the triangles in all."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["3mf", "stl"]
    plates: list[PlateFacts] = Field(min_length=1, max_length=MAX_PLATES)
    triangles: int = Field(ge=0)


class CheckResult(BaseModel):
    """``PUT …/intake/sources/{id}/check``: ``ready`` with the facts, the SHA-256 and a
    preview already PUT; ``refused`` with the reason (the file is not one ScadBuddy
    prints); ``failed`` when the check itself could not finish (a timeout, a crash),
    which the shop can run again."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["ready", "refused", "failed"]
    reason: str | None = Field(default=None, max_length=MAX_REASON)
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    facts: FileFacts | None = None

    @model_validator(mode="after")
    def _what_each_outcome_carries(self) -> Self:
        if self.outcome == "ready" and (self.sha256 is None or self.facts is None):
            raise ValueError("a ready check carries the SHA-256 and the facts")
        if self.outcome == "refused" and not self.reason:
            raise ValueError("a refused check carries its reason")
        if self.outcome != "ready" and self.facts is not None:
            raise ValueError("only a ready check carries facts")
        return self


class CheckRecorded(BaseModel):
    """``PUT …/check``'s answer: whether the verdict was written (false for a source that
    stopped waiting meanwhile: its line removed, its file cleaned up)."""

    recorded: bool
