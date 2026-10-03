"""Payloads between the workflows and their activities (spec 2026-09-27 §3.4)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scadbuddy.library.history import COMMIT_ID_PATTERN
from scadbuddy.library.libraries import ModelLibrary
from scadbuddy.library.slugs import MODEL_ID_PATTERN
from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.job_models import Job, JobResult, JobTableKind, StepInfo
from scadbuddy.render.schema import ParamValue


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
        if self.revision is None and self.scope is None:
            raise ValueError("a piece with no revision needs a scope, or jobs would share it")
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
    #: The pins `library_path` holds (#169), for the result to record.
    libraries: list[ModelLibrary] = Field(default_factory=list)


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


#: `TemplatePipeline`'s first step and its claim count (#1053): local activities, so
#: they never wait behind openscad runs for the worker's activity slots.
ACCEPT_ACTIVITY = "render_accept"
CLAIMS_ACTIVITY = "render_claims"
#: The Update a supersede or a withdrawal sends the job's execution.
RELEASE_UPDATE = "release"
#: `render_accept`'s refusal: `render_queue_max` jobs already wait.
QUEUE_FULL = "QueueFull"


class RenderStart(BaseModel):
    """`render-<render_key>`'s input (spec 2026-10-01 §4.5): what the route resolved."""

    model_config = ConfigDict(protected_namespaces=())

    slug: str
    params: dict[str, ParamValue] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    model_version: str | None = None
    render_key: str
    kind: JobTableKind = "render"
    #: `render_queue_max` when the request was made; 0 is no limit.
    max_pending: int = 0
    search_attributes: bool = False


class AcceptRender(BaseModel):
    start: RenderStart
    workflow_id: str
    run_id: str


class RenderAnswer(BaseModel):
    """The `accepted` Update's answer: the job, or why there is none."""

    job: Job | None = None
    #: A later request that joined the open execution, with one more claim.
    coalesced: bool = False
    #: How many jobs wait, when the queue was full and nothing was started.
    queue_full: int | None = None
    #: The execution's last claim was released: it is closing, and starts again.
    closing: bool = False


class ReleaseAnswer(BaseModel):
    #: The job, cancelled by this release; None while other claims remain.
    cancelled: Job | None = None
