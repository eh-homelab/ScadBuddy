"""An attempt's Bambuddy archives (#4253, spec 2026-10-18 §8.2): what they say copied onto
the attempt once its print settled, and their finish photo and timelapse copied into the
item's attachments."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scadbuddy.core.events import PrintOutcome
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.orders.archives import attempt_archives, copy_in, mirror_archives, mirror_run
from scadbuddy.orders.integrations import (
    NOT_LINKED,
    ArchiveFacts,
    ArchiveMedia,
    CopiedMedia,
    MediaTooLargeError,
    SpoolGroup,
)
from scadbuddy.orders.models import Fulfilment, Order, OrderItem
from scadbuddy.orders.printing import listen_for_prints
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.content import ContentStore
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop
from tests.orders.test_printing import SUBJECT, run_row, settle, started
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 2048
MP4 = b"\0\0\0\x18ftypisom" + b"v" * 4096


class Archives:
    """The seam with a Bambuddy holding ``facts``; its media is ``JPEG`` and ``MP4``."""

    linked = True

    def __init__(self, *facts: ArchiveFacts) -> None:
        self.facts = {f.archive_id: f for f in facts}
        self.downloads: list[tuple[int, str, str | None]] = []

    async def spool_groups(self) -> list[SpoolGroup]:
        raise AssertionError("not asked")

    async def ensure_project(self, name: str, parent_id: int | None) -> int:
        raise AssertionError("not asked")

    async def archive(self, archive_id: int) -> ArchiveFacts | None:
        return self.facts.get(archive_id)

    async def archive_cost(self, archive_id: int) -> float | None:
        raise AssertionError("not asked")

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> CopiedMedia:
        self.downloads.append((archive_id, what, name))
        data = JPEG if what == "finished_photo" else MP4
        if len(data) > limit:
            raise MediaTooLargeError("too large")
        target.write_bytes(data)
        filename = name or f"timelapse-{archive_id}.mp4"
        return CopiedMedia(
            filename=filename, sha256=hashlib.sha256(data).hexdigest(), size=len(data)
        )


def facts(archive_id: int, **fields: object) -> ArchiveFacts:
    return ArchiveFacts.model_validate(
        {
            "archive_id": archive_id,
            "status": "completed",
            "started_at": datetime(2026, 11, 1, 10, tzinfo=UTC),
            "completed_at": datetime(2026, 11, 1, 12, tzinfo=UTC),
            "actual_seconds": 7200,
            "grams": 12.5,
            "finish_photos": ["finish_1_aa.jpg", "finish_2_bb.jpg"],
            "timelapse": True,
            **fields,
        }
    )


def link(pool: PgPool, run_id: str, queue_item_id: int, archive_id: int) -> None:
    """The run's queue item, and the archive Bambuddy reported for it."""
    subject = f"output:{SUBJECT}"
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_sends (queue_item_id, subject, run_id) VALUES (%s, %s, %s)",
            (queue_item_id, subject, run_id),
        )
        conn.execute(
            "INSERT INTO print_links (subject, archive_id, matched_by, queue_item_id)"
            " VALUES (%s, %s, 'queue_item', %s)",
            (subject, archive_id, queue_item_id),
        )


@pytest.fixture(name="shop")
def _shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


@pytest.fixture
async def blobs(tmp_path: Path, pool: PgPool) -> AsyncIterator[CachedBlobStore]:
    content: ContentStore = local_content(tmp_path / "remote", pool)
    try:
        yield CachedBlobStore(
            LocalBlobStore(tmp_path / "blobs"), content, max_bytes=1 << 30, min_age=0
        )
    finally:
        await content.aclose()


def printed(shop: Shop, pool: PgPool) -> tuple[Order, OrderItem, Fulfilment, str]:
    """An attempt whose two-plate print settled, its archives 81 and 82 linked."""
    order, item = shop.accepted()
    run_id = run_row(pool)
    started(shop, order, item, run_id)
    link(pool, run_id, 51, 81)
    link(pool, run_id, 52, 82)
    settle(pool, run_id, "finished")
    attempt = shop.lifecycle.follow_print(run_id)
    assert attempt is not None and attempt.finished_at is not None
    return order, item, attempt, run_id


async def test_a_settled_attempt_copies_what_its_archives_say(shop: Shop, pool: PgPool) -> None:
    order, item, attempt, _ = printed(shop, pool)
    seam = Archives(
        facts(81),
        facts(
            82,
            started_at=datetime(2026, 11, 1, 9, tzinfo=UTC),
            completed_at=datetime(2026, 11, 1, 13, tzinfo=UTC),
            actual_seconds=600,
            grams=2.5,
        ),
    )
    read = await attempt_archives(shop.rows, seam, order.id, item.id, attempt.id)
    assert read.linked and [a.archive_id for a in read.archives] == [81, 82]
    kept = shop.rows.fulfilments(item.id)[0]
    assert kept.archive_ids == [81, 82]
    assert (kept.actual_seconds, kept.actual_grams) == (7800, 15.0)
    assert kept.started_at == datetime(2026, 11, 1, 9, tzinfo=UTC)
    assert kept.finished_at == datetime(2026, 11, 1, 13, tzinfo=UTC)
    # Its outcome is the print path's, never the archive's.
    assert kept.outcome == "succeeded"
    # A time a person corrects since stands when the archives are read again.
    corrected = datetime(2026, 11, 1, 14, tzinfo=UTC)
    with shop.rows.transaction() as conn:
        shop.rows.update_fulfilment(conn, attempt.id, finished_at=corrected)
    await attempt_archives(shop.rows, seam, order.id, item.id, attempt.id)
    assert shop.rows.fulfilments(item.id)[0].finished_at == corrected


async def test_an_open_attempt_offers_its_archives_and_copies_nothing(
    shop: Shop, pool: PgPool
) -> None:
    order, item = shop.accepted()
    run_id = run_row(pool)
    attempt = started(shop, order, item, run_id)
    link(pool, run_id, 51, 81)
    after, offered = await mirror_archives(shop.rows, Archives(facts(81)), attempt)
    assert [f.archive_id for f in offered] == [81]
    assert after.archive_ids == [] and after.actual_seconds is None


async def test_without_bambuddy_nothing_is_asked(shop: Shop, pool: PgPool) -> None:
    order, item, attempt, _ = printed(shop, pool)
    read = await attempt_archives(shop.rows, NOT_LINKED, order.id, item.id, attempt.id)
    assert (read.linked, read.archives) == (False, [])


async def test_the_finish_photo_and_timelapse_copy_into_the_items_files(
    shop: Shop, pool: PgPool, tmp_path: Path, blobs: CachedBlobStore
) -> None:
    order, item, attempt, _ = printed(shop, pool)
    seam = Archives(facts(81), facts(82))
    claims = ClaimStore(tmp_path / "claims")

    async def copy(what: ArchiveMedia, archive_id: int = 81, name: str | None = None) -> object:
        return await copy_in(
            store=shop.rows,
            lifecycle=shop.lifecycle,
            integrations=seam,
            blobs=blobs,
            claims=claims,
            order_id=order.id,
            item_id=item.id,
            fulfilment_id=attempt.id,
            archive_id=archive_id,
            what=what,
            name=name,
            by=BY,
            limit=1 << 20,
        )

    photo = await copy("finished_photo")
    assert seam.downloads == [(81, "finished_photo", "finish_2_bb.jpg")]
    video = await copy("timelapse")
    files = shop.rows.attachments(order.id)
    assert [(a.role, a.kind, a.item_id) for a in files] == [
        ("finished", "image", item.id),
        ("timelapse", "video", item.id),
    ]
    assert files[0].caption == "Attempt 1, Bambuddy archive 81"
    assert files[0].filename == "finish_2_bb.jpg"
    # The same bytes again are the file already there.
    assert await copy("finished_photo") == photo
    assert await copy("timelapse") == video
    assert len(shop.rows.attachments(order.id)) == 2
    # No claim is left behind.
    assert [p for p in claims.root.iterdir() if not p.name.startswith(".")] == []


async def test_a_copy_is_refused_for_what_the_archive_lacks(
    shop: Shop, pool: PgPool, tmp_path: Path, blobs: CachedBlobStore
) -> None:
    order, item, attempt, _ = printed(shop, pool)
    seam = Archives(facts(81, finish_photos=[], timelapse=False))

    async def refusal(what: ArchiveMedia, archive_id: int, limit: int = 1 << 20) -> object:
        with pytest.raises(ApiError) as refused:
            await copy_in(
                store=shop.rows,
                lifecycle=shop.lifecycle,
                integrations=seam,
                blobs=blobs,
                claims=ClaimStore(tmp_path / "claims"),
                order_id=order.id,
                item_id=item.id,
                fulfilment_id=attempt.id,
                archive_id=archive_id,
                what=what,
                name=None,
                by=BY,
                limit=limit,
            )
        return refused.value.status, (refused.value.type or "").rsplit("/", 1)[-1]

    assert await refusal("finished_photo", 81) == (409, "no_finish_photo")
    assert await refusal("timelapse", 81) == (409, "no_timelapse")
    # Not one of this attempt's prints: never read from Bambuddy.
    assert await refusal("timelapse", 99) == (409, "archive_not_attempts")
    # Gone from Bambuddy.
    assert await refusal("timelapse", 82) == (409, "archive_gone")
    seam.facts[81] = facts(81)
    assert await refusal("timelapse", 81, limit=10) == (413, "about:blank")
    assert seam.downloads == [(81, "timelapse", None)]
    assert shop.rows.attachments(order.id) == []
    # Another order's attempt is no attempt of this item.
    other = str(uuid.uuid4())
    with pytest.raises(ApiError) as missing:
        await attempt_archives(shop.rows, seam, order.id, item.id, other)
    assert missing.value.status == 404


async def test_a_heard_outcome_copies_the_attempts_archives(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted()
    run_id = run_row(pool)
    started(shop, order, item, run_id)
    link(pool, run_id, 51, 81)
    seam = Archives(facts(81))
    remove = listen_for_prints(
        shop.bus, shop.lifecycle, lambda run: mirror_run(shop.rows, seam, run)
    )
    try:
        settle(pool, run_id, "finished")
        shop.bus.publish(
            PrintOutcome(output_id=SUBJECT, slug="demo", run_id=run_id, outcome="finished")
        )
        deadline = asyncio.get_running_loop().time() + 10
        while not shop.rows.fulfilments(item.id)[0].archive_ids:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
    finally:
        await remove()
    kept = shop.rows.fulfilments(item.id)[0]
    assert (kept.outcome, kept.archive_ids, kept.actual_grams) == ("succeeded", [81], 12.5)
