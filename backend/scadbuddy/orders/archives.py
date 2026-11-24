"""An attempt's Bambuddy archives (#4253, spec 2026-10-18 §8.2).

A print run queues one item per plate, and each queue item's print is an archive, which
``print_links`` names once Bambuddy reports it (``print_sends`` by ``run_id``, then the
link by queue item: ``OrderStore.run_archives``). Once the attempt's print settled, the
attempt copies what the archives say: their ids, the printer's start and end, the seconds
and grams summed (:func:`mirror_archives`). That runs when the print's outcome is heard
(``printing.py`` ``listen_for_prints``) and again whenever the attempt's archives are read
(``GET …/fulfilments/{f}/archives``), so a link Bambuddy made late is still copied. An
attempt by hand that names its archives copies them when it is recorded or edited too
(``api/orders.py`` ``record_fulfilment``, ``update_fulfilment``, #6132).

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
from datetime import UTC, datetime, timedelta

from fastapi import status
from pydantic import BaseModel, Field

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
    OrderStore,
    fulfilment_subject,
    item_subject,
)
from scadbuddy.store import BlobStore
from scadbuddy.workflows.attempt_media import AttemptMediaStep

logger = logging.getLogger(__name__)

#: The sweep looks at the attempts made this recently (spec 2026-11-05 §4.6).
SWEEP_DAYS = timedelta(days=14)
#: The sweep starts a step for an attempt last read for media at least this long ago.
SWEEP_CHECK_EVERY = timedelta(hours=1)
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
    #: Whether the archive has a timelapse to copy (pulled off the printer first,
    #: ``POST /prints/{archive_id}/timelapse/pull``).
    timelapse: bool = False


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
    ``read_timeout`` raises ``TimeoutError`` before anything is written (#6167)."""
    if not integrations.linked:
        return attempt, []
    if attempt.print_run_id is None:
        archive_ids, settled = attempt.archive_ids, True
    else:
        archive_ids = await asyncio.to_thread(lifecycle.store.run_archives, attempt.print_run_id)
        settled = attempt.finished_at is not None
    if not archive_ids:
        return attempt, []
    facts = await asyncio.wait_for(_facts(integrations, archive_ids), read_timeout)
    if not settled or not facts:
        return attempt, facts
    started = [f.started_at for f in facts if f.started_at is not None]
    completed = [f.completed_at for f in facts if f.completed_at is not None]
    written = await asyncio.to_thread(
        lifecycle.record_archive_facts,
        attempt,
        [f.archive_id for f in facts],
        started_at=min(started) if started else None,
        finished_at=max(completed) if completed else None,
        actual_seconds=_sum([f.actual_seconds for f in facts]),
        actual_grams=_sum([f.grams for f in facts]),
    )
    return written, facts


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
    A medium refused as too large at this limit is not missing (#7281 review).
    ``missing`` is false without a Bambuddy or once the attempt is gone."""
    if not integrations.linked:
        return AttemptMediaStep(missing=False)
    attempt = await asyncio.to_thread(lifecycle.store.fulfilment, fulfilment_id)
    if attempt is None:
        return AttemptMediaStep(missing=False)
    try:
        attempt, facts = await mirror_archives(lifecycle, integrations, attempt)
        await mirror_media(lifecycle, integrations, blobs, claims, attempt, facts, limit=limit)
        skipped = await _skipped(lifecycle, attempt, limit)
    except Exception:
        logger.warning("could not import the media of attempt %s", fulfilment_id, exc_info=True)
        return AttemptMediaStep(missing=True)
    finally:
        await asyncio.to_thread(
            lifecycle.store.stamp_media_checked, fulfilment_id, datetime.now(UTC)
        )
    return AttemptMediaStep(missing=_missing(facts, skipped, datetime.now(UTC)))


async def sweep_media(
    lifecycle: Lifecycle, integrations: OrderIntegrations, starter: MediaStarter, *, limit: int
) -> int:
    """The housekeeping sweep ``housekeeping_import_attempt_media`` (spec 2026-11-05 §4.6):
    one step of ``AttemptMedia`` for each attempt of the last `SWEEP_DAYS` that still
    lacks a medium and was not read for media in the last `SWEEP_CHECK_EVERY`. It catches
    what the triggers missed (Temporal down, an outcome not heard, a timelapse Bambuddy
    got later). A medium the import refused as too large at ``limit`` (the upload limit)
    or a higher one is not missing (#7281 review). It asks Bambuddy nothing itself, and
    nothing without a Bambuddy. A failed start is logged, and the next attempt is
    started. The number of starts asked for."""
    if not integrations.linked:
        return 0
    now = datetime.now(UTC)
    attempts = await asyncio.to_thread(
        lifecycle.store.attempts_missing_media, now - SWEEP_DAYS, now - SWEEP_CHECK_EVERY, limit
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
    if facts:
        skipped = await _skipped(lifecycle, attempt, limit)
        if any((f.archive_id, what) not in skipped for f in facts for what, _ in _wanted(f)):
            await start_media(starter, attempt.id, retries=False)
    return AttemptArchives(
        linked=integrations.linked,
        attempt=attempt,
        archives=[ArchiveOffer(**f.model_dump()) for f in facts],
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
            f"archive {archive_id} has no timelapse: pull it off the printer first"
            f" (POST /prints/{archive_id}/timelapse/pull)",
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
