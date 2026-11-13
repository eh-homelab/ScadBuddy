"""The print dialog's background slice, followed for the realtime socket (#265).

Nothing announces a background slice's progress. Bambuddy broadcasts nothing for a
slice job (`bambuddy/follow.py`), and the native slicer's percent is only in its
activity's heartbeat (`slicing.start.slice_progress`). So the API process reads the
slice itself, as the dialog used to, and publishes `PreviewSliceEvent` on
``preview-slice:<slicer>:<job id>`` when the read changes. The dialog then reads once
per event, and polls only while the socket is unavailable (`usePreviewSlice.ts`).

A read of the slice through its route starts the follow (`follow`). A follow ends when
the slice ends, when a read fails, or after `FOLLOW_FOR`. Each end publishes
``preview_slice.settled`` or ``preview_slice.progress``, so a dialog that still watches
reads the route again, and that read starts a new follow if the slice still runs. That
also covers a restart: the socket closes with the process, and the dialog reads again
when its subscription is confirmed on the new socket. So the follow keeps no record and
needs no Temporal workflow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from scadbuddy.bambuddy.preview import SlicePreview
from scadbuddy.core.events import EventBus, PreviewSliceEvent, emit

logger = logging.getLogger(__name__)

Slicer = Literal["bambuddy", "native"]
Reader = Callable[[], Awaitable[SlicePreview]]

#: Seconds between reads of a slice under way: the interval the dialog polled at.
FOLLOW_INTERVAL = 2.0
#: Seconds a follow lasts unless a read of the route starts it again.
FOLLOW_FOR = 600.0
#: The most slices one process follows at once. Past this, a slice is not followed,
#: and a warning says so.
MAX_FOLLOWS = 64
FINISHED = frozenset({"completed", "failed", "cancelled"})


class PreviewFollows:
    """The background slices this process follows, one task each."""

    def __init__(
        self,
        events: EventBus | None,
        *,
        interval: float = FOLLOW_INTERVAL,
        limit: float = FOLLOW_FOR,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.events = events
        self.interval = interval
        self.limit = limit
        self._clock = clock
        self._running: dict[tuple[Slicer, int, str], asyncio.Task[None]] = {}

    @property
    def following(self) -> int:
        return len(self._running)

    async def read(self, slicer: Slicer, job_id: int, subject: str, read: Reader) -> SlicePreview:
        """``read()``'s answer, with the slice followed from it."""
        seen = await read()
        self.follow(slicer, job_id, subject, seen, read)
        return seen

    def follow(
        self,
        slicer: Slicer,
        job_id: int,
        subject: str,
        seen: SlicePreview,
        read: Reader,
    ) -> None:
        """Follow slice ``job_id`` of ``subject``, which a read just found as ``seen``,
        unless it has ended or a follow of it runs already."""
        if seen.status in FINISHED:
            return
        key = (slicer, job_id, subject)
        if key in self._running:
            return
        if len(self._running) >= MAX_FOLLOWS:
            logger.warning("too many background slices to follow", extra={"job_id": job_id})
            return
        task = asyncio.create_task(self._follow(slicer, job_id, seen, read))
        self._running[key] = task
        task.add_done_callback(lambda _: self._running.pop(key, None))

    async def _follow(
        self,
        slicer: Slicer,
        job_id: int,
        seen: SlicePreview,
        read: Reader,
    ) -> None:
        def publish(kind: Literal["preview_slice.progress", "preview_slice.settled"]) -> None:
            emit(self.events, PreviewSliceEvent(kind=kind, slicer=slicer, job_id=job_id))

        last = (seen.status, seen.progress)
        until = self._clock() + self.limit
        while self._clock() < until:
            await asyncio.sleep(self.interval)
            try:
                now = await read()
            except Exception:
                # The dialog reads the route and shows what it answers (a 404, Bambuddy
                # down); a read that runs again starts a new follow.
                logger.info("a background slice could not be read", extra={"job_id": job_id})
                publish("preview_slice.settled")
                return
            if now.status in FINISHED:
                publish("preview_slice.settled")
                return
            if (now.status, now.progress) != last:
                last = (now.status, now.progress)
                publish("preview_slice.progress")
        # Out of time: a dialog that still watches reads again, which follows again.
        publish("preview_slice.progress")

    async def aclose(self) -> None:
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
