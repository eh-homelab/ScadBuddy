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
from typing import TYPE_CHECKING, Literal, Protocol

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
    from scadbuddy.bambuddy.runs import NewestFailed, PrintRun

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
#: Awaited on each read whose progress differs from the read before it, the settled one
#: included (#4644, #4543): given the run subject and that progress. A LAN print's last
#: print and its rack picks follow it, since the report is all a LAN print has.
ReadHook = Callable[[str, PrintProgress], Awaitable[None]]
#: Records a settled print's outcome once (#2665, `outcomes.PrintOutcomeStore.record`):
#: given the run subject, its slug and the settled read.
OutcomeRecorder = Callable[[str, str, PrintProgress], Awaitable[object]]
#: One run's print, read by that run, not by its subject (#5613): an older run of a
#: subject a newer run printed again. ``None`` when that run's print cannot be read so.
RunReader = Callable[[PrintSubject, "PrintRun"], Awaitable[PrintProgress | None]]


class OlderRuns(Protocol):
    """A subject's older runs still open, and their outcomes (#5613,
    `outcomes.PrintOutcomeStore`)."""

    async def older_open(self, run_subject: str) -> list[PrintRun]: ...

    async def record_run(self, run: PrintRun, slug: str, progress: PrintProgress) -> object: ...


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
        record_outcome: OutcomeRecorder | None = None,
        on_read: Sequence[ReadHook] = (),
        older_runs: OlderRuns | None = None,
        read_run: RunReader | None = None,
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
        #: Each runs on every read that moved, before the settled hooks, bounded and
        #: logged as they are; idempotent for the same reasons.
        self.on_read: list[ReadHook] = list(on_read)
        self.settle_timeout = settle_timeout
        #: The progress the subject's newest run shows when it failed before queueing
        #: (``runs.newest_failure``), else None. While it is not None that is what is
        #: published, as the progress routes publish it: a follow of an older print
        #: publishing that print's progress meanwhile made the two alternate (#1837).
        #: The follow still reads the older print until it settles, for its hooks. Its
        #: settle then emits no ``print.settled`` on that subject: the subject shows the
        #: newer failure, not the older print, and that is intended (#2015).
        self.newest_failed = newest_failed
        #: Records how the print ended, once across replicas and attempts, before the
        #: hooks run (#2665): what flow triggers fire ``print.finished`` and
        #: ``print.failed`` on. A core service, not a hook, so every follow has it.
        self.record_outcome = record_outcome
        #: Each run is settled on its own (#5613): two prints of one subject at once
        #: (two order items of one library file) are both followed. On each pass the
        #: subject's older runs still open are read by their own run (``read_run``) and
        #: each settled one is recorded against its run (``older_runs.record_run``). The
        #: follow ends only once none is open. Without both, only the subject's newest
        #: run is read, as before.
        self.older_runs = older_runs
        self.read_run = read_run

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
        id, or ``library:<file id>`` for a library file's print (#1073). With
        ``older_runs``, it ends only once each older open run of the subject has settled
        too (#5613)."""
        subject = PrintSubject.from_run_subject(run_subject)
        if subject.kind == "library" and self.read_library is None:
            return "gone"
        interval = self.min_interval
        last_failure: tuple[int, str] | None = None
        last_read: PrintProgress | None = None
        #: Whether the newest run's settled read is recorded. A failed record is retried
        #: on each pass that reads it settled, as an older run's is (#5621).
        recorded = False
        #: Each older open run's last read, by run id (#5613).
        older_reads: dict[str, PrintProgress | None] = {}
        #: The older runs whose settled hooks have run, by run id (#5621).
        older_hooked: set[str] = set()
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
            moved = progress != last_read
            last_read = progress
            if moved:
                recorded = False
            if progress is not None and moved:
                await self._read_hooks(key, progress, active, heartbeat)
            if progress is not None and progress.settled and (moved or not recorded):
                # After observe, so print.settled is already published (#836). Not on
                # progress None: a print never queued has no picks. A follow kept open by
                # an older run (#5613) reads the settle again unmoved: it records it
                # again only while the record has failed (#5621).
                recorded = await self._record(key, slug, progress, active, heartbeat)
            if progress is not None and progress.settled and moved:
                # Once per settle.
                await self._settled(subject, active, heartbeat)
            older_open, older_moved = await self._older(
                subject, key, slug, older_reads, older_hooked, active, heartbeat
            )
            if (progress is None or progress.settled) and not older_open:
                return "settled"
            if changed or older_moved:
                active = self.now()
            interval = (
                self.min_interval
                if changed or older_moved
                else min(self.max_interval, interval * 2)
            )

    async def _older(
        self,
        subject: PrintSubject,
        key: str,
        slug: str,
        reads: dict[str, PrintProgress | None],
        hooked: set[str],
        active: datetime,
        heartbeat: Callable[[datetime], None],
    ) -> tuple[bool, bool]:
        """Read each of the subject's older runs still open by its own run (#5613), run
        the read hooks on each that moved, and record each settled one against its run.
        On the pass its record succeeds, moved or not, run the settled hooks once for
        that run (``hooked``, by run id, #5621).
        Whether one is still open (a read that failed counts as open: it is read again),
        and whether one moved. Nothing is published: the subject's topic shows its newest
        print, as before."""
        if self.older_runs is None or self.read_run is None:
            return False, False
        try:
            async with asyncio.timeout(self.settle_timeout):
                runs = await self._heartbeating(self.older_runs.older_open(key), active, heartbeat)
        except Exception as exc:
            # The database: follow the newest run alone this pass, as before #5613.
            logger.warning(
                "the older open runs could not be read",
                extra={"subject": key, "error": type(exc).__name__},
            )
            return False, False
        still_open = moved_any = False
        for run in runs:
            try:
                progress = await self._heartbeating(self.read_run(subject, run), active, heartbeat)
            except Exception as exc:
                # Bambuddy down, a key without the scope: read it again next pass.
                logger.warning(
                    "an older run's print could not be read",
                    extra={"subject": key, "run_id": run.id, "error": type(exc).__name__},
                )
                still_open = True
                continue
            moved = run.id not in reads or progress != reads[run.id]
            reads[run.id] = progress
            if progress is None:
                # Nothing to read it by (no sends, no LAN start): not followed.
                continue
            if moved:
                moved_any = True
                await self._read_hooks(key, progress, active, heartbeat)
            if not progress.settled:
                still_open = True
                continue
            try:
                await self._bounded(
                    self.older_runs.record_run(run, slug, progress), active, heartbeat
                )
            except Exception as exc:
                logger.warning(
                    "an older run's outcome could not be recorded",
                    extra={"subject": key, "run_id": run.id, "error": type(exc).__name__},
                )
                still_open = True
                continue
            if run.id not in hooked:
                # The subject's own hooks (the rack's archives), idempotent as they are.
                hooked.add(run.id)
                await self._settled(subject, active, heartbeat)
        return still_open, moved_any

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

    async def _record(
        self,
        key: str,
        slug: str,
        progress: PrintProgress,
        active: datetime,
        heartbeat: Callable[[datetime], None],
    ) -> bool:
        """Record the newest run's settled read. Whether it is recorded (True also when
        there is nothing to record it with)."""
        if self.record_outcome is None:
            return True
        try:
            await self._bounded(self.record_outcome(key, slug, progress), active, heartbeat)
        except Exception as exc:
            # Type only, as the hooks (#836). The follow records it again on its next
            # pass while it stays open (#5621); after it ends, the next settled read of
            # this print (the progress route's follow, a retried attempt) records it.
            logger.warning(
                "a settled print's outcome could not be recorded",
                extra={"subject": key, "error": type(exc).__name__},
            )
            return False
        return True

    async def _read_hooks(
        self,
        key: str,
        progress: PrintProgress,
        active: datetime,
        heartbeat: Callable[[datetime], None],
    ) -> None:
        for hook in self.on_read:
            try:
                await self._bounded(hook(key, progress), active, heartbeat)
            except Exception as exc:
                # Type only, as the settled hooks.
                logger.warning(
                    "a print read hook failed",
                    extra={"subject": key, "error": type(exc).__name__},
                )

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
        self, work: Awaitable[object], active: datetime, heartbeat: Callable[[datetime], None]
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
