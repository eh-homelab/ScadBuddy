"""The Bambuddy pull's reads, on the print worker (#6772, plan
`docs/superpowers/plans/2026-11-18-filament-bb-sync.md` Decisions 172-186).

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
   the date it names; then the usage rows with them, to book, oldest first in bodies of
   at most `USAGE_BODY_ROWS` rows, each under the API's body gate (#6860). The read takes
   the newest `USAGE_READ_ROWS` rows, so a gap of up to that many rows between two pulls
   is booked whole (#7002).
4. After the Schedule's pull, Bambuddy's tray assignments, to set right the untagged
   trays (#6713).

After the spools (step 2), under ``pulls.spools``, Bambuddy's colour catalog is read and
sent to SB's colour catalog (#6907): its own GET and route, each failing alone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import date

from fastapi import status
from temporalio import activity

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.errors import BAD_BODY_PROBLEM, not_configured
from scadbuddy.bambuddy.linking import ARCHIVE_OVERLAP, ARCHIVE_PAGE, MAX_ARCHIVE_PAGES
from scadbuddy.bambuddy.models import (
    Archive,
    BbColour,
    BbLocation,
    BbSupplier,
    SpoolCatalogEntry,
    SpoolUsage,
    as_utc,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.client import CrudRefusedError
from scadbuddy.filament.bambuddy_pull import try_assignments
from scadbuddy.filament.bambuddy_usage import ARCHIVE_AFTER, ARCHIVE_BEFORE
from scadbuddy.filament.bb_pull_models import (
    PULL_READ_ACTIVITY,
    BbPullApi,
    ColoursBody,
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
    UsageMark,
)
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.operations.kinds import waiting_on_bambuddy

logger = logging.getLogger(__name__)

#: How long :func:`read_catalog` waits for Bambuddy: the catalog is the pull's second
#: read, and a slow one must not hold the spools back (the client's own 30 s would).
CATALOG_TIMEOUT = 5.0

#: At most this many usage rows in one booking body (R-6860-2): with `ARCHIVES_MAX`
#: archives, every field filled, such a body is 1.6 MiB (measured), a fifth of the API's
#: 8 MiB body gate (`api/limits.py` `MAX_TEXT_BODY_BYTES`, Decision 182).
USAGE_BODY_ROWS = 5_000

#: How many usage rows the pull reads (R-7002-1). Bambuddy answers only the newest
#: ``limit`` rows, with no offset or date (R-6860-1), and puts no cap on ``limit``, so a
#: row older than this read is never booked. Prod wrote 471 rows in 60 days (at most 36
#: in a day, 304 in 30 days, measured 2026-11-22), so this is more than a year of rows at
#: the busiest month's rate: one read covers any realistic gap between two pulls. It is
#: one body's rows, so a read is one body and `bambuddy_usage._cover` sees every row of
#: the read together (R-7002-2).
USAGE_READ_ROWS = USAGE_BODY_ROWS

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
    the spool listing as ``error``, and SB's refusal of the body (a 4xx but a 429) as a
    502 ``error``; it raises any other failure."""
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
            api, settings, instance=start.instance, pull_id=start.pull_id, pulls=start.pulls
        )
    except PullInstanceChangedError:
        logger.info("the Bambuddy URL changed during the pull; nothing is pulled")
        return _skipped(start)
    except ApiError as error:
        if start.strict:
            return PullOutcome(outcome="failed", error=PullProblem.of(error))
        _failed("could not pull Bambuddy's spools", error)
        return PullOutcome(outcome="failed")
    except CrudRefusedError as refused:
        if start.strict and refused.status != status.HTTP_429_TOO_MANY_REQUESTS:
            return PullOutcome(outcome="failed", error=_refused(refused))
        if start.strict:
            raise
        _failed("could not pull Bambuddy's spools", refused)
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


def _refused(refused: CrudRefusedError) -> PullProblem:
    """SB refused what the worker read (the API's body gate, a listing past its cap, the
    schema): the same read gets the same answer, so an import is answered 502 naming the
    refusal rather than a 503 to try again (Decision 184). Logged too."""
    _failed("ScadBuddy refused the Bambuddy pull's body", refused)
    return PullProblem(
        status=status.HTTP_502_BAD_GATEWAY,
        detail=(
            f"ScadBuddy refused what the print worker read of Bambuddy ({refused}); importing"
            " again gets the same answer"
        ),
    )


def _skipped(start: PullStart) -> PullOutcome:
    error = PullProblem.of(not_configured(URL_CHANGED)) if start.strict else None
    return PullOutcome(outcome="skipped", error=error)


async def read_and_pull(
    api: BbPullApi, settings: StoredSettings, *, instance: str, pull_id: str, pulls: Pulls
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
    spool pull's report stands. Bambuddy's usage listing takes a ``limit`` and no offset
    or date, newest first (R-6860-1), so the read is the newest `USAGE_READ_ROWS` rows,
    more than a year of prod's rows (R-7002-1); the booking sends them in bodies (`usage_bodies`),
    and books only the rows not booked yet. When the usage
    read fails, no archived BB spool is made in this pull (``archived_deferred``,
    R-6707-16).

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
                usage = await client.spool_usage(limit=USAGE_READ_ROWS)
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
            pull_id=pull_id,
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
    if pulls.spools:
        await _pull_colours(api, settings, instance, report)
    if usage:
        archives: list[Archive] = []
        if applied.archives_since is not None:
            try:
                async with client_for(settings) as client:
                    archives = await _read_archives(client, applied.archives_since)
            except Exception as error:
                _failed("could not read Bambuddy's archives", error)
        await _book_usage(api, instance, pull_id, usage, archives, report)
    logger.info(
        "pulled Bambuddy's spools",
        extra={"usage_booked": report.usage_booked, "usage_unlinked": report.usage_unlinked},
    )
    return report


def usage_bodies(
    usage: Sequence[SpoolUsage], archives: Sequence[Archive], rows: int
) -> list[tuple[list[SpoolUsage], list[Archive]]]:
    """``usage`` cut into bodies of at most ``rows`` rows, oldest first (R-6860-2), each
    with the archives its rows can match (`bambuddy_usage.match_archive`'s window) and of
    those only what the booking matches by. The rows of one print share one ``created_at``
    (spec §8.2), and a body never splits them, so `bambuddy_usage._cover` sees a print's
    rows together; such a group may pass ``rows``."""
    ordered = sorted(usage, key=lambda row: (row.created_at, row.id))
    groups: list[list[SpoolUsage]] = []
    for row in ordered:
        if groups and groups[-1][-1].created_at == row.created_at:
            groups[-1].append(row)
        else:
            groups.append([row])
    chunks: list[list[SpoolUsage]] = []
    for group in groups:
        if chunks and len(chunks[-1]) + len(group) <= rows:
            chunks[-1].extend(group)
        else:
            chunks.append(list(group))
    ended = [(as_utc(a.completed_at), a) for a in archives if a.completed_at is not None]
    bodies: list[tuple[list[SpoolUsage], list[Archive]]] = []
    for chunk in chunks:
        first = chunk[0].created_at - ARCHIVE_BEFORE
        last = chunk[-1].created_at + ARCHIVE_AFTER
        matched = [
            Archive(id=a.id, printer_id=a.printer_id, completed_at=a.completed_at)
            for at, a in ended
            if at is not None and first <= at <= last
        ]
        bodies.append((chunk, matched))
    return bodies


async def _book_usage(
    api: BbPullApi,
    instance: str,
    pull_id: str,
    usage: Sequence[SpoolUsage],
    archives: Sequence[Archive],
    report: PullReport,
) -> None:
    """Book ``usage`` in bodies of `USAGE_BODY_ROWS` (`usage_bodies`), oldest first, adding
    up what each booked. A body that fails is logged and ends the booking: its rows and the
    newer ones wait for the next pull, which reads them again (R-6860-3). The last body
    carries ``read_to``, the newest row read, so the instance's usage mark moves only once
    the whole read is booked, in that body's transaction (R-7002-6). An empty read books
    nothing and sends nothing."""
    # `run_pull` skips an empty read already; keep this guard anyway: `max` below raises
    # ValueError on an empty list, and another caller may not check.
    if not usage:
        return
    bodies = usage_bodies(usage, archives, USAGE_BODY_ROWS)
    newest = max(usage, key=lambda row: (row.created_at, row.id))
    for i, (rows, matched) in enumerate(bodies):
        last = i == len(bodies) - 1
        try:
            booked = await api.book_usage(
                UsageBody(
                    instance=instance,
                    pull_id=pull_id,
                    usage=rows,
                    archives=matched,
                    read_to=UsageMark(at=newest.created_at, id=newest.id) if last else None,
                )
            )
        except Exception as error:
            _failed("could not book Bambuddy's usage rows", error)
            return
        report.usage_booked += booked.booked
        report.usage_unlinked += booked.unlinked


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


async def read_colours(settings: StoredSettings) -> list[BbColour] | None:
    """Bambuddy's colour catalog (``GET /inventory/colors``, #6907), or ``None``, logged,
    as `read_catalog` reads the spool catalog: bounded by `CATALOG_TIMEOUT`, a refusal or
    another shape logged with no Bambuddy text."""
    async with waiting_on_bambuddy():
        try:
            async with asyncio.timeout(CATALOG_TIMEOUT), client_for(settings) as client:
                return await client.colour_catalog()
        except TimeoutError:
            logger.warning(
                "no colour catalog from Bambuddy: no answer within %s s", CATALOG_TIMEOUT
            )
        except ApiError as error:
            logger.warning("no colour catalog from Bambuddy: %s", error.detail)
        except ValueError as error:
            logger.warning(
                "no colour catalog from Bambuddy: an answer of another shape (%s)",
                type(error).__name__,
            )
        except Exception as error:
            # The colours never hold the pull up (Decision 207): any other failure too.
            _failed("could not read Bambuddy's colour catalog", error)
    return None


async def _pull_colours(
    api: BbPullApi, settings: StoredSettings, instance: str, report: PullReport
) -> None:
    """Read Bambuddy's colour catalog and send it to SB's (#6907, Decisions 206-207): after
    the spools, under ``pulls.spools``, each step failing alone (``colours_failed``)."""
    colours = await read_colours(settings)
    if colours is None:
        report.colours_failed = True
        return
    try:
        applied = await api.colours(ColoursBody(instance=instance, colours=colours))
    except Exception as error:
        _failed("could not fill the colour catalog from Bambuddy's", error)
        report.colours_failed = True
        return
    report.colours = applied.added + applied.updated
    report.colours_kept = applied.kept


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
