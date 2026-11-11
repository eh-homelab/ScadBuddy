"""One pass of the print watcher (#4699): import the prints ScadBuddy did not start.

The housekeeping sweep ``housekeeping_auto_import_prints`` runs it on the prune Schedule
(every `PRUNE_INTERVAL`, 300 s) in the API's ``library`` worker, the one process with SQL
access (root CLAUDE.md, "Database access (#5295)"). With ``print_auto_import`` off it
reads nothing from Bambuddy and no printer.

A pass reads the archives Bambuddy made since the watcher was switched on (at most
`LOOKBACK` back), and takes each one that has a printer, no link and no row of its own:

1. Under the archive's import lock (`PrintLinkStore.import_lock`, the lock "Import print"
   takes), it runs the link passes for the archive (`linking.link_archive`). A print
   ScadBuddy made is linked to its run there, and left alone. It then checks again that
   no pass took the archive meanwhile (`AutoImportStore.known`): both Schedules run it.
2. It chooses where the 3MF is, in a fixed order, among the sources the settings allow
   (`StoredSettings.auto_import_sources`): the archive's sliced file, the archive's
   slicer project, a Bambuddy library file with the archive's hash (a file with its name
   only may be another version, so it is never taken). Last, and only when allowed, the
   printer's storage over FTP: that file is read now, while the print runs, and taken
   only when its bytes hash to the archive's ``content_hash`` (the printed file's), so a
   newer file of the same name is never taken; it is then kept as a Bambuddy library file
   in the inbox folder, as the printer overwrites its cache. An archive with no hash
   takes no printer file. A printer read or upload that fails for a reason that may pass
   (`retryable`: no answer, 408, 429, 5xx) raises, so no row is written and the next
   pass tries again, until `past_retries`; then, and for any other failure (401, 403,
   404, 413, 422, a hash that differs), the print goes to the inbox with the reason.
3. It links a model only with confidence: an earlier import of the same printed file
   (its hash) names one model, or the job's name has exactly one model's words. Anything
   else, and a print no source had, goes to the inbox ("Unsorted imports") with why.

A row, its link and its activity row are one transaction (`AutoImportStore.record`).
The library files' queue items are linked once per pass (`linking.link_library_prints`),
before the archives. An archive whose import raises is logged and skipped, so it never
holds back the archives after it; the next pass tries it again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import status

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.bambuddy.linking import link_archive, link_library_prints
from scadbuddy.bambuddy.models import ArchiveDetail
from scadbuddy.bambuddy.print_import import (
    FoundSource,
    SourceFile,
    exact_slugs,
    find_sources,
    forget_copy,
    keep_printer_file,
    printer_sources,
    read_source,
    suggested_slugs,
)
from scadbuddy.bambuddy.print_links import ImportBusyError, ImportSource, PrintLinkStore
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.print_watch.store import AutoImport, AutoImportStore, AutoMatchedBy

logger = logging.getLogger(__name__)

#: How far back a pass reads, whatever the floor: an archive older than this that no pass
#: took (the watcher was down) is left for "Import print".
LOOKBACK = timedelta(days=2)
#: One page of ``GET /archives/``, and how many pages a pass reads at most.
ARCHIVE_PAGE = 100
MAX_PAGES = 5
#: How many archives one pass takes at most, oldest first; the rest wait for the next
#: pass, so a pass stays inside its activity's timeout.
MAX_IMPORTS = 20
#: How many printer files with the print's name one archive reads at most, to find the
#: one whose bytes are the printed file's.
MAX_PRINTER_READS = 3
#: The fixed order of the sources Bambuddy keeps; the printer comes after them.
BAMBUDDY_SOURCES: tuple[ImportSource, ...] = ("archive", "archive_source", "library")

NO_MATCH = "no model matched with confidence"
#: Bambuddy answers a later pass may not meet: a timeout, a rate limit, a server error.
RETRY_STATUSES = frozenset({status.HTTP_408_REQUEST_TIMEOUT, status.HTTP_429_TOO_MANY_REQUESTS})
#: How long after a print ends a failed printer read or upload is tried again. Each
#: Schedule runs a pass every `PRUNE_INTERVAL` (300 s), so 3 h is about 70 tries: enough
#: to outlast a printer or Bambuddy restart, and far inside `LOOKBACK`. The printer keeps
#: the file in its cache until a later job overwrites it, so the file is still there.
RETRY_AFTER_END = timedelta(hours=3)
#: The same bound for a print Bambuddy gives no end for yet, from its archive's making:
#: longer than most prints, so a running print is not sent to the inbox early.
RETRY_AFTER_START = timedelta(hours=24)


def retryable(error: ApiError) -> bool:
    """Whether a failed printer read or upload may work on the next pass: Bambuddy (or
    the printer behind it) did not answer, or answered 408, 429 or 5xx. Any other answer
    (401, 403, 404, 413, 422, another 4xx) and ScadBuddy's own refusals (a file too large
    or not a 3MF) would only come back."""
    if error.type != UNAVAILABLE_PROBLEM:
        return False
    upstream = error.extensions.get("bambuddy_status")
    return upstream is None or upstream in RETRY_STATUSES or upstream >= 500


def past_retries(archive: ArchiveDetail, now: datetime | None = None) -> bool:
    """Whether a transient failure for ``archive`` is recorded in the inbox, not tried
    again: past `RETRY_AFTER_END` after its end, or `RETRY_AFTER_START` after its archive
    was made while it has no end. Without the bound, a printer that never answers would
    be read again every pass until the archive left `LOOKBACK`, holding a pass's slots."""
    now = now or datetime.now(UTC)
    ended = _utc(archive.completed_at)
    if ended is not None:
        return now >= ended + RETRY_AFTER_END
    made = _utc(archive.created_at or archive.started_at)
    return made is not None and now >= made + RETRY_AFTER_START


@dataclass
class PassResult:
    linked: int = 0
    inbox: int = 0
    #: ScadBuddy's own prints, archives another import was taking, and those a link or
    #: an earlier pass took meanwhile.
    skipped: int = 0


def _utc(moment: datetime | None) -> datetime | None:
    """Bambuddy writes naive UTC times."""
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _job(archive: ArchiveDetail) -> str | None:
    return archive.print_name or archive.filename


async def _archives(client: BambuddyClient, since: datetime, skip: set[int]) -> list[ArchiveDetail]:
    """The archives with a printer made since ``since``, oldest first, each once."""
    # Bambuddy filters on whole days; a day earlier covers a date in another time zone.
    date_from = (since - timedelta(days=1)).date()
    rows: dict[int, ArchiveDetail] = {}
    for page in range(MAX_PAGES):
        batch = await client.archive_outcomes(
            date_from=date_from,
            limit=ARCHIVE_PAGE,
            offset=page * ARCHIVE_PAGE,
            skip_invalid=True,
        )
        for row in batch:
            rows[row.id] = row.model_copy(
                update={"created_at": _utc(row.created_at), "started_at": _utc(row.started_at)}
            )
        if len(batch) < ARCHIVE_PAGE:
            break
    return sorted(
        (
            row
            for row in rows.values()
            if row.printer_id is not None
            and row.printer_id not in skip
            and row.created_at is not None
            and row.created_at >= since
        ),
        key=lambda row: row.id,
    )


def _bambuddy_source(
    sources: Sequence[FoundSource], allowed: Sequence[ImportSource]
) -> FoundSource | None:
    for kind in BAMBUDDY_SOURCES:
        if kind not in allowed:
            continue
        for source in sources:
            if (
                source.kind == kind
                and source.available
                and (kind != "library" or source.matched_by == "hash")
            ):
                return source
    return None


def _why_none(sources: Sequence[FoundSource], allowed: Sequence[ImportSource]) -> str:
    reasons = []
    for source in sources:
        if source.kind not in allowed:
            continue
        if source.available and source.kind == "library":
            reasons.append(f"library: {source.name} has this print's name but not its hash")
        elif not source.available:
            reasons.append(f"{source.kind}: {source.reason}")
    if "printer" not in allowed:
        reasons.append("printer: not allowed in Settings")
    return "no source had the 3MF (" + "; ".join(dict.fromkeys(reasons)) + ")"


class AutoImporter:
    """One pass over Bambuddy's new archives (`run`)."""

    def __init__(
        self,
        client: BambuddyClient,
        settings: StoredSettings,
        *,
        links: PrintLinkStore,
        uploads: BambuddyUploadStore,
        store: AutoImportStore,
        slugs: Callable[[], Awaitable[list[str]]],
        exists: Callable[[str], Awaitable[bool]],
    ) -> None:
        self.client = client
        self.settings = settings
        self.links = links
        self.uploads = uploads
        self.store = store
        self._slugs = slugs
        self._exists = exists
        self._known_slugs: list[str] | None = None

    async def run(self, since: datetime) -> PassResult:
        result = PassResult()
        floor = max(since, datetime.now(UTC) - LOOKBACK)
        skip = set(self.settings.print_auto_import_skip_printers or [])
        archives = await _archives(self.client, floor, skip)
        if not archives:
            return result
        linked = await self.links.linked_among([archive.id for archive in archives])
        fresh = [archive for archive in archives if archive.id not in linked]
        taken = await self.store.known(fresh)
        todo = [archive for archive in fresh if archive.id not in taken][:MAX_IMPORTS]
        if todo:
            # Once per pass, not per archive: `link_archive` below skips it.
            await link_library_prints(self.client, self.links)
        for archive in todo:
            try:
                async with self.links.import_lock(archive.id):
                    state = await self._take(archive)
            except ImportBusyError:
                state = None
            except Exception:
                # One archive that fails (a Bambuddy error on its sources) must not hold
                # back the archives after it; the next pass tries it again.
                logger.exception(
                    "could not import a print ScadBuddy did not start",
                    extra={"archive_id": archive.id},
                )
                state = None
            if state == "linked":
                result.linked += 1
            elif state == "inbox":
                result.inbox += 1
            else:
                result.skipped += 1
        return result

    async def _slug_list(self) -> list[str]:
        if self._known_slugs is None:
            self._known_slugs = await self._slugs()
        return self._known_slugs

    async def _match(self, archive: ArchiveDetail) -> tuple[str | None, AutoMatchedBy | None]:
        if archive.content_hash:
            earlier = sorted(await self.store.slugs_by_hash(archive.content_hash))
            standing = [slug for slug in earlier if await self._exists(slug)]
            if len(standing) == 1:
                return standing[0], "earlier_import"
        named = exact_slugs(await self._slug_list(), _job(archive))
        if len(named) == 1:
            return named[0], "job_name"
        return None, None

    async def _take(self, archive: ArchiveDetail) -> str | None:
        """Import one archive under its lock: "linked", "inbox", or None when it is left
        alone."""
        if await link_archive(self.client, self.uploads, self.links, archive, library_prints=False):
            return None  # ScadBuddy's own print, linked to its run now.
        if await self.links.linked(archive.id) is not None:
            return None
        if await self.store.known([archive]):
            # The other Schedule's pass took it while this one waited for the lock: read
            # no source again (#6011).
            return None
        allowed = self.settings.auto_import_sources()
        sources = await find_sources(self.client, archive, printer=False)
        source = _bambuddy_source(sources, allowed)
        row = AutoImport(
            archive_id=archive.id,
            printer_id=archive.printer_id,
            print_name=archive.print_name,
            content_hash=archive.content_hash,
            started_at=archive.started_at,
            state="inbox",
        )
        kept: int | None = None
        if source is not None:
            row.source = source.kind
            row.library_file_id = source.library_file_id
            row.filename = source.name or archive.filename or f"print-{archive.id}.3mf"
            row.size = source.size
            row.sha256 = source.sha256
        elif "printer" in allowed:
            row, kept = await self._printer_file(archive, row, sources, allowed)
        else:
            row.reason = _why_none(sources, allowed)
        slug, matched_by = await self._match(archive)
        row.suggested = [
            *([slug] if slug is not None else []),
            *(s for s in suggested_slugs(await self._slug_list(), _job(archive)) if s != slug),
        ]
        if slug is not None and row.source is not None:
            row.state, row.slug, row.matched_by = "linked", slug, matched_by
        elif row.source is not None:
            row.reason = NO_MATCH
        try:
            recorded = await self.store.record(row, plate_id=archive.plate_id)
        except BaseException:
            if kept is not None:
                await forget_copy(self.client, kept)
            raise
        if not recorded:
            if kept is not None:
                await forget_copy(self.client, kept)
            return None
        logger.info(
            "imported a print ScadBuddy did not start",
            extra={
                "archive_id": archive.id,
                "state": row.state,
                "source": row.source,
                "matched_by": row.matched_by,
            },
        )
        return row.state

    async def _printer_file(
        self,
        archive: ArchiveDetail,
        row: AutoImport,
        sources: Sequence[FoundSource],
        allowed: Sequence[ImportSource],
    ) -> tuple[AutoImport, int | None]:
        """The printer's copy, read now and kept in the library inbox folder; the row,
        and the copy this call uploaded (to delete when the row is not written). Only a
        file whose bytes hash to the archive's ``content_hash`` is kept: the hash is
        compared before the upload, so no other file is ever copied."""
        folder = self.settings.library_folder_id
        if not archive.content_hash:
            row.reason = (
                _why_none(sources, allowed)
                + "; the printer's file is taken only when its bytes are the printed file's,"
                " and Bambuddy gave no hash for this print"
            )
            return row, None
        if folder is None:
            row.reason = (
                _why_none(sources, allowed)
                + "; a printer file is kept in Bambuddy's library inbox folder, and none is"
                " chosen in Settings"
            )
            return row, None
        listed = await printer_sources(self.client, archive)
        found = [s for s in listed if s.available]
        if not found:
            row.reason = _why_none([*sources, *listed], allowed)
            return row, None
        source: FoundSource | None = None
        read: SourceFile | None = None
        problems: list[str] = []
        failed: ApiError | None = None
        for candidate in found[:MAX_PRINTER_READS]:
            try:
                data = await read_source(self.client, archive, candidate)
            except ApiError as error:
                problems.append(f"{candidate.path} could not be read: {error.detail}")
                if retryable(error):
                    failed = error
                continue
            if data.sha256 == archive.content_hash:
                source, read = candidate, data
                break
            problems.append(f"{candidate.path} is not the printed file (its hash differs)")
        if source is None or read is None:
            if failed is not None and not past_retries(archive):
                # A read that failed now may work on the next pass: write no row, which
                # `known` would never retry (#6011).
                raise failed
            row.reason = "no source had the 3MF (printer: " + "; ".join(problems) + ")"
            return row, None
        try:
            file_id, uploaded = await keep_printer_file(self.client, read, folder)
        except ApiError as error:
            if retryable(error) and not past_retries(archive):
                raise  # for the next pass to try again (#6011)
            row.reason = f"the printer's file could not be kept: {error.detail}"
            return row, None
        row.source = "printer"
        row.library_file_id = file_id
        row.printer_path = source.path
        row.filename = read.name
        row.size = len(read.data)
        row.sha256 = read.sha256
        return row, file_id if uploaded else None


async def auto_import(
    client: BambuddyClient,
    settings: StoredSettings,
    since: datetime,
    *,
    links: PrintLinkStore,
    uploads: BambuddyUploadStore,
    store: AutoImportStore,
    slugs: Callable[[], list[str]],
    exists: Callable[[str], bool],
) -> PassResult:
    """One pass; ``slugs`` and ``exists`` are the catalogue's, run in a thread."""

    async def all_slugs() -> list[str]:
        return await asyncio.to_thread(slugs)

    async def model_exists(slug: str) -> bool:
        return await asyncio.to_thread(exists, slug)

    return await AutoImporter(
        client,
        settings,
        links=links,
        uploads=uploads,
        store=store,
        slugs=all_slugs,
        exists=model_exists,
    ).run(since)
