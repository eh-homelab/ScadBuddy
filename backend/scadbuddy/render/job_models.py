"""The render job as the API and every job store see it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.schema import ParamValue

JobState = Literal["pending", "running", "done", "failed", "cancelled"]
JobTableKind = Literal["render", "arrange"]
StepState = Literal["pending", "running", "done", "failed", "cancelled"]


class StepInfo(BaseModel):
    """One row of `render_jobs.steps`: what `ctx.progress` writes (spec §3.2)."""

    name: str
    state: StepState = "pending"
    done: int | None = None
    total: int | None = None


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
    #: The main render's ERROR/WARNING lines, parsed (#252). The per-colour solid
    #: passes re-run the same source and would only repeat them.
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    #: Diagnostics past the cap that ``diagnostics`` leaves out; 0 when it is all.
    diagnostics_dropped: int = 0
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
    #: What OpenSCAD reported, parsed (#252): the result's on success, the failed
    #: run's on failure -- a parser error is exactly when a client needs them.
    #: Stored where the job record lives: the job file, or `render_jobs` columns.
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    diagnostics_dropped: int = 0
    #: ScadBuddy's own warnings (#408): the result's on success, and on failure the
    #: ones the failed run can still give (`failed_render_warnings`), which have no
    #: result to live on. Stored beside `diagnostics`, for the same reason.
    warnings: list[str] = Field(default_factory=list)
    result: JobResult | None = None
    #: Which workflow `render-<id>` runs (spec §3.4): a render, or (phase 5) an arrange.
    kind: JobTableKind = "render"
    #: Template-owned inputs (spec §4.3). For a params-only template, `{"params": …}`;
    #: `params` is kept beside it through phase 1 and dropped by phase 2's migration.
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: sha256 of the pipeline source `load_pipeline` recorded, or "default" (§3.2).
    pipeline_version: str = "default"
    steps: list[StepInfo] = Field(default_factory=list)
    workflow_id: str | None = None
    #: Submitters still waiting on this job (coalesced identical requests).
    claims: int = 1
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
