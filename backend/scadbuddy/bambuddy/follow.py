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
from typing import Literal

from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.bambuddy.output_reader import OutputReader
from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver
from scadbuddy.core.events import EventBus, PrintEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputNotFoundError

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
#: settle leaves the archives it had not yet started unrecorded until that output settles
#: again (another print of it), which records them with its own time; the warning names
#: the output so the gap can be traced. Cutting a hook off stops the wait, not the work:
#: a database read or write it started in a thread runs on until Postgres answers, so
#: the archive whose write was in flight may still be recorded.
SETTLE_TIMEOUT = 60.0

FOLLOW_ACTIVITY = "follow_print"

Reader = Callable[[OutputMeta], Awaitable[PrintProgress | None]]
Ended = Literal["settled", "gone", "deleted", "quiet"]
#: Awaited on each read that finds a print settled (#836): after its ``print.settled``
#: is published.
SettledHook = Callable[[OutputMeta], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(UTC)


class FollowInput(BaseModel):
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
    ) -> None:
        self.outputs = outputs
        self.observer = observer
        self.read = read
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

    async def follow(
        self,
        output_id: str,
        active: datetime,
        heartbeat: Callable[[datetime], None] = lambda _: None,
        *,
        read_now: bool = False,
    ) -> Ended:
        """Read ``output_id``'s print until it ends; ``active`` is when it last moved.
        It waits first, unless ``read_now`` (a poke: a new print, read at once); either
        way the waits after that back off from `min_interval`."""
        interval = self.min_interval
        last_failure: tuple[int, str] | None = None
        while True:
            if read_now:
                read_now = False
            else:
                await self._wait(interval, active, heartbeat)
            if self.now() - active > self.max_age:
                return "quiet"
            try:
                meta = await self.outputs.get(output_id)
            except OutputNotFoundError:
                return "deleted"
            except Exception:
                # A disk blip or a half-written meta.json: keep following, slowly.
                logger.exception("could not read the output", extra={"output_id": output_id})
                interval = self.error_interval
                continue
            try:
                progress = await self.read(meta)
            except ApiError as error:
                failure = (error.status, error.detail)
                if failure != last_failure:
                    last_failure = failure
                    emit(
                        self.events,
                        PrintEvent(kind="print.progress", output_id=meta.id, slug=meta.slug),
                    )
                if error.status == 404:
                    return "gone"
                interval = self.error_interval
                continue
            except Exception:
                # Not Bambuddy's answer (a bug, a broken setting): keep following, slowly.
                logger.exception("a print progress read failed", extra={"output_id": output_id})
                interval = self.error_interval
                continue
            last_failure = None
            changed = self.observer.observe(meta, progress)
            if progress is not None and progress.settled:
                # After observe, so print.settled is already published (#836). Not on
                # progress None: an output never printed through slice_queue has no picks.
                await self._settled(meta, active, heartbeat)
            if progress is None or progress.settled:
                return "settled"
            if changed:
                active = self.now()
            interval = self.min_interval if changed else min(self.max_interval, interval * 2)

    async def _settled(
        self, meta: OutputMeta, active: datetime, heartbeat: Callable[[datetime], None]
    ) -> None:
        for hook in self.on_settled:
            try:
                await self._bounded(hook(meta), active, heartbeat)
            except Exception as exc:
                # Type only: a hook's error can carry data it must not log (#836, spec §7).
                logger.warning(
                    "a settled-print hook failed",
                    extra={"output_id": meta.id, "error": type(exc).__name__},
                )

    async def _bounded(
        self, work: Awaitable[None], active: datetime, heartbeat: Callable[[datetime], None]
    ) -> None:
        """Await ``work`` for at most `settle_timeout` (`TimeoutError` past it),
        heartbeating ``active`` at least every `HEARTBEAT_SLICE` meanwhile."""
        task = asyncio.ensure_future(work)
        try:
            async with asyncio.timeout(self.settle_timeout):
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
    def __init__(self, follower: Follower) -> None:
        self.follower = follower

    @activity.defn(name=FOLLOW_ACTIVITY)
    async def follow_print(self, input: FollowInput) -> str:
        """Follow the print; a retried attempt resumes the age its heartbeat carried, and
        only a poke's first attempt reads at once. A worker shutting down ends the
        attempt rather than holding the shutdown up: another worker retries it."""
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
