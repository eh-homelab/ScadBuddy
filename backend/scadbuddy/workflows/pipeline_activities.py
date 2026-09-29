"""The pipeline's own activities (spec 2026-09-27 §5.2): packing parts onto plates,
and writing an output. Phase 4 Task 2 adds `load_pipeline` beside them."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.render.job_models import PipelineOutput
from scadbuddy.render.jobs import resolve_source
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps, _heartbeating
from scadbuddy.workflows.models import Layout, OutputRequest, PackRequest
from scadbuddy.workflows.outputs import build_output
from scadbuddy.workflows.packing import PackError, shelf_pack


def pack_layout(req: PackRequest) -> Layout:
    if req.goal != "fewest_plates":
        raise ApplicationError(
            f"pack goal {req.goal!r} arrives with Arrange (phase 5); use 'fewest_plates'",
            type="PackError",
            non_retryable=True,
        )
    try:
        return shelf_pack(req.items, req.plate)
    except PackError as error:
        raise ApplicationError(str(error), type="PackError", non_retryable=True) from None


class PipelineActivities:
    def __init__(self, deps: WorkerDeps) -> None:
        self.deps = deps
        #: `prepare`'s snapshot step, shared: a worker on the bambuddy store has no
        #: source of its own until it materializes the revision.
        self._render = RenderActivities(deps)

    def all(self) -> Sequence[Callable[..., Any]]:
        return [self.pack, self.write_output]

    async def model_dir(self, slug: str, revision: str | None) -> Path:
        """The template's directory at ``revision``: the live one, or its export."""
        d = self.deps
        await self._render._materialize(slug, revision)
        source = await resolve_source(
            slug, revision, paths=d.paths, history=d.history, fetcher=d.fetcher
        )
        return source.scad.parent

    @activity.defn(name="pack")
    async def pack(self, req: PackRequest) -> Layout:
        return pack_layout(req)

    @activity.defn(name="write_output")
    async def write_output(self, req: OutputRequest) -> PipelineOutput:
        """Meshes, thumbnails and a many-plate 3MF can outlast a short timeout, so it
        heartbeats, as the openscad stages do. Its budget in the workflow is
        `_openscad_timeout()` plus one `TRANSFER` per store move: each piece and each
        `Blob` it fetches, and the output it publishes."""
        model_dir = await self.model_dir(req.slug, req.record.revision)
        return await _heartbeating(
            asyncio.create_task(build_output(req, self.deps, model_dir=model_dir))
        )
