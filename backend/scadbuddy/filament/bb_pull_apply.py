"""The Bambuddy pull's SB side, in the CRUD service (#6772, plan
`docs/superpowers/plans/2026-11-18-filament-bb-sync.md` Decisions 172-186).

The print worker reads Bambuddy (`filament/bb_pull_run.py`) and sends what it read here:
`LocalBbPull` applies it to SB's tables with `bambuddy_pull.pull`, `bambuddy_usage`,
`sensors` and `FilamentInventory.reconcile_trays`, and makes no Bambuddy call. The CRUD
routes (`api/crud_print.py`, ``…/print/filament/pull*``) and the API's in-process print
worker both run it. `pull_start` and `execute_pull` are the API's side of a run: what the
Schedule's pull or a person's import asks the worker to read, and the start of
``FilamentBbPull`` on the ``bambuddy`` queue.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta

from temporalio.client import Client

from scadbuddy.bambuddy.models import Archive, SpoolUsage
from scadbuddy.core import settings as core_settings
from scadbuddy.core.cancel import CancelToken, check, watched
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import bambuddy_usage, sensors
from scadbuddy.filament.bambuddy_pull import (
    ACTOR,
    links_elsewhere,
    parse_rows,
    pull,
    record_instance,
)
from scadbuddy.filament.bb_pull_models import (
    FILAMENT_BB_PULL_WORKFLOW,
    ColoursApplied,
    ColoursBody,
    HaBody,
    PullApplied,
    PullBody,
    PullInstanceChangedError,
    PullOutcome,
    Pulls,
    PullStart,
    TraysBody,
    UsageBody,
    UsageBooked,
)
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.filament.colour_catalog import bambuddy_rows, upsert_colours
from scadbuddy.filament.component import Filament
from scadbuddy.library.settings_store import StoredSettings

logger = logging.getLogger(__name__)

#: How long before its usage row an archive may have been made. The archive window is on
#: its ``created_at`` (the dispatch), and the row comes when the print ends; a day covers
#: a print that long, and the window is on dates.
ARCHIVE_LEAD = timedelta(days=1)
#: A ``FilamentBbPull`` run's bound: under the spool refresh's activity
#: (`workflows/spool_refresh.py` ``FILAMENT_PULL_TIMEOUT``, 5 min), which waits for it.
PULL_RUN_TIMEOUT = timedelta(minutes=4, seconds=30)


def _failed(message: str, error: Exception) -> None:
    logger.warning(message, extra={"error": type(error).__name__}, exc_info=True)


class LocalBbPull:
    """`bb_pull_models.BbPullApi` on the API's own stores. ``load`` reads the stored
    settings (blocking): a body of another Bambuddy than ``bambuddy_url``'s is refused.
    ``clock`` is the pull's time (the links' ``bb_seen_at``) and the Home Assistant
    readings'."""

    def __init__(
        self,
        filament: Filament,
        load: Callable[[], StoredSettings],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.filament = filament
        self.load = load
        self.clock = clock

    def _actor(self, pull_id: str) -> str:
        """Who the run's events name: the person whose import started it, as the API kept
        it (`remember_actor`), else ``bambuddy``. Never the worker's word (Decision 181)."""
        with self.filament.spools.transaction() as conn:
            row = conn.execute(
                "SELECT actor FROM filament_pull_actors WHERE pull_id = %s", (pull_id,)
            ).fetchone()
        return str(row["actor"]) if row is not None else ACTOR

    def _check(self, instance: str, *, links: bool = True) -> None:
        """Refuse a body of another Bambuddy than the configured one, and, for spool data
        (``links``), while the links belong to another Bambuddy (#6845 R-6845-5): the pull
        would write that Bambuddy's values onto them."""
        url = self.load().bambuddy_url
        if not url or instance_for(url) != instance:
            raise PullInstanceChangedError(instance)
        if links and links_elsewhere(self.filament, instance):
            raise PullInstanceChangedError(instance)

    async def apply(self, body: PullBody, cancel: CancelToken | None = None) -> PullApplied:
        """``cancel``: the CRUD route's (`api/disconnect.py`), which stops the pull (`pull`)."""
        return await asyncio.to_thread(self._apply, body, cancel)

    def _apply(self, body: PullBody, cancel: CancelToken | None = None) -> PullApplied:
        """The pull as `read_and_pull` ran it before #6772, from the worker's read: the
        rows parsed, `pull`, then which usage rows would book and are not booked yet, for
        the archives the worker reads next. That check fails alone, logged."""
        self._check(body.instance)
        spools, bad = parse_rows(body.spools)
        report = pull(
            self.filament,
            spools,
            now=self.clock(),
            actor=self._actor(body.pull_id),
            catalog=body.catalog or (),
            pulls=body.pulls,
            usage=body.usage,
            defer_archived=body.usage_failed,
            suppliers=body.suppliers,
            locations=body.locations,
            instance=body.instance,
            cancel=cancel,
        )
        report.catalog_failed = report.catalog_failed or body.catalog is None
        report.invalid = len(bad)
        report.invalid_bb_ids = [i for i in bad if i is not None]
        since: date | None = None
        try:
            pending = self._unbooked(body.usage) if body.usage else set()
        except Exception as error:
            _failed("could not find Bambuddy's unbooked usage rows", error)
            pending = set()
        if pending:
            since = min(row.created_at for row in body.usage if row.id in pending).date()
            since -= ARCHIVE_LEAD
        return PullApplied(report=report, archives_since=since)

    def _unbooked(self, usage: Sequence[SpoolUsage]) -> set[int]:
        with self.filament.spools.transaction() as conn:
            return bambuddy_usage.unbooked(conn, usage)

    async def book_usage(self, body: UsageBody, cancel: CancelToken | None = None) -> UsageBooked:
        """One transaction. ``cancel`` (as `apply`'s) rolls it back whole unless the booking
        had already reached its commit: the last step before the commit checks it."""
        return await asyncio.to_thread(self._book_usage, body, cancel)

    def _book_usage(self, body: UsageBody, cancel: CancelToken | None = None) -> UsageBooked:
        self._check(body.instance)
        archives: Sequence[Archive] = body.archives
        with self.filament.spools.transaction() as conn, watched(cancel, conn):
            booked = bambuddy_usage.book_usage(
                conn, body.usage, archives, actor=self._actor(body.pull_id)
            )
            check(cancel)
        return UsageBooked(booked=booked.booked, unlinked=booked.unlinked)

    async def ha_states(self, body: HaBody) -> None:
        await asyncio.to_thread(self._ha_states, body)

    def _ha_states(self, body: HaBody) -> None:
        """A state is a reading now (`sensors.apply_ha_states_on`); no states (the read
        failed) marks every Home Assistant sensor on a live place unreachable. Sensors are
        not spool data, so the links' instance does not stop them."""
        self._check(body.instance, links=False)
        with self.filament.spools.transaction() as conn:
            sensors.apply_ha_states_on(conn, body.states or {}, self.clock())

    async def reconcile_trays(self, body: TraysBody) -> int:
        def run() -> int:
            self._check(body.instance)
            return len(self.filament.inventory.reconcile_trays(body.assignments))

        return await asyncio.to_thread(run)

    async def colours(self, body: ColoursBody) -> ColoursApplied:
        return await asyncio.to_thread(self._colours, body)

    def _colours(self, body: ColoursBody) -> ColoursApplied:
        """Bambuddy's colour catalog into SB's (#6907, `colour_catalog.upsert_colours`): a
        row is linked by its Bambuddy id, and a row a person made or edited stays theirs. It
        links rows, so the links' instance stops it as it stops the spools."""
        self._check(body.instance)
        rows, bad = bambuddy_rows(body.colours)
        with self.filament.spools.transaction() as conn:
            counts = upsert_colours(conn, rows, "bambuddy")
            record_instance(conn, body.instance)
        return ColoursApplied(
            added=counts.added,
            updated=counts.updated,
            unchanged=counts.unchanged,
            kept=counts.kept,
            skipped=counts.skipped + bad,
        )


#: How long a kept actor may stay: a row left behind, because the read did not answer in
#: time or the API died mid-run, is deleted by a later `remember_actor` once it is this old.
ACTOR_KEPT = timedelta(hours=1)


def new_pull_id() -> str:
    """A ``FilamentBbPull`` run's id, its workflow id."""
    return f"filament-bb-pull-{uuid.uuid4().hex}"


def remember_actor(store: Filament, pull_id: str, actor: str) -> None:
    """Keep who started the run ``pull_id`` (a person's import), for the routes to name
    in its events (Decision 181); first delete the rows older than `ACTOR_KEPT`."""
    with store.spools.transaction() as conn:
        conn.execute(
            "DELETE FROM filament_pull_actors WHERE created_at < now() - %s", (ACTOR_KEPT,)
        )
        conn.execute(
            "INSERT INTO filament_pull_actors (pull_id, actor) VALUES (%s, %s)", (pull_id, actor)
        )


def forget_actor(store: Filament, pull_id: str) -> None:
    """Drop the run's kept actor once its read answered."""
    with store.spools.transaction() as conn:
        conn.execute("DELETE FROM filament_pull_actors WHERE pull_id = %s", (pull_id,))


def _has_ha_sensors(store: Filament) -> bool:
    """Whether a Home Assistant sensor is set up, so the worker reads Bambuddy's states.
    A failed read says yes (logged): the worker then reads them, and the states route
    marks what it can, as `read_ha_sensors` did before #6772."""
    try:
        return any(s.source == "home_assistant" for s in store.sensors.list(None))
    except Exception as error:
        _failed("could not list the Home Assistant sensors", error)
        return True


async def pull_start(
    store: Filament, load: Callable[[], StoredSettings], sync: Callable[[], FilamentSync]
) -> PullStart | None:
    """The Schedule's pull (`SpoolRefresh`), decided in the API: None (skipped) with no BB
    URL, or when no data type of ``filament_sync`` pulls (#6265 F1) or the links belong to
    another Bambuddy (#6845 R-6845-5), and no Home Assistant sensor is set up. Else the
    start, whose ``pulls`` is None when only the Home Assistant states are read: sensors
    are not spool data, so they are read whatever ``sync`` or the links say (#6268).
    ``sync`` reads the setting as it is now (#6262 Task 13)."""
    settings = await asyncio.to_thread(load)
    if not settings.bambuddy_url:
        logger.info("no Bambuddy configured; its spools are not pulled")
        return None
    ha = await asyncio.to_thread(_has_ha_sensors, store)
    filament_sync = await asyncio.to_thread(sync)
    pulls: Pulls | None = Pulls.of(filament_sync)
    instance = instance_for(settings.bambuddy_url)
    if not any(core_settings.pulls(d) for d in filament_sync.model_dump().values()):
        logger.info("Bambuddy's spools are not pulled: no Filament sync pulls")
        pulls = None
    elif await asyncio.to_thread(links_elsewhere, store, instance):
        logger.info("Bambuddy's spools are not pulled: the links belong to another Bambuddy")
        pulls = None
    if pulls is None and not ha:
        return None
    return PullStart(
        instance=instance,
        pull_id=new_pull_id(),
        pulls=pulls,
        ha=ha,
        reconcile=pulls is not None,
    )


async def execute_pull(client: Client, task_queue: str, start: PullStart) -> PullOutcome:
    """Run ``FilamentBbPull`` on ``task_queue`` (the ``bambuddy`` queue) and wait for its
    outcome, at most `PULL_RUN_TIMEOUT`. Its workflow id is ``start.pull_id``
    (`new_pull_id`), so each run has an id of its own: the Schedule's
    pull and a person's import may run at once, as they could before (each BB spool is
    taken under its own advisory lock, `bambuddy_pull.pull`)."""
    result: PullOutcome = await client.execute_workflow(
        FILAMENT_BB_PULL_WORKFLOW,
        start,
        id=start.pull_id,
        task_queue=task_queue,
        execution_timeout=PULL_RUN_TIMEOUT,
        result_type=PullOutcome,
    )
    return result
