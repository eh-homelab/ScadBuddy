from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.core.config import Config
from scadbuddy.core.metrics import Metrics, RenderOutcome, RenderStage
from scadbuddy.core.paths import (
    BUILTIN_PREFIX,
    SCHEMA_CACHE_NAME,
    SOURCE_NAME,
    DataPaths,
    model_path,
)
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import (
    declared_libraries,
    lock_at,
    model_search_path,
    search_path,
)
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.colours import colour_hex
from scadbuddy.render.glb import BoundingBox, write_glb
from scadbuddy.render.provenance import source_version
from scadbuddy.render.runner import OpenSCADError, cached_schema, render_3mf
from scadbuddy.render.schema import CustomizerSchema, ParamValue
from scadbuddy.render.solids import render_solids
from scadbuddy.render.split import ColourPart, split_by_material
from scadbuddy.render.thumbnail import PlateThumbnails, render_plate_thumbnails

logger = logging.getLogger(__name__)

JobState = Literal["pending", "running", "done", "failed"]

RAW_RENDER_NAME = "render.3mf"
MODEL_NAME = "model.3mf"
PREVIEW_NAME = "preview.glb"

# Material 0 is OpenSCAD's "Default": geometry no color() call reached.
UNCOLOURED_MATERIAL_INDEX = 0
UNCOLOURED_WARNING = "uncoloured geometry present; parts are not closed"
THUMBNAIL_TIMEOUT_WARNING = "plate thumbnail timed out; the 3MF carries no cover image"
THUMBNAIL_FAILED_WARNING = "plate thumbnail failed; the 3MF carries no cover image"
SUPERSEDED_ERROR = "superseded by a newer render before it started"


class PartInfo(BaseModel):
    name: str
    colour: str
    extruder: int
    watertight: bool


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

    def has_unfinished(self, slug: str) -> bool:
        """Is a render of ``slug`` queued or running?"""
        return any(
            job.slug == slug and job.state in ("pending", "running") for job in self.list_jobs()
        )

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


def unreadable_colour_warnings(
    schema: CustomizerSchema, params: Mapping[str, ParamValue]
) -> list[str]:
    """A warning for each colour parameter whose value is not a colour OpenSCAD reads
    (a malformed hex, an unknown name): no part can match it, so it gets no extruder."""
    warnings: list[str] = []
    for parameter in schema.parameters:
        if parameter.type != "color":
            continue
        value = params.get(parameter.name, parameter.initial)
        if colour_hex(value) is None:
            warnings.append(
                f"colour parameter {parameter.name!r} is {value!r}, not a colour; "
                "it gets no extruder"
            )
    return warnings


def extruder_order(
    parts: Sequence[ColourPart], schema: CustomizerSchema, params: Mapping[str, ParamValue]
) -> list[ColourPart]:
    """The split parts in extruder order: spec §7's "extruder 1 = first colour
    parameter".

    OpenSCAD numbers its materials in the order the geometry first uses each colour,
    not in parameter order, so a model that draws its second colour first would
    otherwise swap its extruders. Each part is matched to the first colour parameter,
    in declaration order, whose rendered value is that part's colour; parameters that
    share a value therefore share one extruder, and one no geometry uses gets none.
    Every part no parameter names -- a hard-coded colour, one computed from a
    parameter, the uncoloured Default -- follows, in OpenSCAD's material order."""
    ranks: dict[str, int] = {}
    for parameter in schema.parameters:
        if parameter.type != "color":
            continue
        colour = colour_hex(params.get(parameter.name, parameter.initial))
        if colour is not None:
            ranks.setdefault(colour, len(ranks))

    def rank(part: ColourPart) -> int:
        if part.material_index == UNCOLOURED_MATERIAL_INDEX:
            return len(ranks)
        return ranks.get(part.colour, len(ranks))

    return sorted(parts, key=rank)


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


async def plate_thumbnails(
    parts: Sequence[ColourPart], *, config: Config, executor: Executor | None = None
) -> tuple[PlateThumbnails | None, list[str]]:
    """The 3MF's cover images, under the same wall-clock budget as a render.

    Two separate reasons, and neither is the other's.

    OFF THE EVENT LOOP, because rasterising four images is seconds of numpy on a
    large mesh where writing the rest of the 3MF is milliseconds of XML. One loop
    serves the whole process, so a synchronous call would stall every other job's
    poll, `/healthz` and the second render worker — and §5.3's debounced preview
    submits these back to back on a slider drag. Everything else in this pipeline
    already yields: `render_3mf` and `render_solids` await a subprocess.

    BOUNDED, because §6.1's guarantee is that a job is time-bounded, and until
    now `SCADBUDDY_RENDER_TIMEOUT` delivered it by killing an `openscad` child.
    This step has no child to kill, and its cost rises with face count, so a mesh
    each OpenSCAD pass produced well inside its own budget can still rasterise
    for far longer than the whole job is supposed to take — with
    `render_concurrency` 2, two of those starve the queue. The budget is
    `render_timeout` rather than a new knob: this is the same job's time.

    The degradation is a 3MF with no cover images, NOT a failed job — the model
    is what the user asked for and the cover is a nicety. `write_bambu_3mf` then
    omits the png content type, the cover relationships and the plate's
    `thumbnail_file`/`top_file`/`pick_file` along with the images, so the package
    stays self-consistent rather than carrying dangling references.

    `wait_for` cannot cancel the thread it abandons, so the orphan keeps its core
    until it finishes. That is acceptable here and would not be for `openscad`:
    this work is O(faces) plus O(covered pixels) with no loop that can fail to
    terminate, whereas a `.scad` can legitimately spin forever. It is also why the
    queue passes its own `executor` (#116): on the loop's default one an orphan
    holds a slot `write_bambu_3mf` needs, so a backlog of slow covers could stall
    jobs whose own render finished in budget. On a dedicated pool a backlog only
    queues the next cover, which then times out like any other.
    """
    loop = asyncio.get_running_loop()
    faces = sum(len(part.mesh.faces) for part in parts)
    try:
        rendered = await asyncio.wait_for(
            loop.run_in_executor(executor, render_plate_thumbnails, parts),
            timeout=config.render_timeout,
        )
    except TimeoutError:
        logger.warning(
            "plate thumbnail render exceeded the budget; writing the 3MF without cover images",
            extra={"faces": faces},
        )
        return None, [THUMBNAIL_TIMEOUT_WARNING]
    except Exception:
        # Same degradation for a rasteriser bug (a degenerate face, an allocation
        # failure) as for a slow one: it may cost the cover, never the model (#116).
        logger.exception(
            "plate thumbnail render failed; writing the 3MF without cover images",
            extra={"faces": faces},
        )
        return None, [THUMBNAIL_FAILED_WARNING]
    return rendered, []


@dataclass(frozen=True)
class ModelSource:
    """What a render or a schema read works from: a `.scad`, where its derived
    schema is cached, and which revision the two belong to."""

    scad: Path
    schema_cache: Path
    version: str | None
    #: The checkouts of the libraries this revision declares, at the pins it goes
    #: with (#93): its whole OPENSCADPATH.
    library_path: tuple[Path, ...] = ()

    def configure(self, config: Config) -> Config:
        """``config`` for every openscad call made on this source."""
        return replace(config, library_path=self.library_path)


async def resolve_source(
    slug: str,
    requested: str | None,
    *,
    paths: DataPaths,
    history: ModelHistory | None,
) -> ModelSource:
    """Resolve a model to the source a render reads: the live one, or an export of
    an older revision.

    `last_commit` is read ONCE here, and the resolved revision comes back on the
    result so a caller does not have to ask again to stamp `model_version`.

    The export is an ordinary model directory under ``data/cache``, so the
    renderer -- including the wrapper `render_solids` drops next to the source --
    works on it unchanged, and nothing generated lands in the repository. Commits
    are immutable, so a populated export is never stale.
    """
    current = (
        await asyncio.to_thread(history.last_commit, model_path(slug))
        if history is not None and history.available
        else None
    )
    if requested is None or requested == current:
        return ModelSource(
            scad=paths.model_source(slug),
            schema_cache=paths.model_schema_cache(slug),
            version=current,
            # Off the loop: `model.json`, the lockfile and each checkout are reads
            # on the same PVC the history's calls are offloaded for.
            library_path=await asyncio.to_thread(model_search_path, paths, slug),
        )
    assert history is not None  # a requested revision implies a repository
    directory = paths.model_revision_dir(slug, requested)
    if (directory / SOURCE_NAME).is_file():
        # Mark it used, so `prune_revision_exports` evicts by LAST USE rather
        # than by export time and cannot take an old revision out from under a
        # render that is still browsing it.
        await asyncio.to_thread(_touch, directory)
    else:
        await asyncio.to_thread(_export_atomically, history, slug, requested, directory)
    # The lockfile as it was at that revision, not as it is now: an old revision
    # renders against the library versions it was written with.
    lock = await asyncio.to_thread(lock_at, history, requested)
    return ModelSource(
        scad=directory / SOURCE_NAME,
        schema_cache=directory / SCHEMA_CACHE_NAME,
        version=requested,
        library_path=await asyncio.to_thread(
            lambda: search_path(paths, declared_libraries(directory), lock)
        ),
    )


def _touch(directory: Path) -> None:
    with suppress(OSError):
        os.utime(directory)


def prune_revision_exports(paths: DataPaths, ttl: float, *, now: float | None = None) -> list[str]:
    """Evict revision exports nobody has rendered from in ``ttl`` seconds.

    `cache/schema/` needs none of this -- one file per slug, overwritten in
    place -- but every distinct `{slug, commit}` anyone opens "Customize this
    version" on writes a directory that is otherwise kept forever, on a 5 Gi
    PVC, for a feature whose whole point is browsing arbitrary old revisions.
    Losing one costs a `git archive`, so this mirrors the TTL sweep `jobs/`
    already gets, on the same clock and the same two trigger points.
    """
    root = paths.model_revisions
    if not root.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - ttl
    removed: list[str] = []
    for slug_dir in sorted(root.iterdir()):
        if not slug_dir.is_dir():
            continue
        for export in sorted(slug_dir.iterdir()):
            if not export.is_dir() or export.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(export, ignore_errors=True)
            removed.append(f"{slug_dir.name}/{export.name}")
        with suppress(OSError):
            slug_dir.rmdir()  # only when it emptied
    return removed


def _export_atomically(history: ModelHistory, slug: str, version: str, directory: Path) -> None:
    """Export beside the destination, then move it into place.

    Renders are debounced, so two of the same revision overlap routinely, and a
    reader that finds `model.scad` present while the other writer is still
    extracting `model.json` would render against half a revision.
    """
    staging = directory.with_name(f"{directory.name}.{os.getpid()}.{threading.get_ident()}")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        history.export(model_path(slug), version, staging)
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging, directory)
        except OSError:
            # Another writer got there first; its copy is just as good.
            if not (directory / SOURCE_NAME).is_file():
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def render_job(
    job: Job,
    *,
    config: Config,
    paths: DataPaths,
    history: ModelHistory | None = None,
    thumbnail_executor: Executor | None = None,
    metrics: Metrics | None = None,
) -> tuple[JobResult, list[str]]:
    def stage(name: RenderStage) -> AbstractContextManager[None]:
        return metrics.stage(name) if metrics is not None else nullcontext()

    # Resolved again rather than carried on the job: the model can be edited
    # between submit and render, and a stored "this one is live" flag would then
    # render newer source while claiming the older revision.
    with stage("source"):
        source = await resolve_source(job.slug, job.model_version, paths=paths, history=history)
    scad = source.scad
    # #90 stamps the model's own commit id, which `provenance.source_version` was
    # written to accept (a free string, never a structured field). The content hash
    # remains the answer when there is no repository to name a revision -- and it
    # hashes what was actually rendered, which for an old revision is its export,
    # not the live model directory. Reads every file under it; off the loop, like
    # the other two.
    version = source.version
    config = source.configure(config)
    with stage("source"):
        if version is None:
            version = await asyncio.to_thread(source_version, scad.parent)
        schema = await cached_schema(scad, source.schema_cache, config=config)
    work = paths.job_work_dir(job.id)
    work.mkdir(parents=True, exist_ok=True)

    with stage("render"):
        output = await render_3mf(scad, schema, job.params, work / RAW_RENDER_NAME, config=config)
    with stage("split"):
        preview_parts = extruder_order(
            split_by_material(work / RAW_RENDER_NAME), schema, job.params
        )
        if not preview_parts:
            raise OpenSCADError("the render produced no geometry", output.log_tail)
        preview_path = work / PREVIEW_NAME
        box = write_glb(preview_parts, preview_path)

    with stage("solids"):
        parts, warnings = await solid_parts(
            scad, schema, job.params, preview_parts, work, config=config
        )
    with stage("thumbnail"):
        thumbnails, thumbnail_warnings = await plate_thumbnails(
            parts, config=config, executor=thumbnail_executor
        )
    warnings += thumbnail_warnings
    warnings += unreadable_colour_warnings(schema, job.params)

    model_3mf = work / MODEL_NAME
    # A built-in's bare slug, as download_filename names the file: the id's
    # `builtin:` prefix is not something to show as the model's title.
    with stage("write"):
        await asyncio.to_thread(
            write_bambu_3mf,
            parts,
            model_3mf,
            thumbnails=thumbnails,
            model_name=job.slug.removeprefix(BUILTIN_PREFIX),
        )

    result = JobResult(
        model_3mf=str(model_3mf.relative_to(paths.root)),
        preview_glb=str(preview_path.relative_to(paths.root)),
        source_version=version,
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

#: Two requests with the same key are the same render: the slug, the revision the
#: submit resolved (`None` when there is no repository) and the parameters.
RenderKey = tuple[str, str | None, str]

#: What Retry-After says before any render has finished to measure.
INITIAL_RENDER_ESTIMATE = 10.0
#: Weight of the newest render in the running mean that sizes Retry-After.
RENDER_ESTIMATE_WEIGHT = 0.2


def render_key(slug: str, params: Mapping[str, ParamValue], model_version: str | None) -> RenderKey:
    return (slug, model_version, json.dumps(dict(params), sort_keys=True))


class QueueFullError(Exception):
    """SCADBUDDY_RENDER_QUEUE_MAX jobs are already waiting: the render is refused now
    rather than accepted behind work it would wait minutes for."""

    def __init__(self, depth: int, retry_after: int) -> None:
        super().__init__(
            f"the render queue is full ({depth} jobs waiting for a worker); "
            f"try again in {retry_after} s"
        )
        self.depth = depth
        self.retry_after = retry_after


@dataclass
class _Waiting:
    """A submitted job no worker has taken yet."""

    job: Job
    key: RenderKey
    enqueued: float = field(default_factory=time.monotonic)
    #: Submissions answered with this job: one, plus one per coalesced duplicate. A
    #: supersede releases one claim and the job is dropped only when none remain,
    #: so one tab moving on cannot cancel a render another tab is waiting for.
    claims: int = 1


class RenderQueue:
    """The in-process render queue: `render_concurrency` workers behind a bounded
    wait list.

    What keeps a render's latency bounded under load, in the order a submit meets it:

    - **Supersede.** The preview submits on a debounce while a slider moves, and each
      submit makes the previous one moot. It names that job, and if no worker has
      taken it yet it is dropped (failed as superseded) instead of rendered.
    - **Coalesce.** A submit identical to a job still waiting is answered with that
      job. Only waiting jobs: a running one has already read its source, and an
      edit since would be rendered stale.
    - **Admission.** Past `render_queue_max` waiting jobs a submit is refused with
      `QueueFullError` (503 + Retry-After) rather than queued behind work it cannot
      finish in time.
    - **Deadline.** A job that waited longer than `render_queue_timeout` for a worker
      is failed unrendered; whoever asked for it has long moved on.

    The wait list is in memory, like the `asyncio.Queue` it shadows: a restart fails
    every unfinished job anyway (`JobStore.fail_unfinished`).
    """

    def __init__(
        self,
        config: Config,
        paths: DataPaths,
        *,
        store: JobStore | None = None,
        render: RenderCallable | None = None,
        history: ModelHistory | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.history = history
        self.store = store or JobStore(paths)
        self.metrics = metrics if metrics is not None else Metrics()
        self.metrics.workers.set(config.render_concurrency)
        self.metrics.queue_capacity.set(config.render_queue_max)
        # The cover rasteriser's own threads, sized like the workers that feed it.
        self._thumbnails = ThreadPoolExecutor(
            max_workers=config.render_concurrency, thread_name_prefix="thumbnail"
        )
        self._render: RenderCallable = render or (
            lambda job: render_job(
                job,
                config=config,
                paths=paths,
                history=history,
                thumbnail_executor=self._thumbnails,
                metrics=self.metrics,
            )
        )
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._waiting: dict[str, _Waiting] = {}
        self._by_key: dict[RenderKey, str] = {}
        self._render_estimate = INITIAL_RENDER_ESTIMATE

    @property
    def depth(self) -> int:
        """Jobs waiting for a worker."""
        return len(self._waiting)

    async def start(self) -> None:
        self.paths.ensure()
        self.store.fail_unfinished()
        self.store.prune(self.config.job_ttl)
        prune_revision_exports(self.paths, self.config.job_ttl)
        self._workers = [
            asyncio.create_task(self._worker()) for _ in range(self.config.render_concurrency)
        ]

    async def aclose(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self.close_thumbnails()

    def close_thumbnails(self) -> None:
        """Release the cover pool. Not waited on: an abandoned cover thread cannot be
        interrupted, and shutdown must not pay for it. Queued covers are dropped."""
        self._thumbnails.shutdown(wait=False, cancel_futures=True)

    async def submit(
        self,
        slug: str,
        params: Mapping[str, ParamValue],
        *,
        model_version: str | None = None,
        supersedes: str | None = None,
    ) -> Job:
        """Queue a render, or hand back the waiting job that already is this render.

        ``supersedes`` names the job this submit replaces; it is dropped if no worker
        has taken it yet. Done first, so a slider drag is never refused for want of
        the room its own stale previews take up.

        Raises `QueueFullError` when the wait list is full."""
        key = render_key(slug, params, model_version)
        if supersedes is not None:
            previous = self._waiting.get(supersedes)
            if previous is not None and previous.key == key:
                return previous.job  # asked again for the very render it is waiting on
            self.supersede(supersedes)
        existing = self._by_key.get(key)
        if existing is not None:
            waiting = self._waiting[existing]
            waiting.claims += 1
            self.metrics.render_coalesced.inc()
            return waiting.job
        if len(self._waiting) >= self.config.render_queue_max:
            self.metrics.render_rejected.inc()
            raise QueueFullError(len(self._waiting), self.retry_after())
        job = Job(
            id=uuid.uuid4().hex,
            slug=slug,
            params=dict(params),
            model_version=model_version,
            created_at=_now(),
        )
        self.store.write(job)
        self._waiting[job.id] = _Waiting(job=job, key=key)
        self._by_key[key] = job.id
        self.metrics.queue_depth.set(len(self._waiting))
        self.metrics.render_submitted.inc()
        await self._queue.put(job.id)
        return job

    def supersede(self, job_id: str) -> bool:
        """Release one claim on a job no worker has taken; drop it with the last.

        A job already running or settled is left alone: it is nearly paid for, and
        killing an `openscad` mid-render to save the rest is not worth a job that
        half-wrote its work directory. Returns whether the job was dropped."""
        waiting = self._waiting.get(job_id)
        if waiting is None:
            return False
        waiting.claims -= 1
        if waiting.claims > 0:
            return False
        self._take(job_id)
        job = waiting.job
        job.state = "failed"
        job.error = SUPERSEDED_ERROR
        job.finished_at = _now()
        self.store.write(job)
        self._settled(waiting, "superseded")
        return True

    def retry_after(self) -> int:
        """Seconds a refused client should wait: about one render, the time it takes
        a worker to free a place in the wait list."""
        return max(1, math.ceil(self._render_estimate))

    async def join(self) -> None:
        await self._queue.join()

    def _take(self, job_id: str) -> _Waiting | None:
        """Remove a job from the wait list; `None` if it is no longer on it."""
        waiting = self._waiting.pop(job_id, None)
        if waiting is not None and self._by_key.get(waiting.key) == job_id:
            del self._by_key[waiting.key]
        self.metrics.queue_depth.set(len(self._waiting))
        return waiting

    def _settled(self, waiting: _Waiting, outcome: RenderOutcome) -> None:
        self.metrics.render_finished.labels(outcome).inc()
        self.metrics.job_latency.labels(outcome).observe(time.monotonic() - waiting.enqueued)

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                waiting = self._take(job_id)
                if waiting is not None:  # None: superseded while it waited
                    await self._run(waiting)
            finally:
                self._queue.task_done()

    async def _run(self, waiting: _Waiting) -> None:
        job = self.store.read(waiting.job.id)
        waited = time.monotonic() - waiting.enqueued
        self.metrics.queue_wait.observe(waited)
        deadline = self.config.render_queue_timeout
        if deadline and waited > deadline:
            job.state = "failed"
            job.error = (
                f"waited {waited:.0f} s for a render worker, past the {deadline:.0f} s "
                "SCADBUDDY_RENDER_QUEUE_TIMEOUT; the server is busy, try again"
            )
            job.finished_at = _now()
            self.store.write(job)
            self._settled(waiting, "expired")
            return

        job.state = "running"
        job.started_at = _now()
        self.store.write(job)
        outcome: RenderOutcome
        started = time.monotonic()
        self.metrics.running.inc()
        try:
            result, log_tail = await self._render(job)
        except OpenSCADError as error:
            outcome = "failed"
            job.state = "failed"
            job.error = str(error)
            job.log_tail = error.log_tail
        except Exception as error:  # the job carries the failure, the worker lives on
            outcome = "failed"
            job.state = "failed"
            job.error = f"{type(error).__name__}: {error}"
        else:
            outcome = "done"
            job.state = "done"
            job.result = result
            job.log_tail = log_tail
        finally:
            self.metrics.running.dec()
        elapsed = time.monotonic() - started
        self.metrics.render_duration.labels(outcome).observe(elapsed)
        self._render_estimate += RENDER_ESTIMATE_WEIGHT * (elapsed - self._render_estimate)
        job.finished_at = _now()
        self.store.write(job)
        self._settled(waiting, outcome)
        self.store.prune(self.config.job_ttl)
        prune_revision_exports(self.paths, self.config.job_ttl)
