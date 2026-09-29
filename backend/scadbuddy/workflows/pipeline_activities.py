"""The pipeline's own activities (spec 2026-09-27 §5.2): loading the template's
pipeline, packing parts onto plates, and writing an output."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.library.catalogue import ModelMeta
from scadbuddy.library.pipelines import (
    DEFAULT_PIPELINE_FILE,
    DEFAULT_PIPELINE_SOURCE,
    PIPELINE_API_SUPPORTED,
    inputs_version_of,
    pipeline_version_of,
)
from scadbuddy.render.job_models import PipelineOutput
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.plate import plate_for
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps, _heartbeating
from scadbuddy.workflows.models import (
    Layout,
    LoadedPipeline,
    LoadRequest,
    OutputRequest,
    PackRequest,
    PlateSize,
)
from scadbuddy.workflows.outputs import build_output
from scadbuddy.workflows.packing import PackError, shelf_pack


def _refuse(message: str) -> ApplicationError:
    return ApplicationError(message, type="PipelineApiError", non_retryable=True)


def plate_size(model: str | None) -> PlateSize:
    plate = plate_for(model)
    return PlateSize(key=plate.key, width=plate.usable.width, depth=plate.usable.depth)


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
        return [self.load_pipeline, self.pack, self.write_output]

    async def model_dir(self, slug: str, revision: str | None) -> Path:
        """The template's directory at ``revision``: the live one, or its export."""
        d = self.deps
        await self._render.materialize(slug, revision)
        source = await resolve_source(
            slug, revision, paths=d.paths, history=d.history, fetcher=d.fetcher
        )
        return source.scad.parent

    @activity.defn(name="load_pipeline")
    async def load_pipeline(self, req: LoadRequest) -> LoadedPipeline:
        """The template's pipeline source at ``req.revision``, read and never run: the
        workflow runs it in its sandbox, where what raises is a nondeterministic call
        (`os.getpid()`, `random.random()`, `datetime.now()`, `open()`), not an
        import (spec §3.6)."""
        directory = await self.model_dir(req.slug, req.revision)
        try:
            raw = json.loads(await asyncio.to_thread((directory / "model.json").read_text, "utf-8"))
        except (OSError, ValueError) as error:
            raise _refuse(f"model.json could not be read: {error}") from None
        try:
            meta = ModelMeta.model_validate(raw)
        except ValidationError as error:
            # The template's own data: a retry reads the same file.
            raise _refuse(f"model.json is not a template's: {error}") from None
        ui_api = meta.ui.api if meta.ui is not None else None
        plate = plate_size(None)
        if meta.pipeline_error is not None:
            raise _refuse(meta.pipeline_error)
        if meta.pipeline is None:
            return LoadedPipeline(
                source=DEFAULT_PIPELINE_SOURCE,
                file=DEFAULT_PIPELINE_FILE,
                api=1,
                version="default",
                inputs_version=0,
                ui_api=ui_api,
                plate=plate,
            )
        if meta.pipeline.api not in PIPELINE_API_SUPPORTED:
            majors = ", ".join(str(m) for m in PIPELINE_API_SUPPORTED)
            raise _refuse(
                f"{meta.pipeline.module} declares pipeline api {meta.pipeline.api}; "
                f"this ScadBuddy supports majors {majors}"
            )
        module = meta.pipeline.module
        try:
            # Resolved, as api/template_ui.py resolves ui/ files: a symlink out of the
            # template would put another file's contents into the workflow history.
            base = directory.resolve()
            path = (directory / module).resolve()
        except (OSError, RuntimeError) as error:  # a symlink loop is a RuntimeError
            raise _refuse(f"{module} could not be read: {error}") from None
        if not path.is_relative_to(base):
            raise _refuse(f"{module} points outside the template")
        try:
            source = await asyncio.to_thread(path.read_text, "utf-8")
        except FileNotFoundError:
            raise _refuse(f"{module} is missing from the template") from None
        except (OSError, UnicodeDecodeError) as error:
            # A directory with the module's name, or a file that is not UTF-8: the
            # template's fault, which a retry would only repeat.
            raise _refuse(f"{module} could not be read: {error}") from None
        return LoadedPipeline(
            source=source,
            file=module,
            api=meta.pipeline.api,
            version=pipeline_version_of(source),
            inputs_version=inputs_version_of(source),
            ui_api=ui_api,
            plate=plate,
        )

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
