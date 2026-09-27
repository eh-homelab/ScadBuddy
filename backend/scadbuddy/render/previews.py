"""Rendering default-render previews in the background (#179 follow-up).

A model with no thumbnail of its own and no generated output shows nothing in the
catalogue. This renders its default configuration -- the parameters as the source
declares them -- and keeps that render's plate image as a stand-in, in the
:class:`~scadbuddy.library.previews.PreviewStore`.

The work never sits in a request's path. A catalogue change hands the model's id to
:meth:`PreviewScheduler.request`, which returns at once; the scheduler's one worker
waits out a short debounce (a burst of edits is rendered once), decides whether a
render is needed at all, and runs one through
:meth:`~scadbuddy.render.jobs.RenderQueue.run_background`, behind every render a
person has asked for. One preview at a time, with a pause after each, is also what
throttles the boot-time pass over every model without a thumbnail.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import Executor
from contextlib import suppress

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.previews import PreviewStore, source_key
from scadbuddy.render.jobs import (
    RAW_RENDER_NAME,
    RenderQueue,
    extruder_order,
    plate_thumbnails,
    resolve_source,
)
from scadbuddy.render.runner import OpenSCADError, cached_schema, render_3mf
from scadbuddy.render.split import split_by_material

logger = logging.getLogger(__name__)

#: How long a model must go unchanged before its preview is rendered: a save, then a
#: metadata edit, then a README is one render, not three.
DEFAULT_DEBOUNCE = 2.0
#: The pause after each render, so a backlog -- the first boot after an upgrade,
#: with every thumbnail-less model to render -- never runs back to back.
DEFAULT_INTERVAL = 1.0
#: A preview renders the schema, the model and its plate image, each bounded by
#: `render_timeout`; this bounds the three together.
TIMEOUT_FACTOR = 3

PreviewRender = Callable[[str], Awaitable[bytes]]


class PreviewFailedError(Exception):
    pass


async def render_preview(
    slug: str,
    *,
    config: Config,
    paths: DataPaths,
    history: ModelHistory | None,
    executor: Executor | None = None,
) -> bytes:
    """The plate image of ``slug`` rendered at its default parameters.

    The same pipeline as a render job up to its cover image, and no further: no
    closed solids, no 3MF, no output. Its scratch space is its own, under the
    previews directory, and gone when this returns.
    """
    source = await resolve_source(slug, None, paths=paths, history=history)
    config = source.configure(config)
    schema = await cached_schema(source.scad, source.schema_cache, config=config)
    work = paths.previews / f".work-{uuid.uuid4().hex}"
    work.mkdir(parents=True)
    try:
        raw = work / RAW_RENDER_NAME
        await render_3mf(source.scad, schema, {}, raw, config=config)
        parts = extruder_order(await asyncio.to_thread(split_by_material, raw), schema, {})
        if not parts:
            raise OpenSCADError("the render produced no geometry", [])
        thumbnails, warnings = await plate_thumbnails(parts, config=config, executor=executor)
        if thumbnails is None:
            raise PreviewFailedError(warnings[0] if warnings else "no plate image")
        return thumbnails.plate
    finally:
        await asyncio.to_thread(lambda: shutil.rmtree(work, ignore_errors=True))


class PreviewScheduler:
    """Keeps each model's default-render preview in step with the model."""

    def __init__(
        self,
        catalogue: Catalogue,
        store: PreviewStore,
        queue: RenderQueue,
        render: PreviewRender,
        *,
        timeout: float,
        debounce: float = DEFAULT_DEBOUNCE,
        interval: float = DEFAULT_INTERVAL,
    ) -> None:
        self.catalogue = catalogue
        self.store = store
        self.queue = queue
        self.render = render
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

    def start(self) -> None:
        # Made here, on the loop that will run the worker, not in `__init__`.
        self._due = {}
        self._wake = asyncio.Event()
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

    def request_all(self, slugs: Iterable[str]) -> None:
        """The boot-time pass: every model, taken one at a time like any other."""
        for slug in slugs:
            self.request(slug)

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
                rendered = await self._refresh(slug)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("could not refresh a model's preview", extra={"slug": slug})
                rendered = False
            finally:
                self._busy = False
            if rendered and self.interval > 0:
                await asyncio.sleep(self.interval)

    async def _refresh(self, slug: str) -> bool:
        """Render ``slug``'s preview if it needs one. True when a render ran."""
        key = await asyncio.to_thread(self._plan, slug)
        if key is None:
            return False
        try:
            png = await self.queue.run_background(
                lambda: asyncio.wait_for(self.render(slug), timeout=self.timeout)
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            reason = str(error) or type(error).__name__
            logger.warning(
                "the default render for a preview failed; the model keeps no thumbnail",
                extra={"slug": slug, "error": reason},
            )
            # Recorded against this source, so it is not tried again until the
            # source changes: a model that cannot render must not loop.
            if await asyncio.to_thread(self._still_wanted, slug, key):
                await asyncio.to_thread(self.store.record_failure, slug, key, reason)
            return True
        if await asyncio.to_thread(self._still_wanted, slug, key):
            await asyncio.to_thread(self.store.write, slug, key, png)
        return True

    def _plan(self, slug: str) -> str | None:
        """The source key to render ``slug`` from, or None when it needs no render.

        A model that is gone or has its own thumbnail keeps no preview at all. One
        with a generated output shows that output's plate, so its preview goes too,
        and comes back if the output is deleted. Otherwise a preview already made
        from this source -- or already failed from it -- is left as it is.
        """
        if not self.catalogue.exists(slug) or self.catalogue.thumbnail_path(slug).is_file():
            self.store.drop(slug)
            return None
        if self.catalogue.has_output_cover(slug):
            self.store.drop(slug)
            return None
        key = source_key(self.store.paths, slug)
        if key is None:
            return None
        record = self.store.record(slug)
        if record is not None and record.key == key:
            return None
        return key

    def _still_wanted(self, slug: str, key: str) -> bool:
        """Whether a render from ``key`` is still the one to keep, now that it is done.

        The model can be deleted, given a thumbnail or an output, or edited while it
        renders. An edit has requested a render of its own, so this one is dropped
        rather than kept as a stale stand-in.
        """
        return (
            self.catalogue.exists(slug)
            and not self.catalogue.thumbnail_path(slug).is_file()
            and not self.catalogue.has_output_cover(slug)
            and source_key(self.store.paths, slug) == key
        )
