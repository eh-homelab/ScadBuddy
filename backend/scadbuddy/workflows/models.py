"""Payloads between the workflows and their activities (spec 2026-09-27 §3.4)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from scadbuddy.library.history import COMMIT_ID_PATTERN
from scadbuddy.library.slugs import MODEL_ID_PATTERN
from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.job_models import BomEntry, JobResult, OutputRecord, PipelineOutput, StepInfo
from scadbuddy.render.schema import ParamValue
from scadbuddy.template import Blob as Blob
from scadbuddy.template import Part as Part


def piece_key(slug: str, revision: str | None, file: str, params: Mapping[str, ParamValue]) -> str:
    """One openscad invocation (§3.4): the slug is in it because a revision is the
    template's own last commit, which one commit can give to many templates."""
    raw = json.dumps([slug, revision, file, dict(params)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def input_problem(slug: str, revision: str | None) -> str | None:
    """Why a workflow must not render ``slug`` at ``revision``, or None. The API
    validates both already; this is for a run started straight on the (unauthenticated,
    in-cluster) Temporal frontend, whose slug the worker would resolve into a path."""
    if not re.fullmatch(MODEL_ID_PATTERN, slug):
        return f"not a template id: {slug!r}"
    if revision is not None and not re.fullmatch(COMMIT_ID_PATTERN, revision):
        return f"not a revision: {revision!r}"
    return None


class PieceRequest(BaseModel):
    slug: str
    revision: str | None
    file: str = "model.scad"
    params: dict[str, ParamValue] = Field(default_factory=dict)
    #: For a piece with no revision, what stands in for one in its key: the job's own
    #: (`job:<id>`). A live source can change between two jobs, so such a piece is
    #: never shared, neither its blob directory nor its workflow (#642).
    scope: str | None = None
    #: Stored, so the Temporal payload carries it; checked against the other fields,
    #: because it names the child workflow that dedups the piece.
    piece_key: str

    @model_validator(mode="after")
    def _key_matches(self) -> Self:
        problem = input_problem(self.slug, self.revision)
        if problem is not None:
            raise ValueError(problem)
        if self.revision is not None and self.scope is not None:
            raise ValueError("a piece at a revision is shared, so it takes no scope")
        version = self.revision if self.revision is not None else self.scope
        expected = piece_key(self.slug, version, self.file, self.params)
        if self.piece_key != expected:
            raise ValueError(f"piece_key {self.piece_key} does not match its request")
        return self


class PrepareResult(BaseModel):
    version: str
    scad: str
    library_path: list[str] = Field(default_factory=list)
    schema_cache: str


class RenderMainResult(BaseModel):
    plates: int | None = None
    log_tail: list[str] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    diagnostics_dropped: int = 0
    notes: list[str] = Field(default_factory=list)
    missing_files: list[str] = Field(default_factory=list)
    returncode: int = 0
    duration_s: float = 0.0


class PieceResult(BaseModel):
    result: JobResult
    log_tail: list[str] = Field(default_factory=list)


class Failure(BaseModel):
    error: str
    log_tail: list[str] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    diagnostics_dropped: int = 0
    warnings: list[str] = Field(default_factory=list)


class PieceOutcome(BaseModel):
    """What a piece signals to a job that found it already running (§3.6: a child
    start has no id-conflict policy, so only the first job owns the child)."""

    result: PieceResult | None = None
    failure: Failure | None = None
    #: Which piece: one job waits on many (§3.4).
    piece_key: str = ""


class Projection(BaseModel):
    job_id: str
    slug: str
    #: No "pending": the workflow projects only once it runs; the API inserts the
    #: row as pending, and None leaves the row's state as it is.
    state: Literal["running", "done", "failed", "cancelled"] | None = None
    steps: list[StepInfo] | None = None
    result: JobResult | None = None
    log_tail: list[str] = Field(default_factory=list)
    failure: Failure | None = None
    pipeline_version: str = "default"
    #: The piece the result lives in; `project` adds the job's blob ref (Task 4).
    blob_key: str | None = None
    #: Every `ctx.output`, in order (§5.2); ``result`` is the first one's.
    outputs: list[PipelineOutput] = Field(default_factory=list)
    #: Every blob the job reads (pieces and outputs); `project` refs each on done.
    blob_keys: list[str] = Field(default_factory=list)


class PlateSize(BaseModel):
    """The plate a pipeline packs onto (spec §5.2). Task 2's `load_pipeline` reads it
    from the template; the fields are the plan's."""

    key: str
    width: float
    depth: float


class LoadRequest(BaseModel):
    slug: str
    revision: str | None


class LoadedPipeline(BaseModel):
    """What `load_pipeline` returns: recorded in the history, so a replay runs this
    source whatever the template holds by then (§3.4, §8.3)."""

    source: str
    file: str
    api: int
    #: sha256 of ``source``, or "default" (§3.2).
    version: str
    inputs_version: int
    ui_api: int | None = None
    plate: PlateSize


class PackItem(BaseModel):
    part: Part
    count: int = Field(default=1, ge=1)


class Placed(BaseModel):
    """Where one copy of a piece goes: its box's min corner, relative to the plate's
    content (the writer then centres the plate as it does today)."""

    piece_key: str
    x: float
    y: float


class LayoutPlate(BaseModel):
    items: list[Placed]


class Layout(BaseModel):
    """What `pack`/`plate_of` yield (§5.2). ``own``: one part alone, on the plates it
    laid out itself, written exactly as it rendered (§5.3)."""

    plates: list[LayoutPlate] = Field(default_factory=list)
    own: str | None = None


class PackRequest(BaseModel):
    items: list[PackItem]
    plate: PlateSize
    goal: str = "fewest_plates"


class OutputRequest(BaseModel):
    job_id: str
    index: int
    slug: str
    layout: Layout
    parts: list[Part]
    name: str | None
    bom: list[BomEntry]
    files: dict[str, str | Blob]
    plate_model: str | None = None
    record: OutputRecord


class OutputRef(BaseModel):
    index: int
    name: str | None


class TemplateCall(BaseModel):
    """`ctx.activity(name, …)` (§5.2): JSON arguments, `Blob`/`Part` as their dicts."""

    slug: str
    revision: str | None
    name: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float
    #: The job that holds (refs) the blob the call emits.
    job_id: str = ""
