from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
import shutil
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import (
    AbstractContextManager,
    AsyncExitStack,
    asynccontextmanager,
    contextmanager,
    nullcontext,
    suppress,
)
from dataclasses import dataclass, replace
from pathlib import Path

from scadbuddy.core.config import Config
from scadbuddy.core.events import EventBus, JobEvent, JobKind, JobProgress, emit
from scadbuddy.core.metrics import Metrics, RenderOutcome, RenderStage
from scadbuddy.core.paths import (
    BUILTIN_PREFIX,
    SCHEMA_CACHE_NAME,
    SOURCE_NAME,
    DataPaths,
    model_path,
)
from scadbuddy.library.assets import AssetStore, file_assets
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import (
    CheckoutGate,
    model_search_path,
    require_checkouts,
    revision_search_path,
)
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.colours import colour_hex
from scadbuddy.render.glb import write_glb
from scadbuddy.render.job_models import Job as Job
from scadbuddy.render.job_models import JobResult as JobResult
from scadbuddy.render.job_models import JobState as JobState
from scadbuddy.render.job_models import PartInfo as PartInfo
from scadbuddy.render.job_models import now as _now
from scadbuddy.render.job_store import SUPERSEDED_ERROR as SUPERSEDED_ERROR
from scadbuddy.render.job_store import JobBackend, Listener, render_key
from scadbuddy.render.job_store import JobNotFoundError as JobNotFoundError
from scadbuddy.render.job_store import JobStore as JobStore
from scadbuddy.render.job_store import QueueFullError as QueueFullError
from scadbuddy.render.provenance import source_version
from scadbuddy.render.runner import OpenSCADError, cached_schema, render_3mf
from scadbuddy.render.schema import CustomizerSchema, ParamValue
from scadbuddy.render.solids import STAGED_ASSET_PREFIX, render_solids
from scadbuddy.render.split import ColourPart, split_by_material
from scadbuddy.render.thumbnail import PlateThumbnails, render_plate_thumbnails

# The `X as X` imports above are re-exports: the job models and the file store
# lived here before the stores were split out, and routes and tests import them here.

logger = logging.getLogger(__name__)

#: The event each way a job leaves the queue is published as. An expired job is a
#: failed one to its subscribers; a superseded one gets its own kind, so a client
#: still following it knows a newer render replaced it rather than that it broke.
OUTCOME_EVENT_KINDS: dict[RenderOutcome, JobKind] = {
    "done": "job.done",
    "failed": "job.failed",
    "expired": "job.failed",
    "superseded": "job.superseded",
}

RAW_RENDER_NAME = "render.3mf"
MODEL_NAME = "model.3mf"
PREVIEW_NAME = "preview.glb"

# Material 0 is OpenSCAD's "Default": geometry no color() call reached.
UNCOLOURED_MATERIAL_INDEX = 0
UNCOLOURED_WARNING = "uncoloured geometry present; parts are not closed"
THUMBNAIL_TIMEOUT_WARNING = "plate thumbnail timed out; the 3MF carries no cover image"
THUMBNAIL_FAILED_WARNING = "plate thumbnail failed; the 3MF carries no cover image"
MISSING_FILE_WARNING = "OpenSCAD could not open {name}; the model rendered without it"


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


@contextmanager
def staged_assets(
    schema: CustomizerSchema,
    params: Mapping[str, ParamValue],
    model_dir: Path,
    store: AssetStore,
) -> Iterator[dict[str, ParamValue]]:
    """``params`` with each uploaded file copied beside the model (#204).

    OpenSCAD resolves `import()` and `surface()` relative to the file that calls
    them, so the copy goes into the model's own directory -- where every wrapper
    render of §6.3 runs too, which is what keeps the closed parts from silently
    losing the picture. The name is generated and bare, so a template that guards
    its file parameter against paths still takes it. Each render gets its own
    copies: two renders of one model overlap routinely, and a shared name would be
    deleted from under the one still running.
    """
    staged = dict(params)
    created: list[Path] = []
    try:
        for name, meta in file_assets(schema, params, store, model_dir).items():
            target = model_dir / f"{STAGED_ASSET_PREFIX}{secrets.token_hex(8)}.{meta.kind}"
            shutil.copyfile(store.blob_path(meta), target)
            created.append(target)
            staged[name] = target.name
        yield staged
    finally:
        for path in created:
            path.unlink(missing_ok=True)


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
            # Off the loop: `model.json` and each checkout are reads
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
    # The pins that revision declares, not the live model's: an old revision
    # renders against the library versions it was written with.
    return ModelSource(
        scad=directory / SOURCE_NAME,
        schema_cache=directory / SCHEMA_CACHE_NAME,
        version=requested,
        library_path=await asyncio.to_thread(
            revision_search_path, history, paths, directory, requested
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


@asynccontextmanager
async def _library_lease(
    checkouts: CheckoutGate | None, holder: str, library_path: Sequence[Path]
) -> AsyncIterator[None]:
    """A lease on the checkouts a render resolved, when there are any to hold.
    Released when the attempt's render ends however it ends -- done, failed,
    cancelled -- and per attempt: a retry of the same job after a lapsed lease
    holds its own, so the first attempt finishing late never releases it."""
    if checkouts is None or not library_path:
        yield
        return
    async with checkouts.rendering(holder, library_path):
        # A removal that ran between resolving and leasing took one away.
        require_checkouts(library_path)
        yield


def attempt_work_dir(paths: DataPaths, job: Job) -> Path:
    """Where this attempt at ``job`` writes its files.

    A retry gets a directory of its own. A job is only retried when its worker's
    lease lapsed, and a lapsed lease does not prove the worker died -- a stalled
    event loop or a partition from the database looks the same -- so the first
    attempt may still be writing `model.3mf` when the retry starts. Separate
    directories mean neither can splice into the other's files, and the result
    that `finish` records (only the attempt still holding the job) names its own.
    The first attempt keeps the plain layout; `JobStore.delete` removes them all.
    """
    base = paths.job_work_dir(job.id)
    return base if job.attempt <= 1 else base / f"attempt-{job.attempt}"


async def render_job(
    job: Job,
    *,
    config: Config,
    paths: DataPaths,
    history: ModelHistory | None = None,
    thumbnail_executor: Executor | None = None,
    metrics: Metrics | None = None,
    checkouts: CheckoutGate | None = None,
    on_stage: Callable[[RenderStage], None] | None = None,
) -> tuple[JobResult, list[str]]:
    def stage(name: RenderStage) -> AbstractContextManager[None]:
        if on_stage is not None:
            on_stage(name)
        return metrics.stage(name) if metrics is not None else nullcontext()

    # Resolved again rather than carried on the job: the model can be edited
    # between submit and render, and a stored "this one is live" flag would then
    # render newer source while claiming the older revision.
    async with AsyncExitStack() as held:
        with stage("source"):
            source = await resolve_source(job.slug, job.model_version, paths=paths, history=history)
            scad = source.scad
            # #90 stamps the model's own commit id, which `provenance.source_version`
            # was written to accept (a free string, never a structured field). The
            # content hash remains the answer when there is no repository to name a
            # revision -- and it hashes what was actually rendered, which for an old
            # revision is its export, not the live model directory. Reads every file
            # under it; off the loop, like the other two. One timed stage for all of
            # it: resolving the source and deriving its schema are the same step.
            version = source.version
            config = source.configure(config)
            if version is None:
                version = await asyncio.to_thread(source_version, scad.parent)
            # Held from here for every openscad run below -- the schema derivation
            # included: those are what read the checkouts on OPENSCADPATH, and a
            # removal must not take one out from under them (#253).
            await held.enter_async_context(_library_lease(checkouts, job.id, source.library_path))
            schema = await cached_schema(scad, source.schema_cache, config=config)
        work = attempt_work_dir(paths, job)
        work.mkdir(parents=True, exist_ok=True)

        with staged_assets(schema, job.params, scad.parent, AssetStore(paths.assets)) as params:
            with stage("render"):
                output = await render_3mf(
                    scad, schema, params, work / RAW_RENDER_NAME, config=config
                )
            with stage("split"):
                preview_parts = extruder_order(
                    split_by_material(work / RAW_RENDER_NAME), schema, params
                )
                if not preview_parts:
                    raise OpenSCADError(
                        "the render produced no geometry",
                        output.log_tail,
                        diagnostics=output.diagnostics,
                        diagnostics_dropped=output.diagnostics_dropped,
                    )
                preview_path = work / PREVIEW_NAME
                box = write_glb(preview_parts, preview_path)

            with stage("solids"):
                parts, warnings = await solid_parts(
                    scad, schema, params, preview_parts, work, config=config
                )
    # Exit 0 with the picture missing is otherwise invisible: the preview simply
    # has no overlay, and nothing says why.
    warnings = [
        *(MISSING_FILE_WARNING.format(name=name) for name in output.missing_files),
        *warnings,
    ]
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
        diagnostics=list(output.diagnostics),
        diagnostics_dropped=output.diagnostics_dropped,
        notes=list(output.notes),
    )
    return result, output.log_tail


RenderCallable = Callable[[Job], Awaitable[tuple[JobResult, list[str]]]]

#: What Retry-After says before any render has finished to measure.
INITIAL_RENDER_ESTIMATE = 10.0
#: Weight of the newest render in the running mean that sizes Retry-After.
RENDER_ESTIMATE_WEIGHT = 0.2


class RenderQueue:
    """The render queue: every submit is accepted, and `render_concurrency` workers
    per process render them oldest first.

    Where the jobs live is the `store` (`job_store.JobBackend`): JSON files and an
    in-process wait list by default, or Postgres when ``SCADBUDDY_DATABASE_URL`` is
    set, where accepted jobs survive a restart and several replicas can share the
    queue. What keeps a render's latency down without ever refusing one:

    - **Supersede.** The preview submits on a debounce while a slider moves, and each
      submit makes the previous one moot. It names that job, and if no worker has
      taken it yet it is dropped (failed as superseded) instead of rendered.
    - **Coalesce.** A submit identical to a job still waiting is answered with that
      job. Only waiting jobs: a running one has already read its source, and an
      edit since would be rendered stale.
    - **Deadline** (optional, `render_queue_timeout`). A job that waited longer than
      that for a worker is failed unrendered rather than rendered for nobody.
    - **Leases** (Postgres). A worker heartbeats its job; one whose worker died is
      requeued after `render_lease_timeout`, up to `render_max_attempts` tries.
    - **Wake-ups.** An idle worker waits for a submit in this process, or (Postgres)
      for the NOTIFY any replica's submit sends, which one LISTEN connection per
      process turns into the same wake-up. While that connection is up the poll is
      only a fallback, every `render_fallback_poll_interval` (30 s); while it is
      down, or with the file store, it is `render_poll_interval`.

    **Admission is off by default**: `render_queue_max` (SCADBUDDY_RENDER_QUEUE_MAX)
    0 accepts every render. Set, a submit that would be a new job past that many
    waiting is refused with `QueueFullError` (503 + Retry-After), after superseding
    frees its place and never when it coalesces.

    The depth and latency SLO targets are not limits: they are exported beside the
    measurements so alerts can compare the two.
    """

    def __init__(
        self,
        config: Config,
        paths: DataPaths,
        *,
        store: JobBackend | None = None,
        render: RenderCallable | None = None,
        history: ModelHistory | None = None,
        metrics: Metrics | None = None,
        events: EventBus | None = None,
        checkouts: CheckoutGate | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.history = history
        #: Told of every state a job enters (`job.*`), whichever path moved it.
        self.events = events
        self.store: JobBackend = store if store is not None else JobStore(paths)
        self.metrics = metrics if metrics is not None else Metrics()
        self.metrics.workers.set(config.render_concurrency)
        # So a deployment can tell the queue is where it was configured to be: an
        # image that predates the Postgres store, or a missing URL, reads "files".
        self.metrics.store_info.labels(self.store.backend).set(1)
        self.metrics.queue_depth_slo.set(config.render_queue_depth_slo)
        self.metrics.queue_max.set(config.render_queue_max)
        self.metrics.latency_slo.set(config.render_latency_slo)
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
                checkouts=checkouts,
                on_stage=lambda stage: emit(
                    self.events, JobProgress(job_id=job.id, slug=job.slug, stage=stage)
                ),
            )
        )
        self._tasks: list[asyncio.Task[None]] = []
        # Set on every submit, and (Postgres) on every NOTIFY from any replica's, so
        # an idle worker claims at once rather than at its next poll.
        self._wakeup = asyncio.Event()
        #: What wakes the workers for other processes' jobs; `None` with the file store.
        self.listener: Listener | None = None
        self._listening = False
        self._listened_before = False
        self._busy = 0
        #: Worker seconds per render, smoothed: what Retry-After says on a 503.
        self._render_estimate = INITIAL_RENDER_ESTIMATE

    async def start(self) -> None:
        self.paths.ensure()
        await asyncio.to_thread(self.store.open)
        # Past this point the store holds resources (the Postgres pool's
        # connections and threads). If the rest of startup fails, release them
        # here: the caller's `aclose` is typically in a `finally` that a failed
        # start never reaches, and a process that builds many apps -- the test
        # suite -- would otherwise leak a pool per failure.
        try:
            abandoned = await asyncio.to_thread(self.store.abandon_orphans)
            await self._prune()
        except BaseException:
            await asyncio.to_thread(self.store.close)
            raise
        for job in abandoned:
            self._announce(job, "job.failed")
        self._tasks = [
            asyncio.create_task(self._worker()) for _ in range(self.config.render_concurrency)
        ]
        self._tasks.append(asyncio.create_task(self._reaper()))
        self.listener = self.store.listener(
            on_notify=self._wakeup.set,
            on_state=self._listener_state,
            check_interval=self.config.render_fallback_poll_interval,
        )
        if self.listener is not None:
            self._tasks.append(asyncio.create_task(self.listener.run()))

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.close_thumbnails()
        await asyncio.to_thread(self.store.close)

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

        Raises `QueueFullError` when SCADBUDDY_RENDER_QUEUE_MAX (`render_queue_max`)
        is set and that many renders already wait; callers turn it into a 503.
        With the default of 0 it never raises.

        ``supersedes`` names the job this submit replaces; it is dropped if no worker
        has taken it yet."""
        job = Job(
            id=uuid.uuid4().hex,
            slug=slug,
            params=dict(params),
            model_version=model_version,
            created_at=_now(),
        )
        try:
            submitted = await asyncio.to_thread(
                self.store.submit,
                job,
                render_key(slug, params, model_version),
                supersedes=supersedes,
                max_pending=self.config.render_queue_max,
            )
        except QueueFullError as error:
            self.metrics.render_rejected.inc()
            raise QueueFullError(error.depth, self.retry_after()) from None
        if submitted.superseded is not None:
            self._settled(submitted.superseded, "superseded")
        if submitted.coalesced:
            # The answer is a job already waiting, whose `job.pending` went out when
            # it was submitted: nothing about it changed.
            self.metrics.render_coalesced.inc()
        else:
            self.metrics.render_submitted.inc()
            self._announce(submitted.job, "job.pending")
            self._wakeup.set()
        return submitted.job

    def _announce(self, job: Job, kind: JobKind) -> None:
        emit(self.events, JobEvent(kind=kind, job_id=job.id, slug=job.slug))

    def retry_after(self) -> int:
        """Seconds a refused client should wait: about one render, the time it takes
        a worker to free a place."""
        return max(1, math.ceil(self._render_estimate))

    def refresh_metrics(self) -> None:
        """Read the queue's gauges from the store. Called per scrape, from a sync
        route: with Postgres they count every replica's jobs, not this one's.

        Never raises: a store outage must not cost the scrape. The gauges then keep
        their last good values -- they do NOT go absent -- so the failure is its
        own signal, `scadbuddy_render_store_up` 0, for an alert to watch."""
        try:
            counts = self.store.counts()
        except Exception:
            logger.exception("could not read the render queue from its store")
            self.metrics.store_up.set(0)
            self.metrics.store_errors.labels("read").inc()
            return
        self.metrics.store_up.set(1)
        self.metrics.queue_depth.set(counts.pending)
        self.metrics.running.set(counts.running)
        oldest = counts.oldest_pending
        self.metrics.oldest_pending.set(
            max(0.0, (_now() - oldest).total_seconds()) if oldest is not None else 0.0
        )

    async def join(self) -> None:
        """Wait until nothing is pending or running. For tests and shutdown drains."""
        while True:
            counts = await asyncio.to_thread(self.store.counts)
            if counts.pending == 0 and counts.running == 0 and self._busy == 0:
                return
            await asyncio.sleep(0.01)

    async def _prune(self) -> None:
        await asyncio.to_thread(self.store.prune, self.config.job_ttl)
        await asyncio.to_thread(prune_revision_exports, self.paths, self.config.job_ttl)

    def _settled(self, job: Job, outcome: RenderOutcome) -> None:
        """Every way a job leaves the queue comes through here: done, failed,
        expired, superseded, and failed by the reaper."""
        self._announce(job, OUTCOME_EVENT_KINDS[outcome])
        self.metrics.render_finished.labels(outcome).inc()
        self.metrics.job_latency.labels(outcome).observe(
            max(0.0, ((job.finished_at or _now()) - job.created_at).total_seconds())
        )

    def _listener_state(self, connected: bool) -> None:
        if connected and self._listened_before:
            self.metrics.listener_reconnects.inc()
        self._listened_before = self._listened_before or connected
        self._listening = connected
        self.metrics.listener_connected.set(1 if connected else 0)
        if not connected:
            # Workers asleep on the long fallback go back to the short poll now,
            # not up to `render_fallback_poll_interval` later.
            self._wakeup.set()

    @property
    def idle_poll_interval(self) -> float:
        """How long an idle worker waits for a wake-up before it looks anyway."""
        if self._listening:
            return self.config.render_fallback_poll_interval
        return self.config.render_poll_interval

    async def _worker(self) -> None:
        while True:
            # Cleared BEFORE the claim: a submit landing between an empty claim and
            # the wait below sets it again, so the wait returns at once.
            self._wakeup.clear()
            self._busy += 1
            try:
                job = await asyncio.to_thread(self.store.claim)
                if job is not None:
                    await self._run(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # a store outage must not kill the worker
                logger.exception("render worker failed to claim or record a job")
                self.metrics.store_errors.labels("work").inc()
                job = None
                # One poll interval of back-off, and not the wakeup wait below on
                # top of it: a submit's wakeup must not hammer a store that is down.
                failed = True
            else:
                failed = False
            finally:
                self._busy -= 1
            if failed:
                await asyncio.sleep(self.config.render_poll_interval)
                continue
            if job is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wakeup.wait(), timeout=self.idle_poll_interval)

    async def _reaper(self) -> None:
        """Recover jobs whose worker died, every third of a lease."""
        interval = self.config.render_lease_timeout / 3
        while True:
            try:
                reaped = await asyncio.to_thread(
                    self.store.reap,
                    lease=self.config.render_lease_timeout,
                    max_attempts=self.config.render_max_attempts,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("could not reap render jobs with expired leases")
                self.metrics.store_errors.labels("reap").inc()
            else:
                for job in reaped.requeued:
                    logger.warning("requeued a render whose worker stopped", extra={"job": job.id})
                    self.metrics.render_retried.inc()
                    # Waiting again: `job.running` follows when a worker retakes it.
                    self._announce(job, "job.pending")
                if reaped.requeued:
                    self._wakeup.set()
                for job in reaped.failed:
                    logger.warning("failed a render whose worker stopped", extra={"job": job.id})
                    self._settled(job, "failed")
            await asyncio.sleep(interval)

    async def _heartbeat(self, job: Job) -> None:
        interval = self.config.render_lease_timeout / 3
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(self.store.heartbeat, job)
            except Exception:
                logger.exception("could not heartbeat a render job", extra={"job": job.id})
                self.metrics.store_errors.labels("heartbeat").inc()

    async def _run(self, job: Job) -> None:
        # From the SUBMIT: `created_at` is never reset, so a retry after a lost
        # worker counts the first attempt's wait and run too, and is expired once
        # the client has waited past the deadline in total.
        waited = max(0.0, ((job.started_at or _now()) - job.created_at).total_seconds())
        self.metrics.queue_wait.observe(waited)
        deadline = self.config.render_queue_timeout
        if deadline and waited > deadline:
            job.state = "failed"
            job.error = (
                f"waited {waited:.0f} s for a render worker, past the {deadline:.0f} s "
                "SCADBUDDY_RENDER_QUEUE_TIMEOUT; the server is busy, try again"
            )
            job.finished_at = _now()
            if await asyncio.to_thread(self.store.finish, job):
                self._settled(job, "expired")
            return

        self._announce(job, "job.running")
        outcome: RenderOutcome
        started = time.monotonic()
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            result, log_tail = await self._render(job)
        except OpenSCADError as error:
            outcome = "failed"
            job.state = "failed"
            job.error = str(error)
            job.log_tail = error.log_tail
            job.diagnostics = error.diagnostics
            job.diagnostics_dropped = error.diagnostics_dropped
        except Exception as error:  # the job carries the failure, the worker lives on
            outcome = "failed"
            job.state = "failed"
            job.error = f"{type(error).__name__}: {error}"
        else:
            outcome = "done"
            job.state = "done"
            job.result = result
            job.log_tail = log_tail
            job.diagnostics = result.diagnostics
            job.diagnostics_dropped = result.diagnostics_dropped
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        elapsed = time.monotonic() - started
        self.metrics.render_duration.labels(outcome).observe(elapsed)
        self._render_estimate += RENDER_ESTIMATE_WEIGHT * (elapsed - self._render_estimate)
        job.finished_at = _now()
        if await asyncio.to_thread(self.store.finish, job):
            self._settled(job, outcome)
        else:
            logger.warning(
                "a render finished after its lease was reaped; the retry's result stands",
                extra={"job": job.id},
            )
        await self._prune()
