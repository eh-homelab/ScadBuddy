"""The render job as the API and every job store see it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.schema import ParamValue

JobState = Literal["pending", "running", "done", "failed"]


class PartInfo(BaseModel):
    name: str
    colour: str
    extruder: int
    watertight: bool


class PlateInfo(BaseModel):
    """One plate of a multi-plate render (spec §6.4): what the customizer checks
    against the printer, plate by plate."""

    #: 1-based, the ``plate_id`` a print of it queues.
    index: int
    bbox_mm: BoundingBox
    #: The filament colours this plate uses, in extruder order.
    colors: list[str]


class JobResult(BaseModel):
    model_3mf: str
    preview_glb: str
    #: The model's sources as this render read them. Taken here rather than when the
    #: output is saved: Generate persists a render that already happened, and the
    #: files on the PVC can be edited in between.
    #: Empty only on a job written before this field existed — job files outlive a
    #: deploy on the PVC and the queue validates every one at startup, so a required
    #: field here would turn an upgrade into a crash loop rather than one bad job.
    source_version: str = ""
    parts: list[PartInfo]
    bbox_mm: BoundingBox
    colors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    #: The template's own `NOTE:`/`WARNING:` echoes (#285): what it changed from the
    #: parameters it was given, say a size capped to fit the plate. Defaulted, like
    #: `source_version`, so a result stored before the field existed still loads.
    notes: list[str] = Field(default_factory=list)
    #: Every plate of a template that asks for more than one (spec §6.4). Empty for
    #: the ordinary one-plate render, whose plate is ``bbox_mm`` and ``colors``.
    plates: list[PlateInfo] = Field(default_factory=list)


class Job(BaseModel):
    # `model_version` is the name #90 asks for on the wire; without this pydantic
    # warns that it collides with its own `model_` namespace.
    model_config = ConfigDict(protected_namespaces=())

    id: str
    slug: str
    params: dict[str, ParamValue] = Field(default_factory=dict)
    # The models-repository commit this render read. Carried onto the output it
    # produces, so an output can always name the revision it came from (#80/#90).
    model_version: str | None = None
    state: JobState = "pending"
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log_tail: list[str] = Field(default_factory=list)
    error: str | None = None
    result: JobResult | None = None
    #: Which try this is, as the store that handed the job to a worker numbered it.
    #: Not on the wire: it is how `finish` tells the attempt that still holds a job
    #: from one whose lease was reaped and retried.
    _attempt: int = PrivateAttr(default=0)

    @property
    def attempt(self) -> int:
        return self._attempt

    def claimed(self, attempt: int) -> Job:
        self._attempt = attempt
        return self


def now() -> datetime:
    return datetime.now(UTC)
