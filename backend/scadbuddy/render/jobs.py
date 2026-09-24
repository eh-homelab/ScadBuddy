from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.glb import BoundingBox, write_glb
from scadbuddy.render.runner import OpenSCADError, cached_schema, render_3mf
from scadbuddy.render.schema import CustomizerSchema, ParamValue
from scadbuddy.render.solids import render_solids
from scadbuddy.render.split import ColourPart, split_by_material

JobState = Literal["pending", "running", "done", "failed"]

RAW_RENDER_NAME = "render.3mf"
MODEL_NAME = "model.3mf"
PREVIEW_NAME = "preview.glb"

# Material 0 is OpenSCAD's "Default": geometry no color() call reached.
UNCOLOURED_MATERIAL_INDEX = 0
UNCOLOURED_WARNING = "uncoloured geometry present; parts are not closed"


class PartInfo(BaseModel):
    name: str
    colour: str
    extruder: int
    watertight: bool


class JobResult(BaseModel):
    model_3mf: str
    preview_glb: str
    parts: list[PartInfo]
    bbox_mm: BoundingBox
    colors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Job(BaseModel):
    id: str
    slug: str
    params: dict[str, ParamValue] = Field(default_factory=dict)
    state: JobState = "pending"
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log_tail: list[str] = Field(default_factory=list)
    error: str | None = None
    result: JobResult | None = None


def _now() -> datetime:
    return datetime.now(UTC)


class JobStore:
    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths

    def write(self, job: Job) -> None:
        self.paths.jobs.mkdir(parents=True, exist_ok=True)
        self.paths.job_file(job.id).write_text(
            json.dumps(job.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )

    def read(self, job_id: str) -> Job:
        return Job.model_validate_json(self.paths.job_file(job_id).read_text(encoding="utf-8"))

    def list_jobs(self) -> list[Job]:
        if not self.paths.jobs.is_dir():
            return []
        jobs = [
            Job.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.paths.jobs.glob("*.json"))
        ]
        return sorted(jobs, key=lambda job: job.created_at)

    def delete(self, job_id: str) -> None:
        self.paths.job_file(job_id).unlink(missing_ok=True)
        shutil.rmtree(self.paths.job_work_dir(job_id), ignore_errors=True)

    def fail_unfinished(self) -> list[Job]:
        failed: list[Job] = []
        for job in self.list_jobs():
            if job.state not in ("pending", "running"):
                continue
            job.state = "failed"
            job.finished_at = _now()
            job.error = "interrupted by a restart"
            self.write(job)
            failed.append(job)
        return failed

    def prune(self, ttl: float, *, now: datetime | None = None) -> list[str]:
        cutoff = (now or _now()).timestamp() - ttl
        removed: list[str] = []
        for job in self.list_jobs():
            stamp = job.finished_at or job.created_at
            if stamp.timestamp() < cutoff:
                self.delete(job.id)
                removed.append(job.id)
        return removed


async def solid_parts(
    scad_path: Path,
    schema: CustomizerSchema,
    params: Mapping[str, ParamValue],
    preview_parts: Sequence[ColourPart],
    work_dir: Path,
    *,
    config: Config,
) -> tuple[list[ColourPart], list[str]]:
    """The parts the 3MF is written from: one closed solid per colour where OpenSCAD can
    give us one, the open split mesh where it cannot."""
    if any(part.material_index == UNCOLOURED_MATERIAL_INDEX for part in preview_parts):
        return list(preview_parts), [UNCOLOURED_WARNING]
    solids = await render_solids(
        scad_path,
        schema,
        params,
        [part.colour for part in preview_parts],
        work_dir,
        config=config,
    )
    parts = [
        part if part.colour not in solids.meshes else replace(part, mesh=solids.meshes[part.colour])
        for part in preview_parts
    ]
    return parts, solids.warnings


async def render_job(job: Job, *, config: Config, paths: DataPaths) -> tuple[JobResult, list[str]]:
    scad = paths.model_source(job.slug)
    schema = await cached_schema(scad, paths.model_meta(job.slug), config=config)
    work = paths.job_work_dir(job.id)
    work.mkdir(parents=True, exist_ok=True)

    output = await render_3mf(scad, schema, job.params, work / RAW_RENDER_NAME, config=config)
    preview_parts = split_by_material(work / RAW_RENDER_NAME)
    if not preview_parts:
        raise OpenSCADError("the render produced no geometry", output.log_tail)

    preview_path = work / PREVIEW_NAME
    box = write_glb(preview_parts, preview_path)

    parts, warnings = await solid_parts(
        scad, schema, job.params, preview_parts, work, config=config
    )
    model_path = work / MODEL_NAME
    # Off the event loop. Writing the 3MF used to be XML and a zip — milliseconds
    # — but it now rasterises the plate cover images too, which is seconds of
    # numpy on a large mesh. Everything else in this pipeline already yields:
    # `render_3mf` and `render_solids` await a subprocess. A synchronous call
    # here would stall every other job's poll, `/healthz`, and the other render
    # worker, since one loop serves them all. Same `asyncio.to_thread` the font
    # service uses for its blocking fontconfig calls.
    await asyncio.to_thread(write_bambu_3mf, parts, model_path, model_name=job.slug)

    result = JobResult(
        model_3mf=str(model_path.relative_to(paths.root)),
        preview_glb=str(preview_path.relative_to(paths.root)),
        parts=[
            PartInfo(
                name=part.name,
                colour=part.colour,
                extruder=index,
                watertight=part.watertight,
            )
            for index, part in enumerate(parts, start=1)
        ],
        bbox_mm=box,
        colors=[part.colour for part in parts],
        warnings=warnings,
    )
    return result, output.log_tail


RenderCallable = Callable[[Job], Awaitable[tuple[JobResult, list[str]]]]


class RenderQueue:
    def __init__(
        self,
        config: Config,
        paths: DataPaths,
        *,
        store: JobStore | None = None,
        render: RenderCallable | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.store = store or JobStore(paths)
        self._render: RenderCallable = render or (
            lambda job: render_job(job, config=config, paths=paths)
        )
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.paths.ensure()
        self.store.fail_unfinished()
        self.store.prune(self.config.job_ttl)
        self._workers = [
            asyncio.create_task(self._worker()) for _ in range(self.config.render_concurrency)
        ]

    async def aclose(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def submit(self, slug: str, params: Mapping[str, ParamValue]) -> Job:
        job = Job(id=uuid.uuid4().hex, slug=slug, params=dict(params), created_at=_now())
        self.store.write(job)
        await self._queue.put(job.id)
        return job

    async def join(self) -> None:
        await self._queue.join()

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._run(job_id)
            finally:
                self._queue.task_done()

    async def _run(self, job_id: str) -> None:
        job = self.store.read(job_id)
        job.state = "running"
        job.started_at = _now()
        self.store.write(job)
        try:
            result, log_tail = await self._render(job)
        except OpenSCADError as error:
            job.state = "failed"
            job.error = str(error)
            job.log_tail = error.log_tail
        except Exception as error:  # the job carries the failure, the worker lives on
            job.state = "failed"
            job.error = f"{type(error).__name__}: {error}"
        else:
            job.state = "done"
            job.result = result
            job.log_tail = log_tail
        job.finished_at = _now()
        self.store.write(job)
        self.store.prune(self.config.job_ttl)
