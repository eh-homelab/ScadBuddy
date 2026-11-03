"""An attempt's Bambuddy archives (#4253, spec 2026-10-18 §8.2).

A print run queues one item per plate, and each queue item's print is an archive, which
``print_links`` names once Bambuddy reports it (``print_sends`` by ``run_id``, then the
link by queue item: ``OrderStore.run_archives``). Once the attempt's print settled, the
attempt copies what the archives say: their ids, the printer's start and end, the seconds
and grams summed (:func:`mirror_archives`). That runs when the print's outcome is heard
(``printing.py`` ``listen_for_prints``) and again whenever the attempt's archives are read
(``GET …/fulfilments/{f}/archives``), so a link Bambuddy made late is still copied.

An archive's ``finish_`` photo and its timelapse are offered as one-click copies into the
attempt's attachments (§6.2, #4251): the bytes are pulled once through the seam into the claim
store, kept as ScadBuddy's own blob and recorded as the attachment, with the role
``finished`` or ``timelapse`` (:func:`copy_in`). Nothing goes the other way.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.core.problems import ApiError
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.orders import attachments
from scadbuddy.orders.integrations import (
    ArchiveFacts,
    ArchiveMedia,
    MediaTooLargeError,
    NotLinkedError,
    OrderIntegrations,
    not_linked,
)
from scadbuddy.orders.lifecycle import Lifecycle
from scadbuddy.orders.models import Actor, Attachment, AttachmentRole, Fulfilment
from scadbuddy.orders.problems import PROBLEMS
from scadbuddy.orders.store import OrderStore, fulfilment_subject, item_subject
from scadbuddy.store import BlobStore

logger = logging.getLogger(__name__)

ROLES: dict[ArchiveMedia, AttachmentRole] = {"finished_photo": "finished", "timelapse": "timelapse"}


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
    store: OrderStore, integrations: OrderIntegrations, attempt: Fulfilment
) -> tuple[Fulfilment, list[ArchiveFacts]]:
    """The attempt with its archives' facts copied (once its print settled), and those
    facts. An attempt recorded by hand naming its archive (#4251, a print attached to
    the item from its own page) copies that archive's, its print being over. Nothing is
    asked without a Bambuddy, for an attempt by hand naming none, or one whose run
    Bambuddy has linked no archive to yet."""
    if not integrations.linked:
        return attempt, []
    if attempt.print_run_id is None:
        archive_ids, settled = attempt.archive_ids, True
    else:
        archive_ids = await asyncio.to_thread(store.run_archives, attempt.print_run_id)
        settled = attempt.finished_at is not None
    if not archive_ids:
        return attempt, []
    facts = await _facts(integrations, archive_ids)
    if not settled or not facts:
        return attempt, facts
    started = [f.started_at for f in facts if f.started_at is not None]
    completed = [f.completed_at for f in facts if f.completed_at is not None]
    written = await asyncio.to_thread(
        store.set_archive_facts,
        attempt.id,
        [f.archive_id for f in facts],
        started_at=min(started) if started else None,
        finished_at=max(completed) if completed else None,
        actual_seconds=_sum([f.actual_seconds for f in facts]),
        actual_grams=_sum([f.grams for f in facts]),
    )
    return written or attempt, facts


async def mirror_run(store: OrderStore, integrations: OrderIntegrations, run_id: str) -> None:
    """:func:`mirror_archives` for the attempt a run started, if any; logged, never
    raised: the attempt's archives are read again when they are shown."""
    try:
        attempt = await asyncio.to_thread(store.print_attempt, run_id)
        if attempt is not None:
            await mirror_archives(store, integrations, attempt)
    except Exception:
        logger.warning("could not copy an order item's print archives", exc_info=True)


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
    store: OrderStore,
    integrations: OrderIntegrations,
    order_id: str,
    item_id: str,
    fulfilment_id: str,
) -> AttemptArchives:
    attempt = await asyncio.to_thread(attempt_of, store, order_id, item_id, fulfilment_id)
    attempt, facts = await mirror_archives(store, integrations, attempt)
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
    what: ArchiveMedia,
    name: str | None,
    by: Actor,
    limit: int,
) -> Attachment:
    """The archive's ``finish_`` photo (``name``, else its latest) or its timelapse as
    an attachment of the item, with the role ``finished`` or ``timelapse``. The same
    bytes copied again under the same role are the attachment already there."""
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
            archive_id, what, name if what == "finished_photo" else None, staging, limit
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
        return await asyncio.to_thread(
            lambda: lifecycle.add_attachment(
                order_id,
                item_id,
                by,
                attachment_id=str(uuid.uuid4()),
                kind=kind,
                role=role,
                filename=attachments.clean_filename(copied.filename),
                media_type=media_type,
                size_bytes=copied.size,
                sha256=copied.sha256,
                blob_key=key,
                caption=f"Attempt {attempt.attempt}, Bambuddy archive {archive_id}",
                fulfilment_id=attempt.id,
            )
        )
    finally:
        await asyncio.to_thread(claims.release, held)
