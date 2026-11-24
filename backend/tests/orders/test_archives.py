"""An attempt's Bambuddy archives (#4253, spec 2026-10-18 §8.2): what they say copied onto
the attempt once its print settled, and their finish photo and timelapse copied into the
item's attachments."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.api.orders import record_fulfilment, update_fulfilment
from scadbuddy.bambuddy.models import SpoolAssignment
from scadbuddy.bambuddy.print_links import PrintLinkStore
from scadbuddy.core.events import OrderItemChanged, PrintOutcome
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.orders import archives as archives_module
from scadbuddy.orders import attempt_media as attempt_media_module
from scadbuddy.orders.archives import (
    FRESH,
    attempt_archives,
    copy_check,
    copy_in,
    mirror_archives,
    mirror_attempt,
    mirror_run,
    sweep_media,
)
from scadbuddy.orders.attempt_media import start_media
from scadbuddy.orders.component import OrdersComponent
from scadbuddy.orders.integrations import (
    NOT_LINKED,
    ArchiveFacts,
    ArchiveMedia,
    CopiedMedia,
    MaintenanceDone,
    MaintenanceTask,
    MediaTooLargeError,
)
from scadbuddy.orders.lifecycle import FulfilmentInput, LifecycleError
from scadbuddy.orders.models import (
    Actor,
    ArchiveCopyMedia,
    Attachment,
    Fulfilment,
    Order,
    OrderItem,
)
from scadbuddy.orders.printing import listen_for_prints
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.content import ContentStore
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.workflows.attempt_media import AttemptMediaStep
from scadbuddy.workflows.housekeeping import ATTEMPT_MEDIA_TIMEOUT
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop
from tests.orders.test_printing import SUBJECT, run_row, settle, started
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 2048
MP4 = b"\0\0\0\x18ftypisom" + b"v" * 4096
#: The upload limit a test's import runs with: both media fit.
LIMIT = 1 << 20


class Archives:
    """The seam with a Bambuddy holding ``facts``; its media is ``JPEG`` and ``MP4``."""

    linked = True

    def __init__(self, *facts: ArchiveFacts) -> None:
        self.facts = {f.archive_id: f for f in facts}
        self.downloads: list[tuple[int, str, str | None]] = []

    async def spool_assignments(self) -> list[SpoolAssignment]:
        raise AssertionError("not asked")

    async def maintenance_tasks(self, printer_id: int) -> list[MaintenanceTask]:
        raise AssertionError("not asked")

    async def maintenance_history(
        self, item_ids: Sequence[int]
    ) -> dict[int, list[MaintenanceDone]]:
        raise AssertionError("not asked")

    async def project_exists(self, project_id: int) -> bool:
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
            "timelapse_name": f"timelapse-{archive_id}.avi",
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
    shop.events.clear()
    read = await attempt_archives(
        shop.lifecycle, seam, order.id, item.id, attempt.id, Starts(), limit=LIMIT
    )
    # #5263: written under the order's lock and told, as every item write is.
    assert [e.item_id for e in shop.events if isinstance(e, OrderItemChanged)] == [item.id]
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
    await attempt_archives(
        shop.lifecycle, seam, order.id, item.id, attempt.id, Starts(), limit=LIMIT
    )
    assert shop.rows.fulfilments(item.id)[0].finished_at == corrected


async def test_an_open_attempt_offers_its_archives_and_copies_nothing(
    shop: Shop, pool: PgPool
) -> None:
    order, item = shop.accepted()
    run_id = run_row(pool)
    attempt = started(shop, order, item, run_id)
    link(pool, run_id, 51, 81)
    after, offered = await mirror_archives(shop.lifecycle, Archives(facts(81)), attempt)
    assert [f.archive_id for f in offered] == [81]
    assert after.archive_ids == [] and after.actual_seconds is None


async def test_an_archive_an_attempt_names_is_known_to_the_media_gate(
    shop: Shop, pool: PgPool
) -> None:
    """The media proxy's gate (`PrintLinkStore.known`): an archive an attempt claims is
    ScadBuddy's to read, with no print link; one no record names is not."""
    order, item = shop.accepted()
    shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded", archive_id=81), BY
    )
    links = PrintLinkStore(pool)
    assert await links.linked(81) is None
    assert await links.known(81)
    assert not await links.known(82)


async def test_an_attempt_by_hand_copies_the_archive_it_names(shop: Shop) -> None:
    """#4251: a print attached to the item from its own page."""
    order, item = shop.accepted()
    attempt = shop.lifecycle.record_fulfilment(
        order.id,
        item.id,
        FulfilmentInput(quantity=1, outcome="succeeded", archive_id=81),
        BY,
    )
    assert attempt.archive_ids == [81] and attempt.print_run_id is None

    read = await attempt_archives(
        shop.lifecycle, Archives(facts(81)), order.id, item.id, attempt.id, Starts(), limit=LIMIT
    )

    assert [a.archive_id for a in read.archives] == [81]
    kept = shop.rows.fulfilments(item.id)[0]
    assert (kept.archive_ids, kept.actual_seconds, kept.actual_grams) == ([81], 7200, 12.5)
    assert kept.started_at == datetime(2026, 11, 1, 10, tzinfo=UTC)
    await copy_check(shop.rows, Archives(facts(81)), order.id, item.id, attempt.id, 81)
    with pytest.raises(ApiError):
        await copy_check(shop.rows, Archives(facts(82)), order.id, item.id, attempt.id, 82)


class FailingArchives(Archives):
    """A Bambuddy whose archive reads fail."""

    async def archive(self, archive_id: int) -> ArchiveFacts | None:
        raise RuntimeError("Bambuddy did not answer")


class Starts:
    """The media import's starter (`orders/attempt_media.py`): each start it was asked
    for; with ``fail``, Temporal is down."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.started: list[tuple[str, bool]] = []

    async def start(self, fulfilment_id: str, *, retries: bool) -> None:
        if self.fail:
            raise RuntimeError("Temporal is down")
        self.started.append((fulfilment_id, retries))


def orders_of(shop: Shop, integrations: Any, starts: Starts | None = None) -> OrdersComponent:
    """The routes' component: the shop's lifecycle, the seam and the starter given."""
    return cast(
        OrdersComponent,
        SimpleNamespace(
            lifecycle=shop.lifecycle,
            integrations=integrations,
            media_starter=starts or Starts(),
        ),
    )


async def test_a_hand_attempt_recorded_with_an_archive_answers_its_actuals(shop: Shop) -> None:
    """#6132: the route copies the archive at once, so job cost has the attempt's grams
    and seconds when the quote has none (a manual quote)."""
    order, item = shop.accepted()
    body = FulfilmentInput(quantity=1, outcome="succeeded", archive_id=81)

    answered = await record_fulfilment(
        order.id, item.id, body, orders_of(shop, Archives(facts(81)))
    )

    assert (answered.archive_ids, answered.actual_seconds, answered.actual_grams) == (
        [81],
        7200,
        12.5,
    )
    assert answered.started_at == datetime(2026, 11, 1, 10, tzinfo=UTC)
    kept = shop.rows.fulfilments(item.id)[0]
    assert (kept.actual_seconds, kept.actual_grams) == (7200, 12.5)

    # An edit copies the archive again; the time it sets stays.
    later = Archives(facts(81, actual_seconds=7300, grams=13.0))
    corrected = datetime(2026, 11, 1, 9, tzinfo=UTC)
    edit = FulfilmentInput(quantity=1, outcome="succeeded", started_at=corrected)
    edited = await update_fulfilment(order.id, item.id, answered.id, edit, orders_of(shop, later))
    assert (edited.actual_seconds, edited.actual_grams, edited.started_at) == (
        7300,
        13.0,
        corrected,
    )


async def test_a_hand_attempt_is_recorded_when_its_archive_cannot_be_read(shop: Shop) -> None:
    """#6132: the copy is best effort. A failed read, or a seam that raises, never fails
    the write; the attempt is answered without actuals."""
    order, item = shop.accepted()
    body = FulfilmentInput(quantity=1, outcome="succeeded", archive_id=81)

    answered = await record_fulfilment(order.id, item.id, body, orders_of(shop, FailingArchives()))

    assert (answered.archive_ids, answered.actual_seconds, answered.actual_grams) == (
        [81],
        None,
        None,
    )

    class Broken:
        @property
        def linked(self) -> bool:
            raise RuntimeError("no seam")

    other = FulfilmentInput(quantity=1, outcome="failed", archive_id=82)
    second = await record_fulfilment(order.id, item.id, other, orders_of(shop, Broken()))
    assert (second.archive_ids, second.actual_grams) == ([82], None)
    assert len(shop.rows.fulfilments(item.id)) == 2


async def test_an_edit_that_makes_the_first_copy_takes_the_archives_times(shop: Shop) -> None:
    """#6145: the record's read failed, so the edit's copy is the first. The first copy
    writes the archive's times over the times the same edit set; a later edit's stay."""
    order, item = shop.accepted()
    body = FulfilmentInput(quantity=1, outcome="succeeded", archive_id=81)
    answered = await record_fulfilment(order.id, item.id, body, orders_of(shop, FailingArchives()))
    assert answered.actual_seconds is None

    corrected = datetime(2026, 11, 1, 9, tzinfo=UTC)
    ended = datetime(2026, 11, 1, 14, tzinfo=UTC)
    edit = FulfilmentInput(quantity=1, outcome="succeeded", started_at=corrected, finished_at=ended)
    seam = orders_of(shop, Archives(facts(81)))
    edited = await update_fulfilment(order.id, item.id, answered.id, edit, seam)

    assert (edited.actual_seconds, edited.actual_grams) == (7200, 12.5)
    assert edited.started_at == datetime(2026, 11, 1, 10, tzinfo=UTC)
    assert edited.finished_at == datetime(2026, 11, 1, 12, tzinfo=UTC)

    again = await update_fulfilment(order.id, item.id, answered.id, edit, seam)
    assert (again.started_at, again.finished_at) == (corrected, ended)


class SlowArchives(Archives):
    """A Bambuddy whose archive reads never answer in time."""

    async def archive(self, archive_id: int) -> ArchiveFacts | None:
        await asyncio.sleep(5)
        return await super().archive(archive_id)


async def test_a_hand_attempt_is_answered_when_its_archive_read_is_slow(
    shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6145: the write waits for the archives' read only so long; past it, the attempt
    is recorded and answered with no actuals."""
    monkeypatch.setattr("scadbuddy.api.orders.ARCHIVE_READ_TIMEOUT", 0.05)
    order, item = shop.accepted()
    body = FulfilmentInput(quantity=1, outcome="succeeded", archive_id=81)

    answered = await asyncio.wait_for(
        record_fulfilment(order.id, item.id, body, orders_of(shop, SlowArchives(facts(81)))),
        2,
    )

    assert (answered.archive_ids, answered.actual_seconds, answered.actual_grams) == (
        [81],
        None,
        None,
    )
    kept = shop.rows.fulfilments(item.id)[0]
    assert (kept.id, kept.actual_seconds) == (answered.id, None)


async def test_a_slow_copy_after_a_quick_read_answers_what_it_stored(
    shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6167: only the archives' read is timed. A write that is still going when the
    time runs out finishes, and the answer is the attempt it stored."""
    monkeypatch.setattr("scadbuddy.api.orders.ARCHIVE_READ_TIMEOUT", 0.05)
    record = shop.lifecycle.record_archive_facts

    def slow_record(*args: Any, **kwargs: Any) -> Fulfilment:
        time.sleep(0.2)
        return record(*args, **kwargs)

    monkeypatch.setattr(shop.lifecycle, "record_archive_facts", slow_record)
    order, item = shop.accepted()
    body = FulfilmentInput(quantity=1, outcome="succeeded", archive_id=81)

    answered = await record_fulfilment(
        order.id, item.id, body, orders_of(shop, Archives(facts(81)))
    )

    assert (answered.actual_seconds, answered.actual_grams) == (7200, 12.5)
    kept = shop.rows.fulfilments(item.id)[0]
    assert (kept.actual_seconds, kept.actual_grams) == (7200, 12.5)


async def test_a_hand_attempts_archive_without_facts_keeps_corrected_times(shop: Shop) -> None:
    """#5273: an archive with no time or grams leaves no sign it was copied, which once
    made every read copy the times again over one a person corrected."""
    order, item = shop.accepted()
    attempt = shop.lifecycle.record_fulfilment(
        order.id,
        item.id,
        FulfilmentInput(quantity=1, outcome="succeeded", archive_id=81),
        BY,
    )
    seam = Archives(facts(81, actual_seconds=None, grams=None))
    await attempt_archives(
        shop.lifecycle, seam, order.id, item.id, attempt.id, Starts(), limit=LIMIT
    )
    kept = shop.rows.fulfilments(item.id)[0]
    assert kept.finished_at == datetime(2026, 11, 1, 12, tzinfo=UTC)
    corrected = datetime(2026, 11, 1, 14, tzinfo=UTC)
    with shop.rows.transaction() as conn:
        shop.rows.update_fulfilment(conn, attempt.id, finished_at=corrected)
    await attempt_archives(
        shop.lifecycle, seam, order.id, item.id, attempt.id, Starts(), limit=LIMIT
    )
    assert shop.rows.fulfilments(item.id)[0].finished_at == corrected


async def test_an_archive_named_by_hand_is_not_a_later_runs(shop: Shop, pool: PgPool) -> None:
    """#5273: an archive named by hand before Bambuddy linked it to an order's run stays
    the hand attempt's only: the run's attempt neither copies it nor offers its media."""
    order, item = shop.accepted()
    hand = shop.lifecycle.record_fulfilment(
        order.id,
        item.id,
        FulfilmentInput(quantity=1, outcome="succeeded", archive_id=81),
        BY,
    )
    other, other_item = shop.accepted()
    run_id = run_row(pool)
    started(shop, other, other_item, run_id)
    link(pool, run_id, 51, 81)
    link(pool, run_id, 52, 82)
    settle(pool, run_id, "finished")
    attempt = shop.lifecycle.follow_print(run_id)
    assert attempt is not None

    seam = Archives(facts(81), facts(82, actual_seconds=600, grams=2.5))
    read = await attempt_archives(
        shop.lifecycle, seam, other.id, other_item.id, attempt.id, Starts(), limit=LIMIT
    )

    assert [a.archive_id for a in read.archives] == [82]
    kept = shop.rows.fulfilments(other_item.id)[0]
    assert (kept.archive_ids, kept.actual_seconds, kept.actual_grams) == ([82], 600, 2.5)
    with pytest.raises(ApiError):
        await copy_check(shop.rows, seam, other.id, other_item.id, attempt.id, 81)
    # The hand attempt still has it.
    await copy_check(shop.rows, seam, order.id, item.id, hand.id, 81)
    assert shop.rows.fulfilments(item.id)[0].archive_ids == [81]


def test_a_hand_attempt_cannot_name_another_attempts_archive(shop: Shop, pool: PgPool) -> None:
    """#5268 security review: naming an archive another order's print made would copy
    that customer's photo and timelapse into this order."""
    printed(shop, pool)
    order, item = shop.accepted()
    with pytest.raises(LifecycleError) as raised:
        shop.lifecycle.record_fulfilment(
            order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded", archive_id=81), BY
        )
    assert raised.value.slug == "archive_claimed"
    # Nor one another hand attempt already named.
    shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="failed", archive_id=90), BY
    )
    other, other_item = shop.accepted()
    with pytest.raises(LifecycleError):
        shop.lifecycle.record_fulfilment(
            other.id,
            other_item.id,
            FulfilmentInput(quantity=1, outcome="succeeded", archive_id=90),
            BY,
        )
    # No attempt was recorded, so nothing of either archive can be copied there.
    assert shop.rows.fulfilments(other_item.id) == []


async def test_without_bambuddy_nothing_is_asked(shop: Shop, pool: PgPool) -> None:
    order, item, attempt, _ = printed(shop, pool)
    read = await attempt_archives(
        shop.lifecycle, NOT_LINKED, order.id, item.id, attempt.id, Starts(), limit=LIMIT
    )
    assert (read.linked, read.archives) == (False, [])


async def test_the_finish_photo_and_timelapse_copy_into_the_items_files(
    shop: Shop, pool: PgPool, tmp_path: Path, blobs: CachedBlobStore
) -> None:
    order, item, attempt, _ = printed(shop, pool)
    seam = Archives(facts(81), facts(82))
    claims = ClaimStore(tmp_path / "claims")

    async def copy(what: ArchiveCopyMedia, archive_id: int = 81, name: str | None = None) -> object:
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
    # #5263: named from the facts already read, never by reading the archive again.
    assert seam.downloads[-1] == (81, "timelapse", "timelapse-81.avi")
    files = shop.rows.attachments(order.id)
    # #4251: on the attempt the archive came from, and so under its item.
    assert [(a.role, a.kind, a.item_id, a.fulfilment_id) for a in files] == [
        ("finished", "image", item.id, attempt.id),
        ("timelapse", "video", item.id, attempt.id),
    ]
    assert files[0].caption == "Attempt 1, Bambuddy archive 81"
    assert files[0].filename == "finish_2_bb.jpg"
    # #7232 (spec 2026-11-05 §4.2, §4.5): each copy names its archive and medium, and
    # the audit line says the file came from an archive.
    assert [(a.archive_id, a.archive_media, a.created_by) for a in files] == [
        (81, "finished_photo", BY),
        (81, "timelapse", BY),
    ]
    added = [e for e in shop.rows.events(order.id) if e.kind == "attachment.added"]
    assert [e.detail["source"] for e in added] == ["archive", "archive"]
    # The same bytes again are the file already there.
    assert await copy("finished_photo") == photo
    assert await copy("timelapse") == video
    assert len(shop.rows.attachments(order.id)) == 2
    # No claim is left behind.
    assert [p for p in claims.root.iterdir() if not p.name.startswith(".")] == []


class GatedArchives(Archives):
    """Each download waits until ``count`` downloads have started, so the copies race.
    Each photo's bytes differ by its name."""

    def __init__(self, count: int, *facts: ArchiveFacts) -> None:
        super().__init__(*facts)
        self.count = count
        self.started = 0
        self.all_started = asyncio.Event()

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> CopiedMedia:
        self.started += 1
        if self.started == self.count:
            self.all_started.set()
        await self.all_started.wait()
        self.downloads.append((archive_id, what, name))
        data = JPEG + (name or "").encode()
        target.write_bytes(data)
        return CopiedMedia(
            filename=name or "", sha256=hashlib.sha256(data).hexdigest(), size=len(data)
        )


async def test_two_copies_of_one_photo_at_once_give_one_row(
    shop: Shop, pool: PgPool, tmp_path: Path, blobs: CachedBlobStore
) -> None:
    """#7232 (spec 2026-11-05 §4.6): the manual copy and the import can race. Both answer
    the one row; a second photo of the same archive is a row of its own."""
    order, item, attempt, _ = printed(shop, pool)
    seam = GatedArchives(2, facts(81))
    claims = ClaimStore(tmp_path / "claims")

    async def copy(name: str | None = None) -> Attachment:
        return await copy_in(
            store=shop.rows,
            lifecycle=shop.lifecycle,
            integrations=seam,
            blobs=blobs,
            claims=claims,
            order_id=order.id,
            item_id=item.id,
            fulfilment_id=attempt.id,
            archive_id=81,
            what="finished_photo",
            name=name,
            by=BY,
            limit=1 << 20,
        )

    first, second = await asyncio.gather(copy(), copy())

    assert first == second
    assert shop.rows.attachments(order.id) == [first]
    seam.count, seam.started = 1, 0
    other = await copy("finish_1_aa.jpg")
    assert other.id != first.id
    assert [(a.filename, a.archive_id) for a in shop.rows.attachments(order.id)] == [
        ("finish_2_bb.jpg", 81),
        ("finish_1_aa.jpg", 81),
    ]


class RacedArchives(Archives):
    """While the copy downloads, another copy of the same file is recorded first."""

    def __init__(self, win: Callable[[], None], *facts: ArchiveFacts) -> None:
        super().__init__(*facts)
        self.win = win

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> CopiedMedia:
        await asyncio.to_thread(self.win)
        return await super().download_archive_media(archive_id, what, name, target, limit)


async def test_a_copy_that_loses_the_race_answers_the_winner(
    shop: Shop, pool: PgPool, tmp_path: Path, blobs: CachedBlobStore
) -> None:
    """#7232 (spec §4.6): the winner landed after the sha256 check could see it (here its
    bytes differ, so that check never finds it). The unique index refuses the loser,
    which writes no row, no hold and no audit line, and answers the winner's row."""
    order, item, attempt, _ = printed(shop, pool)
    sha = "d" * 64
    winner: list[Attachment] = []

    def win() -> None:
        winner.append(
            shop.lifecycle.add_attachment(
                order.id,
                item.id,
                BY,
                attachment_id=str(uuid.uuid4()),
                kind="image",
                role="finished",
                filename="finish_2_bb.jpg",
                media_type="image/jpeg",
                size_bytes=10,
                sha256=sha,
                blob_key="attachment-" + sha,
                caption="Attempt 1, Bambuddy archive 81",
                fulfilment_id=attempt.id,
                archive_id=81,
                archive_media="finished_photo",
                source="archive",
            )
        )

    seam = RacedArchives(win, facts(81))
    lost = await copy_in(
        store=shop.rows,
        lifecycle=shop.lifecycle,
        integrations=seam,
        blobs=blobs,
        claims=ClaimStore(tmp_path / "claims"),
        order_id=order.id,
        item_id=item.id,
        fulfilment_id=attempt.id,
        archive_id=81,
        what="finished_photo",
        name=None,
        by=BY,
        limit=1 << 20,
    )

    assert lost == winner[0]
    assert shop.rows.attachments(order.id) == winner
    added = [e for e in shop.rows.events(order.id) if e.kind == "attachment.added"]
    assert len(added) == 1
    with pool.connection() as conn:
        held = conn.execute(
            "SELECT count(*) AS n FROM blob_refs WHERE key = %s",
            ("attachment-" + hashlib.sha256(JPEG).hexdigest(),),
        ).fetchone()
    assert held is not None and held["n"] == 0


async def test_a_copy_is_refused_for_what_the_archive_lacks(
    shop: Shop, pool: PgPool, tmp_path: Path, blobs: CachedBlobStore
) -> None:
    order, item, attempt, _ = printed(shop, pool)
    seam = Archives(facts(81, finish_photos=[], timelapse=False))

    async def refusal(what: ArchiveCopyMedia, archive_id: int, limit: int = 1 << 20) -> object:
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
    assert seam.downloads == [(81, "timelapse", "timelapse-81.avi")]
    assert shop.rows.attachments(order.id) == []
    # Another order's attempt is no attempt of this item.
    other = str(uuid.uuid4())
    with pytest.raises(ApiError) as missing:
        await attempt_archives(
            shop.lifecycle, seam, order.id, item.id, other, Starts(), limit=LIMIT
        )
    assert missing.value.status == 404


async def test_a_heard_outcome_copies_the_attempts_archives(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted()
    run_id = run_row(pool)
    started(shop, order, item, run_id)
    link(pool, run_id, 51, 81)
    seam = Archives(facts(81))
    remove = listen_for_prints(
        shop.bus, shop.lifecycle, lambda run: mirror_run(shop.lifecycle, seam, run, Starts())
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


# -- the media import (#7233, spec 2026-11-05 §4.1, §4.2, §4.6) ------------------------------

AGENT = Actor(kind="agent", principal="agent:orders")


def by_hand(shop: Shop, archive_id: int = 81) -> tuple[Order, OrderItem, Fulfilment]:
    """An attempt the agent recorded by hand, naming ``archive_id``."""
    order, item = shop.accepted()
    attempt = shop.lifecycle.record_fulfilment(
        order.id,
        item.id,
        FulfilmentInput(quantity=1, outcome="succeeded", archive_id=archive_id),
        AGENT,
    )
    return order, item, attempt


class PerArchive(Archives):
    """Each archive's media has bytes of its own, as two prints' photos do."""

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> CopiedMedia:
        copied = await super().download_archive_media(archive_id, what, name, target, limit)
        data = target.read_bytes() + str(archive_id).encode()
        target.write_bytes(data)
        return copied.model_copy(
            update={"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        )


Step = Callable[..., Awaitable[AttemptMediaStep]]


@pytest.fixture
def step(shop: Shop, tmp_path: Path, blobs: CachedBlobStore) -> Step:
    """One step of the attempt's import, as the workflow's activity runs it."""

    async def run(seam: Any, fulfilment_id: str, limit: int = LIMIT) -> AttemptMediaStep:
        return await mirror_attempt(
            shop.lifecycle,
            seam,
            blobs,
            ClaimStore(tmp_path / "claims"),
            fulfilment_id,
            limit=limit,
        )

    return run


def media(shop: Shop, order_id: str) -> list[tuple[str | None, int | None, str | None, str]]:
    return [
        (a.fulfilment_id, a.archive_id, a.archive_media, a.filename)
        for a in shop.rows.attachments(order_id)
    ]


def checked_at(shop: Shop, fulfilment_id: str) -> datetime | None:
    attempt = shop.rows.fulfilment(fulfilment_id)
    assert attempt is not None
    return attempt.media_checked_at


async def test_a_hand_attempt_imports_its_photo_and_timelapse(shop: Shop, step: Step) -> None:
    order, _, attempt = by_hand(shop)
    seam = Archives(facts(81))

    found = await step(seam, attempt.id)

    assert not found.missing
    assert media(shop, order.id) == [
        (attempt.id, 81, "finished_photo", "finish_2_bb.jpg"),
        (attempt.id, 81, "timelapse", "timelapse-81.avi"),
    ]
    # No system actor (Ruling 12): the attempt's creator is the writer.
    assert [a.created_by for a in shop.rows.attachments(order.id)] == [AGENT, AGENT]
    assert checked_at(shop, attempt.id) is not None

    # A second step reads the archive and downloads nothing.
    seam.downloads.clear()
    assert not (await step(seam, attempt.id)).missing
    assert seam.downloads == []
    assert len(shop.rows.attachments(order.id)) == 2


async def test_a_run_attempt_imports_each_archives_media(
    shop: Shop, pool: PgPool, step: Step
) -> None:
    order, _, attempt, _ = printed(shop, pool)
    seam = PerArchive(facts(81), facts(82))

    assert not (await step(seam, attempt.id)).missing

    assert sorted(media(shop, order.id)) == sorted(
        [
            (attempt.id, 81, "finished_photo", "finish_2_bb.jpg"),
            (attempt.id, 81, "timelapse", "timelapse-81.avi"),
            (attempt.id, 82, "finished_photo", "finish_2_bb.jpg"),
            (attempt.id, 82, "timelapse", "timelapse-82.avi"),
        ]
    )
    assert [a.created_by for a in shop.rows.attachments(order.id)] == [attempt.created_by] * 4


async def test_the_latest_photo_is_the_last_by_name(shop: Shop, step: Step) -> None:
    """Bambuddy's list order is not trusted (spec §4.2, F10)."""
    _, _, attempt = by_hand(shop)
    seam = Archives(facts(81, finish_photos=["finish_2_bb.jpg", "finish_1_aa.jpg"]))

    await step(seam, attempt.id)

    assert (81, "finished_photo", "finish_2_bb.jpg") in seam.downloads
    assert (81, "finished_photo", "finish_1_aa.jpg") not in seam.downloads


async def test_a_copy_on_the_item_from_before_5268_counts_as_present(
    shop: Shop, step: Step
) -> None:
    order, item, attempt = by_hand(shop)
    sha = "e" * 64
    shop.lifecycle.add_attachment(
        order.id,
        item.id,
        BY,
        attachment_id=str(uuid.uuid4()),
        kind="image",
        role="finished",
        filename="finish_old.jpg",
        media_type="image/jpeg",
        size_bytes=10,
        sha256=sha,
        blob_key="attachment-" + sha,
        caption="Attempt 1, Bambuddy archive 81",
        archive_id=81,
        archive_media="finished_photo",
    )
    seam = Archives(facts(81))

    assert not (await step(seam, attempt.id)).missing

    assert seam.downloads == [(81, "timelapse", "timelapse-81.avi")]


async def test_a_timelapse_too_large_does_not_stop_the_photo(
    shop: Shop, step: Step, tmp_path: Path, blobs: CachedBlobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal is remembered (#7281 review): a later step and a later read do not
    download it again while the limit is unchanged, so the run ends. The button still
    tries, and a higher limit imports it."""
    order, item, attempt = by_hand(shop)
    seam = Archives(facts(81))
    small = len(JPEG) + 1

    found = await step(seam, attempt.id, limit=small)

    assert not found.missing
    assert media(shop, order.id) == [(attempt.id, 81, "finished_photo", "finish_2_bb.jpg")]
    assert any(
        "too large" in r.getMessage() and str(small) in r.getMessage() for r in caplog.records
    )

    seam.downloads.clear()
    assert not (await step(seam, attempt.id, limit=small)).missing
    assert not (await step(seam, attempt.id, limit=small - 1)).missing
    starts = Starts()
    await attempt_archives(shop.lifecycle, seam, order.id, item.id, attempt.id, starts, limit=small)
    assert (seam.downloads, starts.started) == ([], [])

    # The button still tries, and is refused again.
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
            archive_id=81,
            what="timelapse",
            name=None,
            by=BY,
            limit=small,
        )
    assert refused.value.status == 413
    assert seam.downloads == [(81, "timelapse", "timelapse-81.avi")]

    # A higher limit: the read starts a step, which imports it.
    await attempt_archives(shop.lifecycle, seam, order.id, item.id, attempt.id, starts, limit=LIMIT)
    assert starts.started == [(attempt.id, False)]
    assert not (await step(seam, attempt.id)).missing
    assert (attempt.id, 81, "timelapse", "timelapse-81.avi") in media(shop, order.id)


async def test_a_timelapse_bambuddy_has_later_is_imported_by_a_later_step(
    shop: Shop, step: Step
) -> None:
    order, _, attempt = by_hand(shop)
    seam = Archives(facts(81, timelapse=False, timelapse_name=None, completed_at=datetime.now(UTC)))

    assert (await step(seam, attempt.id)).missing
    assert media(shop, order.id) == [(attempt.id, 81, "finished_photo", "finish_2_bb.jpg")]

    seam.facts[81] = facts(81)
    seam.downloads.clear()
    assert not (await step(seam, attempt.id)).missing
    assert seam.downloads == [(81, "timelapse", "timelapse-81.avi")]


async def test_an_archive_an_hour_old_with_no_timelapse_misses_nothing(
    shop: Shop, step: Step
) -> None:
    """Spec §4.4's rule: an archive that completed an hour ago or more and has no
    timelapse has none to wait for, so a run with retries ends (#7281 review). One with
    no completion time yet still waits."""
    _, _, attempt = by_hand(shop)
    old = datetime.now(UTC) - FRESH
    seam = Archives(facts(81, timelapse=False, timelapse_name=None, completed_at=old))

    assert not (await step(seam, attempt.id)).missing

    seam.facts[81] = facts(81, timelapse=False, timelapse_name=None, completed_at=None)
    assert (await step(seam, attempt.id)).missing


async def test_a_step_with_bambuddy_down_is_missing_and_raises_nothing(
    shop: Shop, step: Step
) -> None:
    order, _, attempt = by_hand(shop)

    assert (await step(FailingArchives(), attempt.id)).missing
    assert shop.rows.attachments(order.id) == []


async def test_a_step_whose_attempt_read_fails_is_missing_and_raises_nothing(
    shop: Shop, step: Step, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7281 review: the database read of the attempt is inside the step's guard."""
    _, _, attempt = by_hand(shop)

    def down(_: str) -> None:
        raise RuntimeError("Postgres is down")

    monkeypatch.setattr(shop.lifecycle.store, "fulfilment", down)

    assert (await step(Archives(facts(81)), attempt.id)).missing


async def test_a_step_whose_stamp_fails_keeps_its_answer(
    shop: Shop, step: Step, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7281 review: a failed ``media_checked_at`` stamp is logged and never replaces the
    step's answer, a failed step's ``missing`` included."""
    order, _, attempt = by_hand(shop)

    def down(*_: object) -> None:
        raise RuntimeError("Postgres is down")

    monkeypatch.setattr(shop.lifecycle.store, "stamp_media_checked", down)

    assert not (await step(Archives(facts(81)), attempt.id)).missing
    assert len(media(shop, order.id)) == 2
    assert (await step(FailingArchives(), attempt.id)).missing


async def test_without_bambuddy_a_step_asks_nothing(shop: Shop, step: Step) -> None:
    order, _, attempt = by_hand(shop)

    assert not (await step(NOT_LINKED, attempt.id)).missing
    assert shop.rows.attachments(order.id) == []
    assert checked_at(shop, attempt.id) is None


async def test_a_hand_attempt_starts_its_import_and_never_downloads_in_the_request(
    shop: Shop,
) -> None:
    """Spec §4.1: the route starts the import after the facts, and answers whether or not
    Temporal took the start."""
    order, item = shop.accepted()
    seam = Archives(facts(81), facts(82))
    starts = Starts()
    body = FulfilmentInput(quantity=1, outcome="succeeded", archive_id=81)

    answered = await record_fulfilment(order.id, item.id, body, orders_of(shop, seam, starts))

    assert starts.started == [(answered.id, True)]
    assert answered.actual_grams == 12.5

    other = FulfilmentInput(quantity=1, outcome="failed", archive_id=82)
    down = orders_of(shop, seam, Starts(fail=True))
    second = await record_fulfilment(order.id, item.id, other, down)
    assert second.archive_ids == [82]
    edit = FulfilmentInput(quantity=1, outcome="failed")
    await update_fulfilment(order.id, item.id, second.id, edit, orders_of(shop, seam, starts))
    assert starts.started[-1] == (second.id, True)
    assert seam.downloads == []

    # An attempt naming no archive starts nothing.
    plain = FulfilmentInput(quantity=1, outcome="failed")
    await record_fulfilment(order.id, item.id, plain, orders_of(shop, seam, starts))
    assert len(starts.started) == 2

    # Without a Bambuddy there is nothing to import: no Temporal call (#7281 review).
    unlinked = FulfilmentInput(quantity=1, outcome="succeeded", archive_id=83)
    await record_fulfilment(order.id, item.id, unlinked, orders_of(shop, NOT_LINKED, starts))
    assert len(starts.started) == 2


async def test_a_heard_outcome_starts_the_import_with_retries(shop: Shop, pool: PgPool) -> None:
    _, _, attempt, run_id = printed(shop, pool)
    starts = Starts()

    await mirror_run(shop.lifecycle, Archives(facts(81), facts(82)), run_id, starts)

    assert starts.started == [(attempt.id, True)]
    # Temporal down: logged, never raised.
    await mirror_run(shop.lifecycle, Archives(facts(81)), run_id, Starts(fail=True))


async def test_an_edit_of_a_hand_attempt_naming_an_archive_starts_the_import_with_retries(
    shop: Shop,
) -> None:
    """#7281 review: ``update_fulfilment`` goes through ``_with_archive_facts``, so each
    edit of a hand attempt that names an archive starts its import with retries; an edit
    of one that names none starts nothing."""
    order, item = shop.accepted()
    seam = Archives(facts(81))
    named = await record_fulfilment(
        order.id,
        item.id,
        FulfilmentInput(quantity=1, outcome="succeeded", archive_id=81),
        orders_of(shop, seam),
    )
    plain = await record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="failed"), orders_of(shop, seam)
    )
    starts = Starts()
    edit = FulfilmentInput(quantity=1, outcome="succeeded")

    await update_fulfilment(order.id, item.id, named.id, edit, orders_of(shop, seam, starts))
    await update_fulfilment(order.id, item.id, plain.id, edit, orders_of(shop, seam, starts))

    assert starts.started == [(named.id, True)]


async def test_a_start_temporal_never_answers_is_given_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7281 review: a start is bounded by ``START_TIMEOUT``; one that never answers is
    logged like any failed start, and the route answers."""

    class Hangs:
        async def start(self, fulfilment_id: str, *, retries: bool) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(attempt_media_module, "START_TIMEOUT", 0.05)

    await asyncio.wait_for(start_media(Hangs(), "f-1", retries=True), 5)


async def test_a_heard_outcome_starts_the_import_when_the_archive_read_fails(
    shop: Shop, pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step reads the archives again, so a failed read here still starts the
    import with retries (#7281 review)."""
    _, _, attempt, run_id = printed(shop, pool)

    async def failing(*_: object, **__: object) -> None:
        raise RuntimeError("Bambuddy did not answer")

    monkeypatch.setattr(archives_module, "mirror_archives", failing)
    starts = Starts()

    await mirror_run(shop.lifecycle, Archives(facts(81)), run_id, starts)

    assert starts.started == [(attempt.id, True)]


async def test_a_read_starts_one_step_only_for_an_offered_medium_not_attached(
    shop: Shop, step: Step
) -> None:
    order, item, attempt = by_hand(shop)
    seam = Archives(facts(81, timelapse=False, timelapse_name=None))
    starts = Starts()

    await attempt_archives(shop.lifecycle, seam, order.id, item.id, attempt.id, starts, limit=LIMIT)
    assert starts.started == [(attempt.id, False)]
    assert seam.downloads == []

    await step(seam, attempt.id)
    await attempt_archives(shop.lifecycle, seam, order.id, item.id, attempt.id, starts, limit=LIMIT)
    # The photo is attached and Bambuddy has no timelapse: nothing to start.
    assert len(starts.started) == 1

    # Temporal down: the read still answers.
    seam.facts[81] = facts(81)
    read = await attempt_archives(
        shop.lifecycle, seam, order.id, item.id, attempt.id, Starts(fail=True), limit=LIMIT
    )
    assert [a.archive_id for a in read.archives] == [81]


async def test_the_read_answers_the_ids_of_what_is_attached(shop: Shop, step: Step) -> None:
    """Spec 2026-11-05 §4.4: the answer says what each archive has attached now, so the
    page shows it after a reload and offers only what is missing."""
    order, item, attempt = by_hand(shop)
    seam = Archives(facts(81))

    read = await attempt_archives(
        shop.lifecycle, seam, order.id, item.id, attempt.id, Starts(), limit=LIMIT
    )
    [offer] = read.archives
    assert (offer.finished_attachment_id, offer.timelapse_attachment_id) == (None, None)

    await step(seam, attempt.id)
    read = await attempt_archives(
        shop.lifecycle, seam, order.id, item.id, attempt.id, Starts(), limit=LIMIT
    )
    ids = {a.archive_media: a.id for a in shop.rows.attachments(order.id)}
    [offer] = read.archives
    assert offer.finished_attachment_id == ids["finished_photo"]
    assert offer.timelapse_attachment_id == ids["timelapse"]


async def test_a_copy_on_the_item_from_before_5268_is_answered_as_attached(shop: Shop) -> None:
    order, item, attempt = by_hand(shop)
    sha = "e" * 64
    kept = shop.lifecycle.add_attachment(
        order.id,
        item.id,
        BY,
        attachment_id=str(uuid.uuid4()),
        kind="image",
        role="finished",
        filename="finish_old.jpg",
        media_type="image/jpeg",
        size_bytes=10,
        sha256=sha,
        blob_key="attachment-" + sha,
        caption="Attempt 1, Bambuddy archive 81",
        archive_id=81,
        archive_media="finished_photo",
    )

    read = await attempt_archives(
        shop.lifecycle, Archives(facts(81)), order.id, item.id, attempt.id, Starts(), limit=LIMIT
    )

    [offer] = read.archives
    assert offer.finished_attachment_id == kept.id
    assert offer.timelapse_attachment_id is None


def aged(pool: PgPool, fulfilment_id: str, created: timedelta, checked: timedelta | None) -> None:
    """The attempt made ``created`` ago, its media last read ``checked`` ago (None: never)."""
    now = datetime.now(UTC)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE order_item_fulfilments SET created_at = %s, media_checked_at = %s"
            " WHERE id = %s",
            (now - created, None if checked is None else now - checked, fulfilment_id),
        )


async def test_the_sweep_starts_one_step_for_an_attempt_missing_a_medium(
    shop: Shop, pool: PgPool, step: Step
) -> None:
    """Spec 2026-11-05 §4.6: an attempt of the last 14 days that names an archive, lacks a
    medium and was last read more than an hour ago gets one step; the sweep asks Bambuddy
    nothing itself."""
    _, _, missing_photo = by_hand(shop, 81)
    await step(PerArchive(facts(81, finish_photos=[], completed_at=None)), missing_photo.id)
    _, _, checked_lately = by_hand(shop, 82)
    _, _, too_old = by_hand(shop, 83)
    _, _, has_both = by_hand(shop, 84)
    await step(PerArchive(facts(84)), has_both.id)
    _, _, never_checked = by_hand(shop, 85)
    shop.accepted()  # an item with no attempt
    order, item = shop.accepted()
    shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="failed"), BY
    )  # an attempt naming no archive
    aged(pool, missing_photo.id, timedelta(days=3), timedelta(hours=2))
    aged(pool, checked_lately.id, timedelta(days=3), timedelta(minutes=10))
    aged(pool, too_old.id, timedelta(days=15), None)
    aged(pool, has_both.id, timedelta(days=3), timedelta(hours=2))
    starts = Starts()
    sweeping = Archives()

    await sweep_media(shop.lifecycle, sweeping, starts, limit=LIMIT)

    assert sorted(starts.started) == sorted([(missing_photo.id, False), (never_checked.id, False)])
    assert sweeping.downloads == []


async def test_the_sweep_starts_a_finished_run_whose_outcome_was_missed(
    shop: Shop, pool: PgPool
) -> None:
    """A run attempt that settled with no outcome heard has no archive copied yet; the
    sweep still starts its step. A run still printing gets none."""
    _, _, attempt, _ = printed(shop, pool)
    order, item = shop.accepted()
    started(shop, order, item, run_row(pool))
    starts = Starts()

    await sweep_media(shop.lifecycle, Archives(), starts, limit=LIMIT)

    assert starts.started == [(attempt.id, False)]


async def test_the_sweep_skips_a_medium_refused_at_the_current_limit(
    shop: Shop, pool: PgPool, step: Step
) -> None:
    """#7281 review: an attempt whose only missing medium the import refused as too large
    at this limit, or a higher one, gets no step; a limit above the refusal's gets one."""
    _, _, attempt = by_hand(shop, 81)
    small = len(JPEG) + 1
    await step(Archives(facts(81)), attempt.id, limit=small)  # the photo; the timelapse refused
    aged(pool, attempt.id, timedelta(days=3), timedelta(hours=2))

    starts = Starts()
    await sweep_media(shop.lifecycle, Archives(), starts, limit=small)
    await sweep_media(shop.lifecycle, Archives(), starts, limit=small - 1)
    assert starts.started == []

    await sweep_media(shop.lifecycle, Archives(), starts, limit=small + 1)
    assert starts.started == [(attempt.id, False)]


async def test_the_sweep_starts_at_most_a_batch_least_lately_checked_first(
    shop: Shop, pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7453: each start may wait `START_TIMEOUT`, so one pass starts at most
    `SWEEP_BATCH` attempts, least lately checked first; the rest wait for the next."""
    monkeypatch.setattr(archives_module, "SWEEP_BATCH", 2)
    _, _, lately = by_hand(shop, 81)
    _, _, never = by_hand(shop, 82)
    _, _, long_ago = by_hand(shop, 83)
    aged(pool, lately.id, timedelta(days=1), timedelta(hours=2))
    aged(pool, never.id, timedelta(days=1), None)
    aged(pool, long_ago.id, timedelta(days=1), timedelta(hours=5))
    starts = Starts()

    assert await sweep_media(shop.lifecycle, Archives(), starts, limit=LIMIT) == 2

    assert starts.started == [(never.id, False), (long_ago.id, False)]


def test_a_full_batch_of_slow_starts_fits_the_sweeps_timeout() -> None:
    """#7453: a pass of `SWEEP_BATCH` starts that each take `START_TIMEOUT` ends inside
    the activity's timeout, with time left for the read."""
    budget = archives_module.SWEEP_BATCH * attempt_media_module.START_TIMEOUT
    assert budget <= ATTEMPT_MEDIA_TIMEOUT.total_seconds() * 0.75


async def test_the_sweep_skips_a_medium_an_archive_past_fresh_does_not_offer(
    shop: Shop, pool: PgPool, step: Step
) -> None:
    """#7453: an archive that completed `FRESH` ago or more and offers no timelapse (the
    printer took none) is not read again each hour for 14 days: the step records it, and
    the sweep leaves it out, as `mirror_attempt`'s ``missing`` does. A medium still
    offered and not imported keeps the attempt in the sweep."""
    old = datetime.now(UTC) - FRESH
    _, _, no_timelapse = by_hand(shop, 81)
    seam = PerArchive(facts(81, timelapse=False, timelapse_name=None, completed_at=old))
    assert not (await step(seam, no_timelapse.id)).missing
    _, _, still_fresh = by_hand(shop, 82)
    fresh = PerArchive(facts(82, timelapse=False, timelapse_name=None, completed_at=None))
    assert (await step(fresh, still_fresh.id)).missing
    _, _, photo_failed = by_hand(shop, 83)
    assert (
        await step(
            FailingDownloads(facts(83, timelapse=False, timelapse_name=None, completed_at=old)),
            photo_failed.id,
        )
    ).missing
    for attempt in (no_timelapse, still_fresh, photo_failed):
        aged(pool, attempt.id, timedelta(days=3), timedelta(hours=2))
    starts = Starts()

    await sweep_media(shop.lifecycle, Archives(), starts, limit=LIMIT)

    assert sorted(starts.started) == sorted([(still_fresh.id, False), (photo_failed.id, False)])


async def test_a_medium_offered_later_returns_the_attempt_to_the_sweep(
    shop: Shop, pool: PgPool, step: Step
) -> None:
    """#7453: the record of a medium not offered is rewritten by each step, so a
    timelapse Bambuddy offers later, whose import failed, is swept again."""
    old = datetime.now(UTC) - FRESH
    _, _, attempt = by_hand(shop, 81)
    await step(
        PerArchive(facts(81, timelapse=False, timelapse_name=None, completed_at=old)), attempt.id
    )
    assert (await step(TimelapseFails(facts(81, completed_at=old)), attempt.id)).missing
    aged(pool, attempt.id, timedelta(days=3), timedelta(hours=2))
    starts = Starts()

    await sweep_media(shop.lifecycle, Archives(), starts, limit=LIMIT)

    assert starts.started == [(attempt.id, False)]


class FailingDownloads(Archives):
    """A Bambuddy whose archives read, but whose media downloads fail."""

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> CopiedMedia:
        raise RuntimeError("Bambuddy did not answer")


class TimelapseFails(Archives):
    """A Bambuddy whose timelapse downloads fail."""

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> CopiedMedia:
        if what == "timelapse":
            raise RuntimeError("Bambuddy did not answer")
        return await super().download_archive_media(archive_id, what, name, target, limit)


async def test_without_bambuddy_the_sweep_starts_nothing(shop: Shop) -> None:
    by_hand(shop)
    starts = Starts()

    await sweep_media(shop.lifecycle, NOT_LINKED, starts, limit=LIMIT)

    assert starts.started == []


class FirstFails(Starts):
    """Temporal refuses the first start only."""

    tries = 0

    async def start(self, fulfilment_id: str, *, retries: bool) -> None:
        self.tries += 1
        if self.tries == 1:
            raise RuntimeError("Temporal is down")
        await super().start(fulfilment_id, retries=retries)


async def test_the_sweep_goes_on_after_a_failed_start(shop: Shop) -> None:
    """A start Temporal refuses is logged; the sweep starts the next attempt, and the
    refused one is tried again at the next sweep (nothing stamped it checked)."""
    _, _, first = by_hand(shop, 81)
    _, _, second = by_hand(shop, 82)
    starts = FirstFails()

    await sweep_media(shop.lifecycle, Archives(), starts, limit=LIMIT)
    assert len(starts.started) == 1

    again = Starts()
    await sweep_media(shop.lifecycle, Archives(), again, limit=LIMIT)
    assert sorted(again.started) == sorted([(first.id, False), (second.id, False)])


async def test_the_read_answers_a_medium_refused_as_too_large(shop: Shop, step: Step) -> None:
    """#7281 review: a medium the import refused at this limit, or a higher one, is
    answered as too large, so the page stops waiting for it; a higher limit clears it."""
    order, item, attempt = by_hand(shop)
    seam = Archives(facts(81))
    small = len(JPEG) + 1
    await step(seam, attempt.id, limit=small)

    read = await attempt_archives(
        shop.lifecycle, seam, order.id, item.id, attempt.id, Starts(), limit=small
    )
    [offer] = read.archives
    assert offer.finished_attachment_id is not None and not offer.finished_too_large
    assert (offer.timelapse_attachment_id, offer.timelapse_too_large) == (None, True)

    read = await attempt_archives(
        shop.lifecycle, seam, order.id, item.id, attempt.id, Starts(), limit=LIMIT
    )
    [offer] = read.archives
    assert (offer.timelapse_attachment_id, offer.timelapse_too_large) == (None, False)
