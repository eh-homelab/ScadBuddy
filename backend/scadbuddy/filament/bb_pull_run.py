"""The Bambuddy pull's reads, on the print worker (#6772, plan
`docs/superpowers/plans/2026-11-18-filament-bb-sync.md` Decisions 172-180).

The print worker holds the Bambuddy key and reads Bambuddy for the push already
(`bb_sync_run.py`). ``FilamentBbPull``'s activity (`FilamentPullActivities`) reads what the
pull needs, in the order `bambuddy_pull.read_and_pull` and `scheduled_pull` read it before
#6772, and sends each read to SB through `bb_pull_models.BbPullApi`; SB applies it
(`bb_pull_apply.py`) and makes no Bambuddy call:

1. Home Assistant's states, when the start asks (a sensor is set up): one GET, then the
   states route; a failed read sends no states, so the sensors are marked unreachable.
2. With a data type that pulls: the spools (archived ones too, #6707), the usage rows
   (under ``pulls.spools`` or ``pulls.weights``), the suppliers and the locations (each
   under its own type), then the spool catalog (under ``pulls.spools``, bounded by
   `CATALOG_TIMEOUT`), sent as one body. Each read but the spools fails alone, logged.
3. When SB answers that usage rows would book and are not booked yet, the archives from
   the date it names; then the usage rows with them, to book.
4. After the Schedule's pull, Bambuddy's tray assignments, to set right the untagged
   trays (#6713).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date

from temporalio import activity

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.errors import BAD_BODY_PROBLEM, not_configured
from scadbuddy.bambuddy.linking import ARCHIVE_OVERLAP, ARCHIVE_PAGE, MAX_ARCHIVE_PAGES
from scadbuddy.bambuddy.models import (
    Archive,
    BbLocation,
    BbSupplier,
    SpoolCatalogEntry,
    SpoolUsage,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.bambuddy_pull import try_assignments
from scadbuddy.filament.bb_pull_models import (
    PULL_READ_ACTIVITY,
    BbPullApi,
    HaBody,
    PullBody,
    PullInstanceChangedError,
    PullOutcome,
    PullProblem,
    PullReport,
    Pulls,
    PullStart,
    TraysBody,
    UsageBody,
)
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.operations.kinds import waiting_on_bambuddy

logger = logging.getLogger(__name__)

#: How long :func:`read_catalog` waits for Bambuddy: the catalog is the pull's second
#: read, and a slow one must not hold the spools back (the client's own 30 s would).
CATALOG_TIMEOUT = 5.0

#: The import's answer when the Bambuddy URL changed between the route and the read.
URL_CHANGED = "the Bambuddy URL changed during the import; import again"


def _failed(message: str, error: Exception) -> None:
    logger.warning(message, extra={"error": type(error).__name__}, exc_info=True)


async def run_pull(
    api: BbPullApi, load: Callable[[], Awaitable[StoredSettings]], start: PullStart
) -> PullOutcome:
    """One ``FilamentBbPull``, as the module says. The settings are read here (the URL and
    the key): when their Bambuddy is not ``start.instance``'s, nothing is read or written
    (``skipped``; an import's error says so). A Schedule's pull never raises: Bambuddy or
    SB failing is ``failed``, logged. An import (``strict``) answers Bambuddy's refusal of
    the spool listing as ``error``, and raises any other failure."""
    settings = await load()
    url = settings.bambuddy_url
    if not url or instance_for(url) != start.instance:
        logger.info("the Bambuddy URL changed since the pull started; nothing is pulled")
        return _skipped(start)
    if start.ha:
        await read_ha_sensors(api, settings, start.instance)
    if start.pulls is None:
        return PullOutcome(outcome="skipped")
    try:
        report = await read_and_pull(
            api, settings, instance=start.instance, actor=start.actor, pulls=start.pulls
        )
    except PullInstanceChangedError:
        logger.info("the Bambuddy URL changed during the pull; nothing is pulled")
        return _skipped(start)
    except ApiError as error:
        if start.strict:
            return PullOutcome(outcome="failed", error=PullProblem.of(error))
        _failed("could not pull Bambuddy's spools", error)
        return PullOutcome(outcome="failed")
    except Exception as error:
        if start.strict:
            raise
        _failed("could not pull Bambuddy's spools", error)
        return PullOutcome(outcome="failed")
    if report.failed_bb_ids or report.invalid:
        logger.warning(
            "some Bambuddy spools were not pulled",
            extra={"bb_spool_ids": report.failed_bb_ids, "invalid": report.invalid},
        )
    if start.reconcile:
        await reconcile_trays(api, settings, start.instance)
    return PullOutcome(outcome="pulled", report=report)


def _skipped(start: PullStart) -> PullOutcome:
    error = PullProblem.of(not_configured(URL_CHANGED)) if start.strict else None
    return PullOutcome(outcome="skipped", error=error)


async def read_and_pull(
    api: BbPullApi, settings: StoredSettings, *, instance: str, actor: str, pulls: Pulls
) -> PullReport:
    """Read BB's spools, the archived ones too (#6707), and send them to SB to pull
    (``pulls`` the data types taken); SB skips a malformed row and counts it ``invalid``.
    Then book BB's usage rows on linked spools, while ``pulls.spools`` or
    ``pulls.weights`` (#6265 F1). BB not configured is `client_for`'s 409; BB's own errors
    on the spools are its client's.

    The usage rows are read right after the spools, so a deduction BB writes between the
    two reads shows in the usage rows, never only in a spool's value. The archives are
    read after the pull, and only when SB answers that a usage row that would book is not
    booked yet, so the rows of a spool this pull made are matched with their archives too.
    The usage read, the archive read and the booking each fail alone: logged, and the
    spool pull's report stands. When the usage read fails, no archived BB spool is made in
    this pull (``archived_deferred``, R-6707-16).

    BB's suppliers are read only under ``pulls.suppliers``, and its locations only under
    ``pulls.locations``, whatever ``pulls.spools`` says (#6265 Decision 16); each read
    fails alone, logged, and leaves its rows alone."""
    usage: list[SpoolUsage] = []
    usage_failed = False
    suppliers: list[BbSupplier] | None = None
    locations: list[BbLocation] | None = None
    async with client_for(settings) as client:
        rows = await client.spool_rows(include_archived=True)
        try:
            if pulls.spools or pulls.weights:
                usage = await client.spool_usage()
        except Exception as error:
            usage_failed = True
            _failed("could not read Bambuddy's usage rows", error)
        try:
            if pulls.suppliers:
                suppliers = await client.suppliers()
        except Exception as error:
            _failed("could not read Bambuddy's suppliers", error)
        try:
            if pulls.locations:
                locations = await client.locations()
        except Exception as error:
            _failed("could not read Bambuddy's locations", error)
    catalog = await read_catalog(settings) if pulls.spools else []
    applied = await api.apply(
        PullBody(
            instance=instance,
            actor=actor,
            pulls=pulls,
            spools=list(rows),
            usage=usage,
            usage_failed=usage_failed,
            suppliers=suppliers,
            locations=locations,
            catalog=catalog,
        )
    )
    report = applied.report
    if usage:
        archives: list[Archive] = []
        if applied.archives_since is not None:
            try:
                async with client_for(settings) as client:
                    archives = await _read_archives(client, applied.archives_since)
            except Exception as error:
                _failed("could not read Bambuddy's archives", error)
        try:
            booked = await api.book_usage(
                UsageBody(
                    instance=instance,
                    actor=actor,
                    usage=usage,
                    # Only what the booking matches by (`bambuddy_usage.match_archive`).
                    archives=[
                        Archive(id=a.id, printer_id=a.printer_id, completed_at=a.completed_at)
                        for a in archives
                    ],
                )
            )
        except Exception as error:
            _failed("could not book Bambuddy's usage rows", error)
        else:
            report.usage_booked, report.usage_unlinked = booked.booked, booked.unlinked
    logger.info(
        "pulled Bambuddy's spools",
        extra={"usage_booked": report.usage_booked, "usage_unlinked": report.usage_unlinked},
    )
    return report


async def _read_archives(client: BambuddyClient, since: date) -> list[Archive]:
    """The archives made from ``since`` on, paged as `linking` pages them (an overlap, at
    most `MAX_ARCHIVE_PAGES` pages): the newest 50 would miss the archives of the older
    usage rows a first pull books (#6598). A read that hits the cap logs it and keeps
    what it has."""
    found: dict[int, Archive] = {}
    for page in range(MAX_ARCHIVE_PAGES):
        rows = await client.archives(
            date_from=since, limit=ARCHIVE_PAGE, offset=page * (ARCHIVE_PAGE - ARCHIVE_OVERLAP)
        )
        found.update((row.id, row) for row in rows)
        if len(rows) < ARCHIVE_PAGE:
            break
    else:
        logger.warning(
            "stopped reading Bambuddy's archives for usage rows; the archives of the oldest"
            " pending rows were not read (pages probably come newest first)",
            extra={"since": since.isoformat(), "pages": MAX_ARCHIVE_PAGES},
        )
    return list(found.values())


async def read_catalog(settings: StoredSettings) -> list[SpoolCatalogEntry] | None:
    """Bambuddy's spool catalog (``GET /inventory/catalog``), or ``None``, logged, when
    Bambuddy refuses, answers in another shape or no JSON (it has no such route before
    1.2.6b1), or does not answer within :data:`CATALOG_TIMEOUT`: the pull then leaves the
    insert types alone. Bounded and a wait on Bambuddy as `try_assignments` is."""
    async with waiting_on_bambuddy():
        try:
            async with asyncio.timeout(CATALOG_TIMEOUT), client_for(settings) as client:
                return await client.spool_catalog()
        except TimeoutError:
            logger.warning("no spool catalog from Bambuddy: no answer within %s s", CATALOG_TIMEOUT)
        except ApiError as error:
            if error.type == BAD_BODY_PROBLEM:
                # No JSON, or not a list (R-T7-10): the client's own detail, no Bambuddy text.
                logger.warning(
                    "no spool catalog from Bambuddy: an answer of another shape (%s)", error.detail
                )
            else:
                logger.warning("no spool catalog from Bambuddy: %s", error.detail)
        except ValueError as error:
            # A pydantic ValidationError: rows of another shape. Only the type is logged: its
            # text quotes Bambuddy's values (#6580).
            logger.warning(
                "no spool catalog from Bambuddy: an answer of another shape (%s)",
                type(error).__name__,
            )
    return None


async def read_ha_sensors(api: BbPullApi, settings: StoredSettings, instance: str) -> None:
    """Read Bambuddy's Home Assistant states and send them to SB's location sensors
    (#6268): one GET. The GET reads every entity, because Bambuddy 1.3.1's route takes no
    list of ids (#6679, plan Decision 26). It never fails the pull: a failed read is logged
    with its type only, and SB is sent no states, so every Home Assistant sensor on a live
    place is marked unreachable (it got no number)."""
    states: dict[str, str | None] | None = None
    try:
        async with client_for(settings) as client:
            entities = await client.ha_entities(None)
        states = {e.entity_id: e.state for e in entities}
    except Exception as error:
        _failed("could not read Bambuddy's Home Assistant sensors", error)
    try:
        await api.ha_states(HaBody(instance=instance, states=states))
    except Exception as error:
        _failed("could not record the Home Assistant sensors' readings", error)


async def reconcile_trays(api: BbPullApi, settings: StoredSettings, instance: str) -> None:
    """Set right each untagged tray whose placed spool disagrees with what the printer
    reports, by Bambuddy's assignments (`FilamentInventory.reconcile_trays`, #6713): the
    worker reads them, SB moves the spools. A failed assignment read (`try_assignments`)
    reconciles nothing this pass. It never fails the pull: a failure is logged with its
    type only."""
    try:
        assignments = await try_assignments(settings)
        if assignments is None:
            logger.info("no tray reconcile: Bambuddy's assignments could not be read")
            return
        moved = await api.reconcile_trays(TraysBody(instance=instance, assignments=assignments))
    except Exception as error:
        _failed("could not reconcile the trays with Bambuddy's assignments", error)
        return
    if moved:
        logger.info("reconciled trays with Bambuddy's assignments", extra={"moved": moved})


class FilamentPullActivities:
    """``FilamentBbPull``'s activity. ``api`` is `RemoteBbPull` on the print worker and
    `bb_pull_apply.LocalBbPull` where the API serves the ``bambuddy`` queue; ``load`` reads
    the stored settings (the Bambuddy URL and key)."""

    def __init__(self, api: BbPullApi, load: Callable[[], Awaitable[StoredSettings]]) -> None:
        self.api = api
        self.load = load

    @activity.defn(name=PULL_READ_ACTIVITY)
    async def pull_read(self, start: PullStart) -> PullOutcome:
        return await run_pull(self.api, self.load, start)
