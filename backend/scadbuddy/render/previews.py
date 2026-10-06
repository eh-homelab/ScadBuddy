"""Rendering default-render previews in the background (#179 follow-up).

A model with no thumbnail of its own and no generated output shows nothing in the
catalogue. This renders its default configuration -- the parameters as the source
declares them -- and keeps that render's plate image as a stand-in, in the
:class:`~scadbuddy.library.previews.PreviewStore`.

The work never sits in a request's path. A catalogue change hands the model's id to
:meth:`PreviewScheduler.request`, which returns at once; the scheduler's one worker
waits out a short debounce (a burst of edits is rendered once), decides whether a
render is needed at all, and runs one through its ``runner``
(:meth:`~scadbuddy.render.submit.RenderService.render_preview`, on the render worker).
The pass over every model is the ``PreviewBackfill`` workflow
(:mod:`scadbuddy.workflows.previews`), on a Schedule: it lists what is due (:meth:`due`)
and refreshes each through :meth:`PreviewScheduler.refresh`, the scheduler's own call.
One lock makes the two take turns, so the process renders one preview at a time.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor
from contextlib import suppress

from temporalio.exceptions import ApplicationError

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.tracing import span
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import CheckoutFetcher, CheckoutGate
from scadbuddy.library.previews import PreviewStore, new_work_dir, source_key
from scadbuddy.render.jobs import (
    RAW_RENDER_NAME,
    SnapshotPendingError,
    extruder_order,
    library_lease,
    plate_thumbnails,
    resolve_source,
    staged_assets,
)
from scadbuddy.render.runner import OpenSCADError, cached_schema, render_3mf
from scadbuddy.render.split import split_by_material
from scadbuddy.workflows.previews import PAUSE

logger = logging.getLogger(__name__)

#: How long a model must go unchanged before its preview is rendered: a save, then a
#: metadata edit, then a README is one render, not three.
DEFAULT_DEBOUNCE = 2.0
#: The pause after each render, so a backlog -- the first boot after an upgrade,
#: with every thumbnail-less model to render -- never runs back to back. The
#: backfill's, so the two never drift apart.
DEFAULT_INTERVAL = PAUSE.total_seconds()
#: A preview renders the schema, the model and its plate image, each bounded by
#: `render_timeout`; this bounds the three together.
TIMEOUT_FACTOR = 3

#: Renders a slug's preview within the timeout it is given, applying it itself.
PreviewRender = Callable[[str, float], Awaitable[bytes]]


class PreviewFailedError(Exception):
    pass


class PreviewUnrunError(Exception):
    """The render could not be run at all, which says nothing about the source."""


#: What a render activity's failure carries as its type when it raised one of these.
_RENDER_ERROR_TYPES = frozenset({OpenSCADError.__name__, PreviewFailedError.__name__})


def is_render_error(error: BaseException) -> bool:
    """Whether ``error`` says this source does not render (so it is not worth trying
    again until the source changes), rather than that the render could not be run:
    OpenSCAD's own failure, here or on the worker (a workflow failure caused by one),
    or a render past its timeout. An RPC error, a worker not polling yet, or any other
    infrastructure failure is not."""
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, OpenSCADError | PreviewFailedError | TimeoutError):
            return True
        if isinstance(cause, ApplicationError) and cause.type in _RENDER_ERROR_TYPES:
            return True
        cause = cause.__cause__
    return False


async def render_preview(
    slug: str,
    *,
    config: Config,
    paths: DataPaths,
    history: ModelHistory | None,
    assets: AssetStore,
    executor: Executor | None = None,
    checkouts: CheckoutGate | None = None,
    revision: str | None = None,
    fetcher: CheckoutFetcher | None = None,
) -> bytes:
    """The plate image of ``slug`` rendered at its default parameters.

    The same pipeline as a render job up to its cover image, and no further: no
    closed solids, no 3MF, no output. Its scratch space is its own, under
    ``cache/preview-work/``, and gone when this returns. As `render_job`, it holds a
    lease on the library checkouts it resolved for every openscad run (#253), and
    stages a `// file` parameter's default from the shared upload store (#204).
    ``revision`` renders that revision's export instead of the live source: a worker
    without the volume renders the snapshot it materialized (phase 3). With a
    ``fetcher``, a pinned library checkout missing here is cloned back, as for a piece.
    """
    source = await resolve_source(slug, revision, paths=paths, history=history, fetcher=fetcher)
    config = source.configure(config)
    work = new_work_dir(paths)
    async with library_lease(checkouts, f"preview:{slug}", source.library_path):
        schema = await cached_schema(source.scad, source.schema_cache, config=config)
        work.mkdir(parents=True)
        try:
            raw = work / RAW_RENDER_NAME
            async with staged_assets(schema, {}, source.scad.parent, assets) as params:
                await render_3mf(source.scad, schema, params, raw, config=config)
                parts = extruder_order(
                    await asyncio.to_thread(split_by_material, raw), schema, params
                )
        finally:
            await asyncio.to_thread(lambda: shutil.rmtree(work, ignore_errors=True))
    # Rasterising reads the meshes in memory, not the checkouts: out of the lease.
    if not parts:
        raise OpenSCADError("the render produced no geometry", [])
    thumbnails, warnings = await plate_thumbnails(parts, config=config, executor=executor)
    if thumbnails is None:
        raise PreviewFailedError(warnings[0] if warnings else "no plate image")
    return thumbnails.plate


class PreviewScheduler:
    """Keeps each model's default-render preview in step with the model."""

    def __init__(
        self,
        catalogue: Catalogue,
        store: PreviewStore,
        runner: PreviewRender,
        *,
        timeout: float,
        debounce: float = DEFAULT_DEBOUNCE,
        interval: float = DEFAULT_INTERVAL,
    ) -> None:
        self.catalogue = catalogue
        self.store = store
        self.runner = runner
        self.timeout = timeout
        self.debounce = debounce
        self.interval = interval
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Model id -> the loop time it may start at. One entry per model, so a
        #: second request for a model already waiting coalesces into it.
        self._due: dict[str, float] = {}
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._busy = False
        #: Held by every refresh, the backfill's too: one preview at a time.
        self._lock = asyncio.Lock()

    def start(self) -> None:
        # Made here, on the loop that will run the worker, not in `__init__`.
        self._due = {}
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._loop = asyncio.get_running_loop()
        self._worker = asyncio.create_task(self._run())

    async def aclose(self) -> None:
        self._loop = None
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    def request(self, slug: str) -> None:
        """Look at ``slug`` again once it has been left alone for the debounce.

        Safe from any thread -- catalogue changes happen in the worker threads the
        sync routes run in -- and never blocks: the decision and the render both
        happen on the scheduler's own worker. A no-op before `start` and after
        `aclose`.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with suppress(RuntimeError):  # the loop closed between the check and the call
            loop.call_soon_threadsafe(self._schedule, slug, self.debounce)

    def due(self) -> list[str]:
        """The models whose preview is missing, stale or no longer wanted: the
        backfill's listing. It only reads; each refresh plans again, and drops, under
        the lock. One that cannot be looked at is logged and left out; a listing that
        fails raises."""
        due: list[str] = []
        for slug in self.catalogue.slugs():
            try:
                if self.needs_refresh(slug):
                    due.append(slug)
            except Exception:
                logger.exception("could not plan a model's preview", extra={"slug": slug})
        return due

    async def idle(self) -> None:
        """Wait until nothing is due or running -- for tests."""
        while self._due or self._busy:
            await asyncio.sleep(0.01)

    def _schedule(self, slug: str, delay: float) -> None:
        assert self._loop is not None
        self._due[slug] = self._loop.time() + delay
        self._wake.set()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            if not self._due:
                self._wake.clear()
                await self._wake.wait()
                continue
            slug, due = min(self._due.items(), key=lambda item: item[1])
            wait = due - loop.time()
            if wait > 0:
                # A request can come in for an earlier model, or push this one back.
                self._wake.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), wait)
                continue
            del self._due[slug]
            self._busy = True
            try:
                rendered = await self.refresh(slug)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("could not refresh a model's preview", extra={"slug": slug})
                rendered = False
            finally:
                self._busy = False
            if rendered and self.interval > 0:
                await asyncio.sleep(self.interval)

    async def refresh(self, slug: str, *, raise_unrun: bool = False) -> bool:
        """Render ``slug``'s preview if it needs one. True when a render ran. Planned
        under the lock, so a refresh that waited on another of the same model finds
        its preview current and renders nothing. With ``raise_unrun``, a render that
        could not be run raises `PreviewUnrunError` (the backfill's failure) rather
        than counting as one that ran."""
        async with self._lock:
            return await self._refresh(slug, raise_unrun=raise_unrun)

    async def _refresh(self, slug: str, *, raise_unrun: bool) -> bool:
        key = await asyncio.to_thread(self.plan, slug)
        if key is None:
            return False
        try:
            # A root span: Temporal's StartWorkflow is a CLIENT span, which the default
            # sampler drops when nothing is above it, and the workflow goes with it.
            with span("render.preview", attributes={"scadbuddy.slug": slug}):
                png = await self.runner(slug, self.timeout)
        except asyncio.CancelledError:
            raise
        except SnapshotPendingError as error:
            # The source's first snapshot is still uploading and carries on (#686):
            # come back once it should be stored. Nothing else would bring the slug
            # back before its next edit or the next boot.
            logger.info(
                "a preview waits for its source snapshot; it is tried again",
                extra={"slug": slug, "retry_after": error.retry_after},
            )
            self._schedule(slug, error.retry_after)
            return True
        except Exception as error:
            reason = str(error) or type(error).__name__
            if not is_render_error(error):
                # Not this source's fault: nothing is recorded, so the next request
                # for it (an edit, the next backfill tick, hourly) tries again.
                logger.warning(
                    "could not run the default render for a preview; it is tried again later",
                    extra={"slug": slug, "error": reason},
                )
                if raise_unrun:
                    raise PreviewUnrunError(reason) from error
                return True
            logger.warning(
                "the default render for a preview failed; the model keeps no thumbnail",
                extra={"slug": slug, "error": reason},
            )
            # Recorded against this source, so it is not tried again until the
            # source changes: a model that cannot render must not loop.
            await asyncio.to_thread(
                lambda: self.store.record_failure(
                    slug, key, reason, wanted=lambda: self._still_wanted(slug, key)
                )
            )
            return True
        # Re-checked under the model's preview lock, so a thumbnail set or a delete
        # landing while this finishes is never undone by it.
        await asyncio.to_thread(
            lambda: self.store.write(slug, key, png, wanted=lambda: self._still_wanted(slug, key))
        )
        return True

    def plan(self, slug: str) -> str | None:
        """The source key to render ``slug`` from, or None when it needs no render.

        A model that is gone or has its own thumbnail keeps no preview at all. One
        with a generated output shows that output's plate, so its preview goes too,
        and comes back if the output is deleted. Otherwise a preview already made
        from this source -- or already failed from it -- is left as it is.
        """
        if not self._keeps_preview(slug):
            self.store.drop(slug)
            return None
        # A delete landing after the `exists` check above makes this None, and
        # nothing here drops the preview. That is safe only because
        # `Catalogue.delete` drops the model's preview row synchronously, whether or
        # not this ever sees the model go.
        key = source_key(self.catalogue.paths, slug)
        if key is None or self.store.current(slug, key):
            return None
        return key

    def needs_refresh(self, slug: str) -> bool:
        """Whether `plan` would render ``slug``'s preview or drop it, without doing
        either."""
        if not self._keeps_preview(slug):
            return self.store.record(slug) is not None
        key = source_key(self.catalogue.paths, slug)
        return key is not None and not self.store.current(slug, key)

    def _keeps_preview(self, slug: str) -> bool:
        """Whether ``slug`` shows a preview: it exists, with no thumbnail of its own
        and no generated output."""
        return (
            self.catalogue.exists(slug)
            and not self.catalogue.thumbnail_path(slug).is_file()
            and not self.catalogue.has_output_cover(slug)
        )

    def _still_wanted(self, slug: str, key: str) -> bool:
        """Whether a render from ``key`` is still the one to keep, now that it is done.

        The model can be deleted, given a thumbnail or an output, or edited while it
        renders. An edit has requested a render of its own, so this one is dropped
        rather than kept as a stale stand-in.
        """
        return self._keeps_preview(slug) and source_key(self.catalogue.paths, slug) == key
