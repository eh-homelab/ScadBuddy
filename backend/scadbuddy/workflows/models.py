"""Payloads between the workflows and their activities (spec 2026-09-27 §3.4)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.job_models import JobResult, StepInfo
from scadbuddy.render.schema import ParamValue


def piece_key(slug: str, revision: str | None, file: str, params: Mapping[str, ParamValue]) -> str:
    """One openscad invocation (§3.4): the slug is in it because a revision is the
    template's own last commit, which one commit can give to many templates."""
    raw = json.dumps([slug, revision, file, dict(params)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PieceRequest(BaseModel):
    slug: str
    revision: str | None
    file: str = "model.scad"
    params: dict[str, ParamValue] = Field(default_factory=dict)
    piece_key: str


class PrepareResult(BaseModel):
    version: str
    scad: str
    library_path: list[str] = Field(default_factory=list)
    schema_cache: str


class RenderMainResult(BaseModel):
    plates: int | None = 1
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
    state: Literal["running", "done", "failed", "cancelled"] | None = None
    steps: list[StepInfo] | None = None
    result: JobResult | None = None
    log_tail: list[str] = Field(default_factory=list)
    failure: Failure | None = None
    pipeline_version: str = "default"
    #: The piece the result lives in; `project` adds the job's blob ref (Task 4).
    blob_key: str | None = None
