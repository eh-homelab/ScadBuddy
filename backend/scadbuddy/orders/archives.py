"""An attempt's Bambuddy archives (#4253, spec 2026-10-18 §8.2).

A print run queues one item per plate, and each queue item's print is an archive, which
``print_links`` names once Bambuddy reports it (``print_sends`` by ``run_id``, then the
link by queue item: ``OrderStore.run_archives``). A LAN start has no queue item: its
archive is the one ``lan_print_archives`` records for the run (#8124). When nobody read
the print yet, the attempt's read records it first (:func:`link_lan_archives`, #8281):
from the archives a link by content hash gives the run's subject, by the name of the
file each one printed, as the print's read does. Once the attempt's print settled, the
attempt copies what the archives say: their ids, the printer's start and end, the
seconds and grams summed (:func:`mirror_archives`). That runs when the print's outcome
is heard (``printing.py`` ``listen_for_prints``) and again whenever the attempt's
archives are read (``GET …/fulfilments/{f}/archives``) or the media sweep steps its
import, so a link Bambuddy made late is still copied. An attempt by hand that names its
archives copies them when it is recorded or edited too (``api/orders.py``
``record_fulfilment``, ``update_fulfilment``, #6132).

A LAN attempt takes its start, end, seconds and grams from ScadBuddy's own record of the
run (#8282, :func:`lan_facts`), with or without a Bambuddy and an archive; an archive
gives only a fact that record lacks, and its media. A queue attempt copies all of them
from its archives, as before.

An archive's ``finish_`` photo and its timelapse are copied into the attempt's
attachments (§6.2, #4251): the bytes are pulled once through the seam into the claim store,
kept as ScadBuddy's own blob and recorded as the attachment, with the role ``finished`` or
``timelapse`` (:func:`copy_in`). Nothing goes the other way. The copy is imported with no
click (#7233, spec 2026-11-05 §4.1): a hand attempt with an archive, a heard outcome and a
read of the archives start the attempt's ``AttemptMedia`` workflow, whose step is
:func:`mirror_attempt`. The copy route stays, for a chosen photo and as the fallback.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.lan_files import lan_subtask, starts_by_files
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.orders import attachments
from scadbuddy.orders.attempt_media import MediaStarter, start_media
from scadbuddy.orders.integrations import (
    ArchiveFacts,
    MediaTooLargeError,
    NotLinkedError,
    OrderIntegrations,
    not_linked,
)
from scadbuddy.orders.lifecycle import Lifecycle
from scadbuddy.orders.models import (
    Actor,
    ArchiveCopyMedia,
    Attachment,
    AttachmentRole,
    Fulfilment,
)
from scadbuddy.orders.problems import PROBLEMS
from scadbuddy.orders.store import (
    ArchiveCopyExistsError,
    LanRecord,
    OrderStore,
    fulfilment_subject,
    item_subject,
)
from scadbuddy.print_watch.sweep import LOOKBACK
from scadbuddy.store import BlobStore
from scadbuddy.workflows.attempt_media import AttemptMediaStep

logger = logging.getLogger(__name__)

#: The sweep looks at the attempts made this recently (spec 2026-11-05 §4.6).
SWEEP_DAYS = timedelta(days=14)
#: The sweep starts a step for an attempt last read for media at least this long ago.
SWEEP_CHECK_EVERY = timedelta(hours=1)
#: The most attempts one sweep starts (#7453). Each start may wait `START_TIMEOUT` (5 s),
#: so 40 take at most 200 s of the sweep's `ATTEMPT_MEDIA_TIMEOUT` (5 min), with room for
#: the read; the rest wait for the next pass.
SWEEP_BATCH = 40
#: How many archives that may be a LAN run's one read looks at, at most (#8281,
#: `link_lan_archives`): each is one archive read of Bambuddy.
LAN_CANDIDATES = 20
#: How long after a LAN start a link by content hash can still be its archive's, for
#: `link_lan_archives` (#8306). The links it reads are the ones the print watcher makes
#: (`print_watch/sweep.py`: a pass every 300 s takes the archives Bambuddy made at most
#: `LOOKBACK` back), and Bambuddy makes the archive when the print starts. The Prints
#: list makes its own links later, but records each start itself
#: (`print_history._link_lan_prints`).
LAN_LINK_WINDOW = LOOKBACK
#: How long after its print completed an archive can still gain a timelapse (spec
#: 2026-11-05 §4.4): Bambuddy writes it minutes after, and the UI says "waiting" till then.
FRESH = timedelta(hours=1)

ROLES: dict[ArchiveCopyMedia, AttachmentRole] = {
    "finished_photo": "finished",
    "timelapse": "timelapse",
}


class ArchiveOffer(BaseModel):
    """One of the attempt's archives, and what of it can be copied (§8.2)."""

    archive_id: int
    #: Bambuddy's own status of the print (``completed``, ``failed``, …).
    status: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    actual_seconds: int | None = None
    grams: float | None = None
    #: The photos Bambuddy took as the print finished, oldest first.
    finish_photos: list[str] = Field(default_factory=list)
    #: Whether the archive has a timelapse in Bambuddy to copy.
    timelapse: bool = False
    #: The attachment the attempt, or its item, holds of this archive's finish photo,
    #: if any: what is attached now, not what the import started by this read will
    #: attach (spec 2026-11-05 §4.4).
    finished_attachment_id: str | None = None
    #: The same for the archive's timelapse.
    timelapse_attachment_id: str | None = None
    #: Whether the import refused the finish photo as larger than the upload limit, and
    #: the limit is no higher now (#7281 review): no import tries it again, and the copy
    #: button is refused too, so it is attached by hand.
    finished_too_large: bool = False
    #: The same for the archive's timelapse.
    timelapse_too_large: bool = False


class AttemptArchives(BaseModel):
    """``GET …/fulfilments/{f}/archives``."""

    #: Whether a Bambuddy is linked; without one there are no archives to read.
    linked: bool
    #: The attempt with what its archives say copied onto it.
    attempt: Fulfilment
    archives: list[ArchiveOffer]


def _refused(slug: str, detail: str) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, detail, type_=PROBLEMS + slug)


def _sum[T: (int, float)](values: list[T | None]) -> T | None:
    known = [v for v in values if v is not None]
    return sum(known) if known else None


@dataclass(frozen=True)
class OwnFacts:
    """What ScadBuddy's own record says of a LAN run (:func:`lan_facts`); ``None`` for a
    fact the record does not give."""

    started_at: datetime | None
    finished_at: datetime | None
    actual_seconds: int | None
    actual_grams: float | None


def lan_facts(record: LanRecord) -> OwnFacts:
    """A LAN run's facts from ScadBuddy's own record (#8282, `OrderStore.lan_record`).

    - The start is the first start's ``started_at``: the report that showed the printer
      take the plate.
    - Each start ends at the printer report that showed its plate end. The start records
      that time itself (``LanStarted.ended_at``, #8321) once a later start of the run
      waited for it. A start with no such time ends where the ledger booked its end at a
      report (`filament/prints.py` `plate_ended`, ``data.ended`` ``report``). The last
      start with neither ends when the run's follow read the print settled
      (``print_outcomes.settled_at``). The end is the last start's end.
    - The seconds are the sum over the starts of the end less the start. An earlier start
      with no timed end has no end in the record, so there are no seconds.
    - The grams are the plates' sliced grams (``LanStarted.grams_by_slot``, #8321, every
      slot whatever its tray holds) when every plate printed to the end: each earlier
      start ended ``done`` and the run's outcome is ``finished``. Sliced grams are what
      the ledger books as the actual for such a plate too.
    - Else the grams are what the ledger booked as used: each start books the sliced grams
      of each slot whose tray holds an SB spool, and its end books them as the actual when
      the plate printed to the end (`filament/print_uses.py`). There are no grams when a
      start booked no use, when a use is still open, or when an end dropped its estimate
      (a plate that did not finish): the record does not say what such a plate used. A
      start from before #8321 has no sliced grams, so its run takes this rule.
    """
    starts = record.starts
    timed: dict[str, datetime] = {}
    for use in record.uses:
        if use.timed and use.ended_at is not None:
            first = timed.get(use.subtask)
            timed[use.subtask] = use.ended_at if first is None else min(first, use.ended_at)
    ends = [start.ended_at or timed.get(start.subtask_name) for start in starts]
    if ends[-1] is None:
        ends[-1] = record.settled_at
    seconds: int | None = None
    spans = [
        (end - start.started_at).total_seconds()
        for start, end in zip(starts, ends, strict=True)
        if end is not None
    ]
    if len(spans) == len(starts) and all(span >= 0 for span in spans):
        seconds = round(sum(spans))
    booked = {use.subtask for use in record.uses}
    grams: float | None = None
    printed = record.outcome == "finished" and all(start.ended == "done" for start in starts[:-1])
    if printed and all(start.grams_by_slot for start in starts):
        grams = sum(sum(start.grams_by_slot.values()) for start in starts)
    elif (
        record.uses
        and all(start.subtask_name in booked for start in starts)
        and all(use.grams is not None and use.applied for use in record.uses)
    ):
        grams = sum(use.grams or 0.0 for use in record.uses)
    return OwnFacts(
        started_at=min(start.started_at for start in starts),
        finished_at=ends[-1],
        actual_seconds=seconds,
        actual_grams=grams,
    )


async def _facts(integrations: OrderIntegrations, archive_ids: list[int]) -> list[ArchiveFacts]:
    """Each archive Bambuddy still has; one that fails to read is left out (logged), so
    the other plates still offer their copies."""
    read = await asyncio.gather(
        *(integrations.archive(a) for a in archive_ids), return_exceptions=True
    )
    facts: list[ArchiveFacts] = []
    for archive_id, answer in zip(archive_ids, read, strict=True):
        if isinstance(answer, NotLinkedError):
            return []
        if isinstance(answer, BaseException):
            logger.warning("could not read archive %s", archive_id, exc_info=answer)
        elif answer is not None:
            facts.append(answer)
    return facts


async def link_lan_archives(
    lifecycle: Lifecycle, integrations: OrderIntegrations, run_id: str
) -> dict[int, ArchiveFacts]:
    """Record which archive each LAN start of ``run_id`` became, when nobody read the
    print yet (#8281): only the print's read records it there (`print_history._run_by_file`).
    The candidates are the archives that a link by content hash gives the run's subject
    and that name no start yet, linked within `LAN_LINK_WINDOW` of the run's newest
    start that no archive names (`OrderStore.lan_candidates`, at most `LAN_CANDIDATES`),
    so an archive that never matches is not read again past that window. Each is read
    through the seam, as the attempt reads its own archives, and its file name names its
    start by the print read's own rule (`lan_files.start_by_file`): exactly one start of
    one run of the subject, never a guess. One archive per start (#8306): two candidates
    that name one start record neither (`lan_files.starts_by_files`), and a start that
    another archive became already keeps it (`OrderStore.record_lan_start`). Returns the
    facts read, by archive id, so the attempt does not read them again."""
    store = lifecycle.store
    found = await asyncio.to_thread(store.lan_candidates, run_id, LAN_CANDIDATES, LAN_LINK_WINDOW)
    if found is None or not found[1]:
        return {}
    subject, archive_ids = found
    read = {f.archive_id: f for f in await _facts(integrations, archive_ids)}
    subtasks = sorted({s for f in read.values() if (s := lan_subtask(f.filename)) is not None})
    if not subtasks:
        return read
    runs = await asyncio.to_thread(store.lan_runs, subject, subtasks)
    for link in starts_by_files({a: f.filename for a, f in read.items()}, runs):
        # False when another archive became the start already: it keeps that one.
        await asyncio.to_thread(store.record_lan_start, link)
    return read


async def mirror_archives(
    lifecycle: Lifecycle,
    integrations: OrderIntegrations,
    attempt: Fulfilment,
    read_timeout: float | None = None,
) -> tuple[Fulfilment, list[ArchiveFacts]]:
    """The attempt with its archives' facts copied (once its print settled), and those
    facts. An attempt recorded by hand naming its archive (#4251, a print attached to
    the item from its own page) copies that archive's, its print being over. Nothing is
    asked without a Bambuddy, for an attempt by hand naming none, or one whose run
    Bambuddy has linked no archive to yet. A read of Bambuddy slower than
    ``read_timeout`` raises ``TimeoutError`` before anything is written (#6167). A
    database error in the LAN link (:func:`link_lan_archives`) is logged, and the read
    goes on without it (#8306).

    A settled LAN attempt (#8282) takes each fact :func:`lan_facts` gives from ScadBuddy's
    own record, and an archive's only where that record gives none. It writes them with no
    Bambuddy and no archive too."""
    own: OwnFacts | None = None
    if attempt.print_run_id is not None and attempt.finished_at is not None:
        record = await asyncio.to_thread(lifecycle.store.lan_record, attempt.print_run_id)
        own = None if record is None else lan_facts(record)
    if not integrations.linked:
        return await _record_facts(lifecycle, attempt, own, []), []
    read: dict[int, ArchiveFacts] = {}
    if attempt.print_run_id is None:
        archive_ids, settled = attempt.archive_ids, True
    else:
        try:
            read = await asyncio.wait_for(
                link_lan_archives(lifecycle, integrations, attempt.print_run_id), read_timeout
            )
        except psycopg.Error:
            # Only the LAN link is lost; the archives already named are read as before,
            # and the next read tries the link again.
            logger.exception(
                "could not link the LAN archives of print run %s", attempt.print_run_id
            )
        archive_ids = await asyncio.to_thread(lifecycle.store.run_archives, attempt.print_run_id)
        settled = attempt.finished_at is not None
    if not archive_ids:
        return await _record_facts(lifecycle, attempt, own, []), []
    fetched = await asyncio.wait_for(
        _facts(integrations, [a for a in archive_ids if a not in read]), read_timeout
    )
    by_id = {**read, **{f.archive_id: f for f in fetched}}
    facts = [by_id[a] for a in archive_ids if a in by_id]
    if not settled or not facts:
        return await _record_facts(lifecycle, attempt, own, []), facts
    return await _record_facts(lifecycle, attempt, own, facts), facts


async def _record_facts(
    lifecycle: Lifecycle, attempt: Fulfilment, own: OwnFacts | None, facts: list[ArchiveFacts]
) -> Fulfilment:
    """The attempt with ``facts`` and ``own`` written (`Lifecycle.record_archive_facts`):
    each of ``own``'s facts, else the archives'. With no archive read (no Bambuddy, none
    linked yet, or a read that failed), the attempt keeps its archives, and its seconds
    and grams where ``own`` gives none. Nothing is written with neither.

    Nothing is written, and the order is not locked, when the attempt was copied once
    and already holds these archives, seconds and grams (#8330). A later copy keeps the
    times, so a repeated view only reads."""
    if own is None and not facts:
        return attempt
    mine = own or OwnFacts(None, None, None, None)
    if facts:
        started = [f.started_at for f in facts if f.started_at is not None]
        completed = [f.completed_at for f in facts if f.completed_at is not None]
        theirs = OwnFacts(
            started_at=min(started) if started else None,
            finished_at=max(completed) if completed else None,
            actual_seconds=_sum([f.actual_seconds for f in facts]),
            actual_grams=_sum([f.grams for f in facts]),
        )
        archive_ids = [f.archive_id for f in facts]
    else:
        # The times are written on the first copy only, and one not given stays.
        theirs = OwnFacts(None, None, attempt.actual_seconds, attempt.actual_grams)
        archive_ids = attempt.archive_ids
    seconds = theirs.actual_seconds if mine.actual_seconds is None else mine.actual_seconds
    grams = theirs.actual_grams if mine.actual_grams is None else mine.actual_grams
    if (
        list(archive_ids) == attempt.archive_ids
        and (seconds, grams) == (attempt.actual_seconds, attempt.actual_grams)
        and await asyncio.to_thread(lifecycle.store.archive_copied, attempt.id)
    ):
        return attempt
    return await asyncio.to_thread(
        lifecycle.record_archive_facts,
        attempt,
        archive_ids,
        started_at=mine.started_at or theirs.started_at,
        finished_at=mine.finished_at or theirs.finished_at,
        actual_seconds=seconds,
        actual_grams=grams,
    )


async def mirror_run(
    lifecycle: Lifecycle, integrations: OrderIntegrations, run_id: str, starter: MediaStarter
) -> None:
    """:func:`mirror_archives` for the attempt a run started, if any, then the start of
    its media import, with retries (spec 2026-11-05 §4.1), also when the archive read
    failed; logged, never raised: the attempt's archives are read again when they are
    shown, and by each step of the import."""
    try:
        attempt = await asyncio.to_thread(lifecycle.store.print_attempt, run_id)
    except Exception:
        logger.warning("could not find the attempt of print run %s", run_id, exc_info=True)
        return
    if attempt is None:
        return
    try:
        await mirror_archives(lifecycle, integrations, attempt)
    except Exception:
        logger.warning("could not copy an order item's print archives", exc_info=True)
    # Each step reads the archives again, so a failed read here still starts the import.
    await start_media(starter, attempt.id, retries=True)


def _wanted(facts: ArchiveFacts) -> list[tuple[ArchiveCopyMedia, str | None]]:
    """What the archive offers: its latest ``finish_`` photo (the last by name, as the
    names carry a timestamp; Bambuddy's list order is not trusted, spec §4.2) and its
    timelapse, when it has one."""
    wanted: list[tuple[ArchiveCopyMedia, str | None]] = []
    if facts.finish_photos:
        wanted.append(("finished_photo", sorted(facts.finish_photos)[-1]))
    if facts.timelapse:
        wanted.append(("timelapse", None))
    return wanted


async def _skipped(
    lifecycle: Lifecycle, attempt: Fulfilment, limit: int
) -> set[tuple[int, ArchiveCopyMedia]]:
    """Each ``(archive_id, medium)`` the import does not download: the attempt or its
    item has a copy (spec §4.2), or the import refused it at this limit (#7281 review)."""
    present = await asyncio.to_thread(
        lifecycle.store.archive_media_present, attempt.id, attempt.order_item_id
    )
    refused = await asyncio.to_thread(lifecycle.store.media_refused, attempt.id, limit)
    return present | refused


def _not_offered(
    facts: list[ArchiveFacts], skipped: set[tuple[int, ArchiveCopyMedia]], now: datetime
) -> set[tuple[int, ArchiveCopyMedia]]:
    """Each ``(archive_id, medium)`` that :func:`_missing` no longer counts, though the
    attempt has no copy of it: an archive that completed ``FRESH`` ago or more does not
    offer it (#7453). The sweep leaves these out."""
    absent: set[tuple[int, ArchiveCopyMedia]] = set()
    for archive in facts:
        if archive.completed_at is None or now - archive.completed_at < FRESH:
            continue
        offered = {what for what, _ in _wanted(archive)}
        for what in ROLES:
            if what not in offered and (archive.archive_id, what) not in skipped:
                absent.add((archive.archive_id, what))
    return absent


def _missing(
    facts: list[ArchiveFacts], skipped: set[tuple[int, ArchiveCopyMedia]], now: datetime
) -> bool:
    """Whether a later step can still import something: no archive was read, or an
    archive lacks a medium that it offers or, while it is fresh (``FRESH``), may offer
    soon. A medium in ``skipped`` (copied, or refused as too large) is not missing."""
    if not facts:
        return True
    for archive in facts:
        fresh = archive.completed_at is None or now - archive.completed_at < FRESH
        offered = {what for what, _ in _wanted(archive)}
        for what in ROLES:
            if (archive.archive_id, what) not in skipped and (what in offered or fresh):
                return True
    return False


async def mirror_media(
    lifecycle: Lifecycle,
    integrations: OrderIntegrations,
    blobs: BlobStore,
    claims: ClaimStore,
    attempt: Fulfilment,
    facts: list[ArchiveFacts],
    *,
    limit: int,
) -> list[Attachment]:
    """For each archive in ``facts``, the media the attempt is still missing, imported
    (spec 2026-11-05 §4.2). A medium the attempt, or its item, already has a copy of is
    skipped with no download, and so is one refused before at this limit or a lower
    one; a refusal as too large is recorded (#7281 review). Each medium is its own try:
    one that fails (too large, Bambuddy down) is logged and does not stop the others.
    Each copy is written by the attempt's creator (Ruling 12, spec §4.5)."""
    item = await asyncio.to_thread(lifecycle.store.item, attempt.order_item_id)
    if item is None:
        return []
    skipped = await _skipped(lifecycle, attempt, limit)
    imported: list[Attachment] = []
    for archive in facts:
        for what, name in _wanted(archive):
            if (archive.archive_id, what) in skipped:
                continue
            try:
                imported.append(
                    await copy_in(
                        store=lifecycle.store,
                        lifecycle=lifecycle,
                        integrations=integrations,
                        blobs=blobs,
                        claims=claims,
                        order_id=item.order_id,
                        item_id=item.id,
                        fulfilment_id=attempt.id,
                        archive_id=archive.archive_id,
                        what=what,
                        name=name,
                        by=attempt.created_by,
                        limit=limit,
                    )
                )
            except ApiError as error:
                if error.status != status.HTTP_413_CONTENT_TOO_LARGE:
                    logger.warning(
                        "could not import the %s of archive %s: %s",
                        what,
                        archive.archive_id,
                        error.detail,
                    )
                    continue
                logger.warning(
                    "the %s of archive %s is too large to import (%s; the limit is %s bytes)",
                    what,
                    archive.archive_id,
                    error.detail,
                    limit,
                )
                await asyncio.to_thread(
                    lifecycle.store.record_media_refusal,
                    attempt.id,
                    archive.archive_id,
                    what,
                    limit,
                    datetime.now(UTC),
                )
            except Exception:
                logger.warning(
                    "could not import the %s of archive %s",
                    what,
                    archive.archive_id,
                    exc_info=True,
                )
    return imported


async def mirror_attempt(
    lifecycle: Lifecycle,
    integrations: OrderIntegrations,
    blobs: BlobStore,
    claims: ClaimStore,
    fulfilment_id: str,
    *,
    limit: int,
) -> AttemptMediaStep:
    """One step of the attempt's ``AttemptMedia`` workflow: :func:`mirror_archives`, then
    :func:`mirror_media`, then ``media_checked_at`` stamped (spec 2026-11-05 §4.6).
    Logged, never raised. ``missing`` is true while no archive could be read yet, or an
    archive lacks its finish photo or its timelapse and either offers it or completed
    less than ``FRESH`` ago (or has no completion time yet), so the workflow steps again.
    A medium refused as too large at this limit is not missing (#7281 review). The media
    an archive past ``FRESH`` does not offer are recorded for the sweep (#7453).
    ``missing`` is false without a Bambuddy or once the attempt is gone. A failure, a
    failed database read included, answers ``missing``; a failed stamp is logged and
    keeps the answer."""
    if not integrations.linked:
        return AttemptMediaStep(missing=False)
    try:
        attempt = await asyncio.to_thread(lifecycle.store.fulfilment, fulfilment_id)
        if attempt is None:
            return AttemptMediaStep(missing=False)
        attempt, facts = await mirror_archives(lifecycle, integrations, attempt)
        await mirror_media(lifecycle, integrations, blobs, claims, attempt, facts, limit=limit)
        skipped = await _skipped(lifecycle, attempt, limit)
        now = datetime.now(UTC)
        missing = _missing(facts, skipped, now)
        await asyncio.to_thread(
            lifecycle.store.record_media_not_offered,
            attempt.id,
            _not_offered(facts, skipped, now),
            now,
        )
    except Exception:
        # The attempt's earlier "not offered" rows stay on purpose: the sweep keeps
        # skipping those media until a step succeeds, and a step a trigger starts
        # still asks the archives for the media.
        logger.warning("could not import the media of attempt %s", fulfilment_id, exc_info=True)
        missing = True
    try:
        await asyncio.to_thread(
            lifecycle.store.stamp_media_checked, fulfilment_id, datetime.now(UTC)
        )
    except Exception:
        logger.warning(
            "could not stamp the media check of attempt %s", fulfilment_id, exc_info=True
        )
    return AttemptMediaStep(missing=missing)


async def sweep_media(
    lifecycle: Lifecycle, integrations: OrderIntegrations, starter: MediaStarter, *, limit: int
) -> int:
    """The housekeeping sweep ``housekeeping_import_attempt_media`` (spec 2026-11-05 §4.6):
    one step of ``AttemptMedia`` for each attempt of the last `SWEEP_DAYS` that still
    lacks a medium and was not read for media in the last `SWEEP_CHECK_EVERY`. It catches
    what the triggers missed (Temporal down, an outcome not heard, a timelapse Bambuddy
    got later). A medium the import refused as too large at ``limit`` (the upload limit)
    or a higher one is not missing (#7281 review), nor is one that the last step found an
    archive past ``FRESH`` not offering (#7453). At most `SWEEP_BATCH` attempts, least
    lately read first (#7453). It asks Bambuddy nothing itself, and nothing without a
    Bambuddy. A failed start is logged, and the next attempt is started. The number of
    starts asked for."""
    if not integrations.linked:
        return 0
    now = datetime.now(UTC)
    attempts = await asyncio.to_thread(
        lifecycle.store.attempts_missing_media,
        now - SWEEP_DAYS,
        now - SWEEP_CHECK_EVERY,
        limit,
        batch=SWEEP_BATCH,
    )
    for fulfilment_id in attempts:
        await start_media(starter, fulfilment_id, retries=False)
    return len(attempts)


def attempt_of(store: OrderStore, order_id: str, item_id: str, fulfilment_id: str) -> Fulfilment:
    """The attempt, refused with a 404 unless it is the item's and the item the order's."""
    item = store.item(item_id)
    attempt = store.fulfilment(fulfilment_id)
    if item is None or item.order_id != order_id or attempt is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no attempt {fulfilment_id!r} of that item")
    if attempt.order_item_id != item.id:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no attempt {fulfilment_id!r} of that item")
    return attempt


async def attempt_archives(
    lifecycle: Lifecycle,
    integrations: OrderIntegrations,
    order_id: str,
    item_id: str,
    fulfilment_id: str,
    starter: MediaStarter,
    *,
    limit: int,
) -> AttemptArchives:
    """The attempt's archives, their facts copied. When an archive offers a medium the
    attempt does not have yet, and the import did not refuse it at ``limit`` (the upload
    limit) or a lower one, one step of its media import is started; the read answers at
    once and never waits on a download (spec 2026-11-05 §4.1)."""
    attempt = await asyncio.to_thread(attempt_of, lifecycle.store, order_id, item_id, fulfilment_id)
    attempt, facts = await mirror_archives(lifecycle, integrations, attempt)
    attached: dict[tuple[int, ArchiveCopyMedia], str] = {}
    refused: set[tuple[int, ArchiveCopyMedia]] = set()
    if facts:
        attached = await asyncio.to_thread(
            lifecycle.store.archive_media_attachments, attempt.id, item_id
        )
        refused = await asyncio.to_thread(lifecycle.store.media_refused, attempt.id, limit)
        skipped = set(attached) | refused
        if any((f.archive_id, what) not in skipped for f in facts for what, _ in _wanted(f)):
            await start_media(starter, attempt.id, retries=False)
    return AttemptArchives(
        linked=integrations.linked,
        attempt=attempt,
        archives=[
            ArchiveOffer(
                **f.model_dump(),
                finished_attachment_id=attached.get((f.archive_id, "finished_photo")),
                timelapse_attachment_id=attached.get((f.archive_id, "timelapse")),
                finished_too_large=(f.archive_id, "finished_photo") in refused,
                timelapse_too_large=(f.archive_id, "timelapse") in refused,
            )
            for f in facts
        ],
    )


async def copy_check(
    store: OrderStore,
    integrations: OrderIntegrations,
    order_id: str,
    item_id: str,
    fulfilment_id: str,
    archive_id: int,
) -> None:
    """What the copy's check refuses from the rows alone: a Bambuddy not linked, an
    attempt not the item's, an archive not one of the attempt's run's."""
    if not integrations.linked:
        raise not_linked()
    attempt = await asyncio.to_thread(attempt_of, store, order_id, item_id, fulfilment_id)
    linked = (
        attempt.archive_ids
        if attempt.print_run_id is None
        else await asyncio.to_thread(store.run_archives, attempt.print_run_id)
    )
    if archive_id not in linked:
        raise _refused(
            "archive_not_attempts",
            f"archive {archive_id} is not one of attempt {attempt.attempt}'s prints",
        )


async def copy_in(
    *,
    store: OrderStore,
    lifecycle: Lifecycle,
    integrations: OrderIntegrations,
    blobs: BlobStore,
    claims: ClaimStore,
    order_id: str,
    item_id: str,
    fulfilment_id: str,
    archive_id: int,
    what: ArchiveCopyMedia,
    name: str | None,
    by: Actor,
    limit: int,
) -> Attachment:
    """The archive's ``finish_`` photo (``name``, else its latest) or its timelapse as
    an attachment of the attempt (listed under its item), with the role ``finished`` or
    ``timelapse``, written by ``by``. The row names the archive and the medium, and its
    audit line says ``source: archive`` (spec 2026-11-05 §4.2, §4.5). The same bytes
    copied again under the same role are the attachment already there. A copy of the
    same archive file that another copy recorded first is that copy's row (§4.6)."""
    await copy_check(store, integrations, order_id, item_id, fulfilment_id, archive_id)
    attempt = await asyncio.to_thread(attempt_of, store, order_id, item_id, fulfilment_id)
    try:
        facts = await integrations.archive(archive_id)
    except NotLinkedError:
        raise not_linked() from None
    if facts is None:
        raise _refused("archive_gone", f"Bambuddy no longer has archive {archive_id}")
    if what == "finished_photo":
        if not facts.finish_photos:
            raise _refused("no_finish_photo", f"archive {archive_id} has no finish photo")
        name = name or facts.finish_photos[-1]
        if name not in facts.finish_photos:
            raise _refused("no_finish_photo", f"archive {archive_id} has no photo {name!r}")
    elif not facts.timelapse:
        raise _refused(
            "no_timelapse",
            f"archive {archive_id} has no timelapse in Bambuddy",
        )
    claims.root.mkdir(parents=True, exist_ok=True)
    staging = claims.root / f".copy-{uuid.uuid4().hex}"
    try:
        copied = await integrations.download_archive_media(
            archive_id,
            what,
            name if what == "finished_photo" else facts.timelapse_name,
            staging,
            limit,
        )
    except MediaTooLargeError as error:
        raise ApiError(status.HTTP_413_CONTENT_TOO_LARGE, str(error)) from None
    role = ROLES[what]
    held = await asyncio.to_thread(claims.hold_file, staging, copied.sha256)
    try:
        # The attempt's files, and the item's from before copies went onto the attempt.
        existing = [
            *await asyncio.to_thread(store.subject_attachments, fulfilment_subject(fulfilment_id)),
            *await asyncio.to_thread(store.subject_attachments, item_subject(item_id)),
        ]
        same = next((a for a in existing if a.sha256 == copied.sha256 and a.role == role), None)
        if same is not None:
            return same
        with claims.open(copied.sha256) as handle:
            head = handle.read(attachments.SNIFF_BYTES)
        kind, media_type = attachments.sniff(head, copied.filename)
        try:
            key = await attachments.keep(blobs, claims, copied.sha256, copied.sha256)
        except LookupError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "the copied file was swept before it was kept; copy it again",
            ) from None
        filename = attachments.clean_filename(copied.filename)
        try:
            return await asyncio.to_thread(
                lambda: lifecycle.add_attachment(
                    order_id,
                    item_id,
                    by,
                    attachment_id=str(uuid.uuid4()),
                    kind=kind,
                    role=role,
                    filename=filename,
                    media_type=media_type,
                    size_bytes=copied.size,
                    sha256=copied.sha256,
                    blob_key=key,
                    caption=f"Attempt {attempt.attempt}, Bambuddy archive {archive_id}",
                    fulfilment_id=attempt.id,
                    archive_id=archive_id,
                    archive_media=what,
                    source="archive",
                )
            )
        except ArchiveCopyExistsError:
            # The race is lost: the add wrote no row, hold or audit line.
            rows = await asyncio.to_thread(
                store.subject_attachments, fulfilment_subject(fulfilment_id)
            )
            return next(
                a
                for a in rows
                if (a.archive_id, a.archive_media, a.filename) == (archive_id, what, filename)
            )
    finally:
        await asyncio.to_thread(claims.release, held)
