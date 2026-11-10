"""One pass of the print watcher (#4699): import the prints ScadBuddy did not start.

The housekeeping sweep ``housekeeping_auto_import_prints`` runs it on the prune Schedule
(every `PRUNE_INTERVAL`, 300 s) in the API's ``library`` worker, the one process with SQL
access (root CLAUDE.md, "Database access (#5295)"). With ``print_auto_import`` off it
reads nothing from Bambuddy and no printer.

A pass reads the archives Bambuddy made since the watcher was switched on (at most
`LOOKBACK` back), and takes each one that has a printer, no link and no row of its own:

1. Under the archive's import lock (`PrintLinkStore.import_lock`, the lock "Import print"
   takes), it runs the link passes for the archive (`linking.link_archive`). A print
   ScadBuddy made is linked to its run there, and left alone.
2. It chooses where the 3MF is, in a fixed order, among the sources the settings allow
   (`StoredSettings.auto_import_sources`): the archive's sliced file, the archive's
   slicer project, a Bambuddy library file with the archive's hash (a file with its name
   only may be another version, so it is never taken). Last, and only when allowed, the
   printer's storage over FTP: that file is read now, while the print runs, and kept as a
   Bambuddy library file in the inbox folder, as the printer overwrites its cache.
3. It links a model only with confidence: an earlier import of the same printed file
   (its hash) names one model, or the job's name has exactly one model's words. Anything
   else, and a print no source had, goes to the inbox ("Unsorted imports") with why.

A row, its link and its activity row are one transaction (`AutoImportStore.record`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.linking import link_archive
from scadbuddy.bambuddy.models import ArchiveDetail
from scadbuddy.bambuddy.print_import import (
    FoundSource,
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
#: The fixed order of the sources Bambuddy keeps; the printer comes after them.
BAMBUDDY_SOURCES: tuple[ImportSource, ...] = ("archive", "archive_source", "library")

NO_MATCH = "no model matched with confidence"


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
        for archive in [archive for archive in fresh if archive.id not in taken][:MAX_IMPORTS]:
            try:
                async with self.links.import_lock(archive.id):
                    state = await self._take(archive)
            except ImportBusyError:
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
        if await link_archive(self.client, self.uploads, self.links, archive):
            return None  # ScadBuddy's own print, linked to its run now.
        if await self.links.linked(archive.id) is not None:
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
        recorded = await self.store.record(row, plate_id=archive.plate_id)
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
        and the copy this call uploaded (to delete when the row is not written)."""
        folder = self.settings.library_folder_id
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
        source = found[0]
        try:
            read = await read_source(self.client, archive, source)
            file_id, uploaded = await keep_printer_file(self.client, read, folder)
        except ApiError as error:
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
