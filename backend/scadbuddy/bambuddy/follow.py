"""Following one print until it settles (#268), as `FollowPrint`'s activity (#1053,
spec 2026-10-01 §4.4).

A print moves only when someone reads it: the backend follows each print itself, once,
from the moment a run starts it until it is ``settled``, and publishes each change as a
``print.*`` event the UI follows on the ``print:<output id>`` topic (#266). Temporal
keeps the follow across restarts (`workflows/follow.py`), so there is no log of
prints to resume, no lock and no rescan.

Why polling, with back-off
--------------------------
Bambuddy 1.2.5.5 does have a push channel, ``WS /api/v1/ws``, which an API key can
join with a token from ``POST /api/v1/auth/ws-token`` (``can_read_status``;
bambuddy ``backend/app/api/routes/auth.py`` ``mint_websocket_token``). It cannot
replace reading, though:
- nothing is broadcast for a slice job;
- completion is ``print_complete`` per *printer*, not per queue item;
- ``pipeline_run_updated`` goes through ``broadcast_to_user(run.created_by)``.
(bambuddy ``backend/app/core/websocket.py`` ``send_*``;
``backend/app/api/routes/pipeline_runs.py``.)

So the follow reads through :func:`progress_for`, the rules the progress route uses
(#89, #233, #240): every ``MIN_INTERVAL`` while the print is moving, doubling to
``MAX_INTERVAL`` while nothing changes. A failed read publishes ``print.progress`` once
per distinct failure, so the UI re-reads the progress route and shows the scope-aware
problem it answers (``bambuddy/errors.py``), then waits ``ERROR_INTERVAL``. A 404 means
Bambuddy no longer has the print, and the follow ends. ``MAX_AGE`` counts from the
latest of the attempt's start and the last change seen: a long print that keeps moving
is followed to the end, and a forgotten quiet one is not followed for ever. The
activity heartbeats that time, so a retried attempt keeps it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from prometheus_client import Gauge
from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.bambuddy.output_reader import OutputReader
from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver
from scadbuddy.bambuddy.subject import PrintSubject, library_slug
from scadbuddy.core.events import EventBus, PrintEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputNotFoundError

if TYPE_CHECKING:
    # Annotations only: runs imports print_run, which reaches rack.usage, which imports this.
    from scadbuddy.bambuddy.runs import NewestFailed

logger = logging.getLogger(__name__)

#: Seconds between reads while a print is moving.
MIN_INTERVAL = 2.0
#: The longest wait between reads of a print that is not changing.
MAX_INTERVAL = 30.0
#: The wait after a failed read (Bambuddy down, a key without the scope).
ERROR_INTERVAL = 60.0
#: A quiet print is followed for at most this long after it last moved.
MAX_AGE = timedelta(hours=24)
#: The longest the activity goes between heartbeats while it waits.
HEARTBEAT_SLICE = 5.0
#: How long one settled-print hook may run (#1083). Hooks are awaited inside the
#: activity, which heartbeats meanwhile (a hook longer than ``FOLLOW_HEARTBEAT`` would
#: otherwise time the attempt out, and each retry would run it again), so one that never
#: returns would hold the follow open. A hook cut off here is not retried now: the rack's
#: settle leaves the archives it had not yet started unrecorded until that subject settles
#: again (another print of it), which records them with its own time; the warning names
#: the subject so the gap can be traced. Cutting a hook off stops the wait, not the work:
#: a database read or write it started in a thread runs on until Postgres answers, so
#: the archive whose write was in flight may still be recorded.
SETTLE_TIMEOUT = 60.0

FOLLOW_ACTIVITY = "follow_print"
#: The follow worker's slots (review #1091 2): each attempt holds one for as long as its
#: print moves, and a poke's old attempt holds its own until its next heartbeat. Past
#: this many, new follows wait on the queue and nothing is published for their prints;
#: `scadbuddy_print_follows_running` and a warning say so.
FOLLOW_SLOTS = 200

Reader = Callable[[OutputMeta], Awaitable[PrintProgress | None]]
#: A library file's print, read by its subject (#1073): its recent sends' queue items.
LibraryReader = Callable[[PrintSubject], Awaitable[PrintProgress | None]]
Ended = Literal["settled", "gone", "deleted", "quiet"]
#: Awaited on each read that finds a print settled (#836): after its ``print.settled``
#: is published. Given the print's subject, an output's or a library file's (#1073).
SettledHook = Callable[[PrintSubject], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(UTC)


class FollowInput(BaseModel):
    #: The run subject (`PrintSubject.run_subject`): an output's id, or
    #: ``library:<file id>`` (#1073). Named as it was when only outputs were followed.
    output_id: str
    #: A poke (a new print, someone reading it): read now, the age counts from now.
    fresh: bool = False


class Follower:
    def __init__(
        self,
        *,
        outputs: OutputReader,
        observer: ProgressObserver,
        read: Reader,
        events: EventBus | None,
        min_interval: float = MIN_INTERVAL,
        max_interval: float = MAX_INTERVAL,
        error_interval: float = ERROR_INTERVAL,
        max_age: timedelta = MAX_AGE,
        now: Callable[[], datetime] = _now,
        on_settled: Sequence[SettledHook] = (),
        settle_timeout: float = SETTLE_TIMEOUT,
        read_library: LibraryReader | None = None,
        newest_failed: NewestFailed | None = None,
    ) -> None:
        self.outputs = outputs
        self.observer = observer
        self.read = read
        #: Without one, a library file's follow ends at once, as ``gone``.
        self.read_library = read_library
        self.events = events
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.error_interval = error_interval
        self.max_age = max_age
        self.now = now
        #: Each runs on the settled branch only; what one raises is logged by type and the
        #: follow ends as before. A feature registers itself here (``rack/component.py``).
        #: A hook must be idempotent: it may run more than once for one print, and
        #: concurrently (a poke's old attempt reads until its next heartbeat).
        self.on_settled: list[SettledHook] = list(on_settled)
        self.settle_timeout = settle_timeout
        #: The progress the subject's newest run shows when it failed before queueing
        #: (``runs.newest_failure``), else None. While it is not None that is what is
        #: published, as the progress routes publish it: a follow of an older print
        #: publishing that print's progress meanwhile made the two alternate (#1837).
        #: The follow still reads the older print until it settles, for its hooks. Its
        #: settle then emits no ``print.settled`` on that subject: the subject shows the
        #: newer failure, not the older print, and that is intended (#2015).
        self.newest_failed = newest_failed

    async def follow(
        self,
        run_subject: str,
        active: datetime,
        heartbeat: Callable[[datetime], None] = lambda _: None,
        *,
        read_now: bool = False,
    ) -> Ended:
        """Read ``run_subject``'s print until it ends; ``active`` is when it last moved.
        It waits first, unless ``read_now`` (a poke: a new print, read at once); either
        way the waits after that back off from `min_interval`. ``run_subject`` is an output's
        id, or ``library:<file id>`` for a library file's print (#1073)."""
        subject = PrintSubject.from_run_subject(run_subject)
        if subject.kind == "library" and self.read_library is None:
            return "gone"
        interval = self.min_interval
        last_failure: tuple[int, str] | None = None
        last_read: PrintProgress | None = None
        while True:
            if read_now:
                read_now = False
            else:
                await self._wait(interval, active, heartbeat)
            if self.now() - active > self.max_age:
                return "quiet"
            try:
                key, slug, reading = await self._read(subject)
            except OutputNotFoundError:
                return "deleted"
            except Exception:
                # A disk blip or a half-written meta.json: keep following, slowly.
                logger.exception(
                    "could not read the print's subject", extra={"subject": subject.key}
                )
                interval = self.error_interval
                continue
            try:
                # Two Bambuddy calls of up to 30 s each: longer than ``FOLLOW_HEARTBEAT``
                # (review #1091 1).
                progress = await self._heartbeating(reading, active, heartbeat)
            except ApiError as error:
                failure = (error.status, error.detail)
                if failure != last_failure:
                    last_failure = failure
                    emit(self.events, PrintEvent(kind="print.progress", output_id=key, slug=slug))
                if error.status == 404:
                    return "gone"
                interval = self.error_interval
                continue
            except Exception:
                # Not Bambuddy's answer (a bug, a broken setting): keep following, slowly.
                logger.exception("a print progress read failed", extra={"subject": subject.key})
                interval = self.error_interval
                continue
            last_failure = None
            failed = await self._newest_failed(key)
            published = self.observer.observe_subject(key, slug, failed or progress)
            # Whether the print moved: while a newer run's failure is what is published,
            # that is not what the observer compared.
            changed = published if failed is None else progress != last_read
            last_read = progress
            if progress is not None and progress.settled:
                # After observe, so print.settled is already published (#836). Not on
                # progress None: a print never queued has no picks.
                await self._settled(subject, active, heartbeat)
            if progress is None or progress.settled:
                return "settled"
            if changed:
                active = self.now()
            interval = self.min_interval if changed else min(self.max_interval, interval * 2)

    async def _newest_failed(self, key: str) -> PrintProgress | None:
        if self.newest_failed is None:
            return None
        try:
            return await self.newest_failed(key)
        except Exception:
            # Not the database (newest_failure answers None for that): a bug, or a
            # broken setting. Publish the print's own progress, as before #1837.
            logger.exception("the newest run's failure could not be read", extra={"subject": key})
            return None

    async def _read(
        self, subject: PrintSubject
    ) -> tuple[str, str, Awaitable[PrintProgress | None]]:
        """What ``subject``'s print is announced under (its run subject and slug), and
        its progress read: an output's by its record (`OutputNotFoundError` once it is
        deleted), a library file's by its subject (#1073, #1751). Where the read comes
        from is the one difference between them."""
        if subject.kind == "library":
            assert self.read_library is not None  # `follow` ends at once without one.
            return subject.run_subject, library_slug(subject), self.read_library(subject)
        meta = await self.outputs.get(subject.id)
        return meta.id, meta.slug, self.read(meta)

    async def _settled(
        self, subject: PrintSubject, active: datetime, heartbeat: Callable[[datetime], None]
    ) -> None:
        for hook in self.on_settled:
            try:
                await self._bounded(hook(subject), active, heartbeat)
            except Exception as exc:
                # Type only: a hook's error can carry data it must not log (#836, spec §7).
                logger.warning(
                    "a settled-print hook failed",
                    extra={"subject": subject.key, "error": type(exc).__name__},
                )

    async def _bounded(
        self, work: Awaitable[None], active: datetime, heartbeat: Callable[[datetime], None]
    ) -> None:
        """Await ``work`` for at most `settle_timeout` (`TimeoutError` past it),
        heartbeating ``active`` at least every `HEARTBEAT_SLICE` meanwhile."""
        async with asyncio.timeout(self.settle_timeout):
            await self._heartbeating(work, active, heartbeat)

    async def _heartbeating[T](
        self, work: Awaitable[T], active: datetime, heartbeat: Callable[[datetime], None]
    ) -> T:
        """Await ``work``, heartbeating ``active`` at least every `HEARTBEAT_SLICE`."""
        task = asyncio.ensure_future(work)
        try:
            while not (await asyncio.wait({task}, timeout=HEARTBEAT_SLICE))[0]:
                heartbeat(active)
            return task.result()
        finally:
            task.cancel()

    async def _wait(
        self, seconds: float, active: datetime, heartbeat: Callable[[datetime], None]
    ) -> None:
        """Wait ``seconds`` in slices, heartbeating ``active`` after each."""
        while True:
            step = min(seconds, HEARTBEAT_SLICE)
            if step > 0:
                await asyncio.sleep(step)
            heartbeat(active)
            seconds -= step
            if seconds <= 0:
                return


class FollowActivities:
    def __init__(
        self, follower: Follower, *, slots: int = FOLLOW_SLOTS, running: Gauge | None = None
    ) -> None:
        self.follower = follower
        self.slots = slots
        self.running = running
        self._held = 0

    @activity.defn(name=FOLLOW_ACTIVITY)
    async def follow_print(self, input: FollowInput) -> str:
        """Follow the print; a retried attempt resumes the age its heartbeat carried, and
        only a poke's first attempt reads at once. A worker shutting down ends the
        attempt rather than holding the shutdown up: another worker retries it."""
        self._held += 1
        if self.running is not None:
            self.running.inc()
        if self._held >= self.slots:
            logger.warning(
                "every follow slot is taken: the next prints wait to be followed",
                extra={"slots": self.slots},
            )
        try:
            return await self._follow_print(input)
        finally:
            self._held -= 1
            if self.running is not None:
                self.running.dec()

    async def _follow_print(self, input: FollowInput) -> str:
        info = activity.info()
        active = self.follower.now()
        if info.heartbeat_details:
            active = datetime.fromisoformat(str(info.heartbeat_details[0]))
        following = asyncio.ensure_future(
            self.follower.follow(
                input.output_id,
                active,
                lambda at: activity.heartbeat(at.isoformat()),
                read_now=input.fresh and info.attempt == 1,
            )
        )
        shutdown = asyncio.ensure_future(activity.wait_for_worker_shutdown())
        try:
            await asyncio.wait({following, shutdown}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            following.cancel()
            shutdown.cancel()
        if following.done() and not following.cancelled():
            return following.result()
        raise ApplicationError("the worker is shutting down; another attempt follows on")
