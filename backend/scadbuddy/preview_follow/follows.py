"""The print dialog's background slice, followed for the realtime socket (#265).

Nothing announces a background slice's progress. Bambuddy broadcasts nothing for a
slice job (`bambuddy/follow.py`), and the native slicer's percent is only in its
activity's heartbeat (`slicing.start.slice_progress`). So the API process reads the
slice itself, as the dialog used to, and publishes `PreviewSliceEvent` on
``preview-slice:<slicer>:<job id>`` when the read changes. The dialog then reads once
per event, and polls only while the socket is unavailable (`usePreviewSlice.ts`).

A read of the slice through its route starts the follow (`follow`). A follow ends when
the slice ends, when a read fails, after `QUIET_READS` reads with no change (its dialog
may have closed), or after `FOLLOW_FOR`. Each end publishes
``preview_slice.settled`` or ``preview_slice.progress``, so a dialog that still watches
reads the route again, and that read starts a new follow if the slice still runs. That
also covers a restart: the socket closes with the process, and the dialog reads again
when its subscription is confirmed on the new socket. So the follow keeps no record and
needs no Temporal workflow.

One replica follows a slice (#6158, `claims.py`): a follow starts only once its process
holds the slice's advisory lock. A read on a replica that finds the lock held elsewhere
starts nothing and answers ``followed: true``, because the follower's events reach every
replica's sockets over the Postgres bus. A follow releases its lock before it publishes
its last event, so the read that the event causes can take the lock on any replica. A
follower that stops (a shutdown, a crash) publishes nothing, so its dialogs on other
replicas hear nothing more until they read again (their socket reconnects, or a choice
changes). That was so before #6158 too: a dialog's socket and its reads need not reach
the same replica.

At most `MAX_FOLLOWS` slices are followed at once. Past that, the read answers
``followed: false``, and the dialog reads on its timer instead. A follow is never
evicted to make room: its dialog would read at once and evict the next, a loop at the
speed of the round trip.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from scadbuddy.bambuddy.preview import SlicePreview
from scadbuddy.core.events import EventBus, PreviewSliceEvent, emit
from scadbuddy.preview_follow.claims import FollowClaims

logger = logging.getLogger(__name__)

Slicer = Literal["bambuddy", "native"]
Reader = Callable[[], Awaitable[SlicePreview]]
Kind = Literal["preview_slice.progress", "preview_slice.settled"]

#: Seconds between reads of a slice under way: the interval the dialog polled at.
FOLLOW_INTERVAL = 2.0
#: Seconds a follow lasts unless a read of the route starts it again.
FOLLOW_FOR = 600.0
#: Reads in a row with no change after which a follow stops (a minute at
#: `FOLLOW_INTERVAL`): a closed dialog's slice is not read for long, and a dialog that
#: still watches reads again on the ``progress`` the stop publishes.
QUIET_READS = 30
#: The most slices one process follows (or is claiming) at once. Past this, a slice is
#: not followed: its read says so (``followed: false``), and a warning is logged.
MAX_FOLLOWS = 64
FINISHED = frozenset({"completed", "failed", "cancelled"})


class PreviewFollows:
    """The background slices this process follows, one task each."""

    def __init__(
        self,
        events: EventBus | None,
        *,
        claims: FollowClaims | None = None,
        interval: float = FOLLOW_INTERVAL,
        limit: float = FOLLOW_FOR,
        quiet_reads: int = QUIET_READS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.events = events
        #: Which replica follows each slice; None follows every slice read here.
        self.claims = claims
        self.interval = interval
        self.limit = limit
        self.quiet_reads = quiet_reads
        self._clock = clock
        self._running: dict[tuple[Slicer, int, str], asyncio.Task[None]] = {}
        #: Slices whose claim is on its way: a second read here does not claim again.
        self._claiming: set[tuple[Slicer, int, str]] = set()
        #: Claims whose read was cancelled, and the releases of those that came back
        #: True: `aclose` waits for them.
        self._abandoned: set[asyncio.Future[Any]] = set()

    @property
    def following(self) -> int:
        return len(self._running)

    async def read(self, slicer: Slicer, job_id: int, subject: str, read: Reader) -> SlicePreview:
        """``read()``'s answer, with the slice followed from it; ``followed`` says
        whether a follow announces its next change."""
        seen = await read()
        followed = await self.follow(slicer, job_id, subject, seen, read)
        return seen.model_copy(update={"followed": followed})

    async def follow(
        self,
        slicer: Slicer,
        job_id: int,
        subject: str,
        seen: SlicePreview,
        read: Reader,
    ) -> bool:
        """Follow slice ``job_id`` of ``subject``, which a read just found as ``seen``,
        unless it has ended or a follow of it runs already, here or on another replica.
        Whether a follow announces its next change: False for an ended slice, and past
        `MAX_FOLLOWS`."""
        if seen.status in FINISHED:
            return False
        key = (slicer, job_id, subject)
        if key in self._running or key in self._claiming:
            return True
        if len(self._running) + len(self._claiming) >= MAX_FOLLOWS:
            logger.warning("too many background slices to follow", extra={"job_id": job_id})
            return False
        claim = f"{slicer}:{job_id}:{subject}"
        if self.claims is not None:
            self._claiming.add(key)
            # Shielded (#6162): a cancelled read does not stop the thread, which may
            # still take the lock. When the read is cancelled, `_abandon` releases the
            # lock once the claim answers True, and the slice stays in `_claiming` until
            # then.
            pending = asyncio.get_running_loop().run_in_executor(
                None, functools.partial(contextvars.copy_context().run, self.claims.claim, claim)
            )
            abandoned = False
            try:
                mine = await asyncio.shield(pending)
            except asyncio.CancelledError:
                abandoned = True
                self._abandon(self.claims, key, claim, pending)
                raise
            finally:
                if not abandoned:
                    self._claiming.discard(key)
            if not mine:
                # Another replica follows it, and its events reach this dialog's socket.
                return True
        task = asyncio.create_task(self._follow(slicer, job_id, claim, seen, read))
        self._running[key] = task
        task.add_done_callback(lambda _: self._running.pop(key, None))
        return True

    def _abandon(
        self,
        claims: FollowClaims,
        key: tuple[Slicer, int, str],
        claim: str,
        pending: asyncio.Future[bool],
    ) -> None:
        """Release ``claim`` once ``pending`` answers True: no follow uses it."""
        self._abandoned.add(pending)

        def answered(done: asyncio.Future[bool]) -> None:
            self._abandoned.discard(done)
            if done.cancelled() or done.exception() is not None or not done.result():
                self._claiming.discard(key)
                return
            release = asyncio.ensure_future(self._release_abandoned(claims, key, claim))
            self._abandoned.add(release)
            release.add_done_callback(self._abandoned.discard)

        pending.add_done_callback(answered)

    async def _release_abandoned(
        self, claims: FollowClaims, key: tuple[Slicer, int, str], claim: str
    ) -> None:
        try:
            await asyncio.to_thread(claims.release, claim)
        finally:
            self._claiming.discard(key)

    async def _follow(
        self,
        slicer: Slicer,
        job_id: int,
        claim: str,
        seen: SlicePreview,
        read: Reader,
    ) -> None:
        try:
            last = await self._watch(slicer, job_id, seen, read)
        finally:
            # Released before the last event, so the read it causes, on any replica, can
            # follow the slice again. A cancelled follow (shutdown) releases too.
            if self.claims is not None:
                await asyncio.to_thread(self.claims.release, claim)
        emit(self.events, PreviewSliceEvent(kind=last, slicer=slicer, job_id=job_id))

    async def _watch(self, slicer: Slicer, job_id: int, seen: SlicePreview, read: Reader) -> Kind:
        """Read the slice until its follow ends, publishing each change. The kind of the
        last event, which `_follow` publishes once the claim is released."""
        last = (seen.status, seen.progress)
        until = self._clock() + self.limit
        quiet = 0
        while self._clock() < until and quiet < self.quiet_reads:
            await asyncio.sleep(self.interval)
            try:
                now = await read()
            except Exception:
                # The dialog reads the route and shows what it answers (a 404, Bambuddy
                # down); a read that runs again starts a new follow.
                logger.info("a background slice could not be read", extra={"job_id": job_id})
                return "preview_slice.settled"
            if now.status in FINISHED:
                return "preview_slice.settled"
            if (now.status, now.progress) != last:
                last = (now.status, now.progress)
                quiet = 0
                emit(
                    self.events,
                    PreviewSliceEvent(kind="preview_slice.progress", slicer=slicer, job_id=job_id),
                )
            else:
                quiet += 1
        # Out of time, or quiet: a dialog that still watches reads again, which follows
        # again.
        return "preview_slice.progress"

    async def aclose(self) -> None:
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A claim still answering, then the release of one that came back True.
        while self._abandoned:
            await asyncio.gather(*list(self._abandoned), return_exceptions=True)
        if self.claims is not None:
            await asyncio.to_thread(self.claims.close)
