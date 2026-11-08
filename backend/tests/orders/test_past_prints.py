"""A past print as an order item (#4777, order operations spec 2026-11-07 §3.2): the item
keeps the archives it was taken from, an archive no ScadBuddy subject printed is copied
into the native library once, and the item is priced from what the print used (quote
source ``archive``), with no re-slice."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from scadbuddy.core.problems import ApiError
from scadbuddy.orders.costs import _unit
from scadbuddy.orders.integrations import (
    NOT_LINKED,
    ArchiveFacts,
    ArchiveMedia,
    ArchiveSlot,
    CopiedMedia,
)
from scadbuddy.orders.lifecycle import NewLibraryItem, NewModelItem
from scadbuddy.orders.models import LibraryFileRef, OrderItem
from scadbuddy.orders.past_prints import (
    ArchiveQuoteRequest,
    archive_colours,
    copied_file,
    quote_from_archive,
)
from scadbuddy.orders.problems import PROBLEMS
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_archives import Archives
from tests.orders.test_lifecycle import BY, resolved
from tests.orders.test_quote import Book
from tests.orders.test_quote import Shop as QuoteShop

pytestmark = pytest.mark.requires_postgres

OUTPUT = "o" * 32
GCODE = b"PK\x03\x04 a sliced 3mf"


def plate(archive_id: int, **fields: object) -> ArchiveFacts:
    """A completed plate archive: 30 min printed (40 estimated), 10 g in slot 1 and 5 g
    in slot 2."""
    return ArchiveFacts.model_validate(
        {
            "archive_id": archive_id,
            "status": "completed",
            "plate_id": 1,
            "actual_seconds": 1800,
            "print_seconds": 2400,
            "filename": "bracket.gcode.3mf",
            "slots": [
                {"slot_id": 1, "used_g": 10.0, "color": "#FF0000"},
                {"slot_id": 2, "used_g": 5.0, "color": "00FF00FF"},
            ],
            **fields,
        }
    )


class Seam(Archives):
    """`Archives` whose download is the archive's printed file."""

    async def download_archive_media(
        self, archive_id: int, what: ArchiveMedia, name: str | None, target: Path, limit: int
    ) -> CopiedMedia:
        assert what == "file"
        self.downloads.append((archive_id, what, name))
        target.write_bytes(GCODE)
        found = self.facts[archive_id]
        return CopiedMedia(
            filename=found.filename or f"archive-{archive_id}.3mf",
            sha256=hashlib.sha256(GCODE).hexdigest(),
            size=len(GCODE),
        )


def send(
    pool: PgPool,
    subject: str,
    run_id: str | None,
    queue_item_id: int,
    plate_id: int,
    archive_id: int | None,
) -> None:
    """A queue item a run sent, and the archive Bambuddy reported for it, if any."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_sends (queue_item_id, subject, run_id, plate_id)"
            " VALUES (%s, %s, %s, %s)",
            (queue_item_id, subject, run_id, plate_id),
        )
        if archive_id is not None:
            conn.execute(
                "INSERT INTO print_links (subject, archive_id, matched_by, queue_item_id,"
                " plate_id) VALUES (%s, %s, 'queue_item', %s, %s)",
                (subject, archive_id, queue_item_id, plate_id),
            )


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> QuoteShop:
    return QuoteShop(rows, BlobRefs(pool))


def taken_from(pool: PgPool, item_id: str, *archive_ids: int) -> None:
    """The item as if it were added from those archives."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE order_items SET origin_archive_ids = %s WHERE id = %s",
            (list(archive_ids), item_id),
        )


# -- where an archive came from ---------------------------------------------------------------


def test_an_archive_names_the_output_or_file_that_printed_it(
    rows: OrderStore, pool: PgPool
) -> None:
    send(pool, f"output:{OUTPUT}", "run-1", 51, 1, 81)
    send(pool, "library:89", "run-2", 61, 1, 91)
    out = rows.archive_origin(81)
    assert out is not None and (out.output_id, out.library_file_id) == (OUTPUT, None)
    lib = rows.archive_origin(91)
    assert lib is not None and (lib.output_id, lib.library_file_id) == (None, 89)
    assert rows.archive_origin(99) is None


def test_an_archives_run_lists_every_plate_and_its_archive(rows: OrderStore, pool: PgPool) -> None:
    subject = f"output:{OUTPUT}"
    send(pool, subject, "run-1", 52, 2, None)
    send(pool, subject, "run-1", 51, 1, 81)
    send(pool, subject, "run-other", 53, 1, 83)
    assert rows.archive_run_plates(81) == [(1, 81), (2, None)]
    # A send recorded before runs were has no run: its archive stands alone.
    send(pool, subject, None, 54, 1, 84)
    assert rows.archive_run_plates(84) == []
    assert rows.archive_run_plates(99) == []


def test_an_item_keeps_the_archives_it_was_taken_from(shop: QuoteShop) -> None:
    order = shop.order
    model, library = shop.lifecycle.add_items(
        order.id,
        BY,
        [
            NewModelItem(resolved=resolved(), quantity=1, origin_archive_ids=(81, 82)),
            NewLibraryItem(
                file=LibraryFileRef(id=89, filename="bracket.3mf", library="bambuddy"),
                quantity=1,
                origin_archive_ids=(91,),
            ),
        ],
    )
    assert (model.origin_archive_ids, library.origin_archive_ids) == ([81, 82], [91])
    assert shop.rows.item(model.id) == model
    assert shop.get_item().origin_archive_ids == []
    added = [e.detail for e in shop.rows.events(order.id) if e.kind == "item.added"]
    assert added[-2]["origin_archive_ids"] == [81, 82]


# -- the copy into the native library ---------------------------------------------------------


class Copies:
    """The native library: the files it holds and what it was given."""

    def __init__(self) -> None:
        self.files: dict[int, str] = {}
        self.next = 5_000_000_001
        self.trashed: list[int] = []

    async def trash(self, file_id: int) -> None:
        self.trashed.append(file_id)
        self.files.pop(file_id, None)

    async def file(self, file_id: int) -> LibraryFileRef | None:
        name = self.files.get(file_id)
        return None if name is None else LibraryFileRef(id=file_id, filename=name, library="native")

    async def upload(self, filename: str, path: Path) -> LibraryFileRef:
        assert path.read_bytes() == GCODE
        file_id, self.next = self.next, self.next + 1
        self.files[file_id] = filename
        return LibraryFileRef(id=file_id, filename=filename, library="native")


async def test_a_foreign_archive_is_copied_once_and_reused(
    rows: OrderStore, tmp_path: Path
) -> None:
    seam, copies = Seam(plate(71)), Copies()
    first = await copied_file(rows, seam, copies, 71, tmp_path)
    assert (first.filename, first.library) == ("bracket.gcode.3mf", "native")
    again = await copied_file(rows, seam, copies, 71, tmp_path)
    assert again == first
    assert seam.downloads == [(71, "file", "bracket.gcode.3mf")]
    assert list(tmp_path.iterdir()) == []
    # Its library file trashed since, a pick copies it again.
    del copies.files[first.id]
    third = await copied_file(rows, seam, copies, 71, tmp_path)
    assert third.id != first.id and len(seam.downloads) == 2


async def test_the_copy_makes_its_folder_and_a_lost_race_trashes_its_copy(
    rows: OrderStore, tmp_path: Path
) -> None:
    # The store's spool is made on its first use: a fresh pod has none yet.
    spool = tmp_path / "cache" / "blob-spool"
    seam, copies = Seam(plate(71)), Copies()
    first = await copied_file(rows, seam, copies, 71, spool)
    assert first.id in copies.files and list(spool.iterdir()) == []

    class Racing(Copies):
        """Another pick records its copy of 72 while this one uploads."""

        async def upload(self, filename: str, path: Path) -> LibraryFileRef:
            made = await super().upload(filename, path)
            rows.record_archive_copy(72, first.id, replace=None)
            return made

    racing = Racing()
    racing.next = 6_000_000_001
    racing.files[first.id] = "theirs.gcode.3mf"
    won = await copied_file(rows, Seam(plate(72)), racing, 72, spool)
    # The other pick's copy stands; this pick's own is not left in the library.
    assert won.id == first.id
    assert racing.trashed == [6_000_000_001]


async def test_an_archive_gone_before_the_copy_is_refused(rows: OrderStore, tmp_path: Path) -> None:
    with pytest.raises(ApiError) as gone:
        await copied_file(rows, Seam(), Copies(), 71, tmp_path)
    assert (gone.value.status, gone.value.type) == (409, PROBLEMS + "archive_gone")
    with pytest.raises(ApiError) as unlinked:
        await copied_file(rows, NOT_LINKED, Copies(), 71, tmp_path)
    assert (unlinked.value.status, unlinked.value.type) == (409, PROBLEMS + "not_linked")


# -- the archive quote ------------------------------------------------------------------------


def archive_request(shop: QuoteShop, archive_id: int | None = None) -> ArchiveQuoteRequest:
    return ArchiveQuoteRequest(archive_id=archive_id, filaments={0: shop.pla, 1: shop.petg})


async def test_a_two_plate_print_is_priced_from_both_archives(
    shop: QuoteShop, pool: PgPool
) -> None:
    subject = f"output:{OUTPUT}"
    send(pool, subject, "run-1", 51, 1, 81)
    send(pool, subject, "run-1", 52, 2, 82)
    seam = Seam(
        plate(81),
        # A plate with no actual time is priced at its estimate.
        plate(82, plate_id=2, actual_seconds=None, slots=[{"slot_id": 1, "used_g": 4.0}]),
    )
    # A print of the item's own output is its print, whether or not it was added from it.
    with shop.rows.transaction() as conn:
        conn.execute("UPDATE order_items SET output_id = %s WHERE id = %s", (OUTPUT, shop.item.id))
    quote = await quote_from_archive(
        shop.lifecycle, seam, Book(), shop.order.id, shop.item.id, archive_request(shop, 82), BY
    )
    assert quote.source == "archive"
    assert [(p["index"], p["print_seconds"], p["archive_id"]) for p in quote.plates] == [
        (1, 1800, 81),
        (2, 2400, 82),
    ]
    assert [[(f["slot"], f["used_g"]) for f in p["filaments"]] for p in quote.plates] == [
        [(0, "10.0"), (1, "5.0")],
        [(0, "4.0")],
    ]
    # 14 g PLA at 20/kg, 5 g PETG at 30/kg, 4200 s at 1.5/h, 0.5 setup.
    lines: dict[str, int] = {}
    for line in quote.lines:
        lines[line.kind] = lines.get(line.kind, 0) + line.amount_minor
    assert (lines["filament"], lines["machine_time"], lines["setup"]) == (28 + 15, 175, 100)
    assert shop.get_item().status == "quoted"
    unit = _unit(quote)
    assert unit is not None and unit.grams == Decimal("19.0")


async def test_an_item_quotes_from_the_archive_it_was_taken_from(
    shop: QuoteShop,
) -> None:
    item = shop.lifecycle.add_items(
        shop.order.id, BY, [NewModelItem(resolved=resolved(), quantity=2, origin_archive_ids=(71,))]
    )[0]
    seam = Seam(plate(71, slots=[{"slot_id": 1, "used_g": 3.0}]))
    request = ArchiveQuoteRequest(filaments={0: shop.pla})
    quote = await quote_from_archive(
        shop.lifecycle, seam, Book(), shop.order.id, item.id, request, BY
    )
    assert ([p["archive_id"] for p in quote.plates], quote.quantity) == ([71], 2)


async def test_an_archive_quote_refuses_a_print_it_cannot_price(
    shop: QuoteShop, pool: PgPool
) -> None:
    subject = f"output:{OUTPUT}"
    send(pool, subject, "run-1", 51, 1, 81)
    send(pool, subject, "run-1", 52, 2, None)

    async def refused(seam: Seam, archive_id: int | None) -> ApiError:
        with pytest.raises(ApiError) as caught:
            await quote_from_archive(
                shop.lifecycle,
                seam,
                Book(),
                shop.order.id,
                shop.item.id,
                archive_request(shop, archive_id),
                BY,
            )
        return caught.value

    unnamed = await refused(Seam(plate(71)), None)
    assert (unnamed.status, unnamed.extensions["errors"][0]["loc"]) == (
        422,
        ["body", "archive_id"],
    )
    # Any other print is not this item's: it is not priced from it.
    stranger = await refused(Seam(plate(55)), 55)
    assert (stranger.status, stranger.extensions["errors"][0]["loc"]) == (
        422,
        ["body", "archive_id"],
    )
    taken_from(pool, shop.item.id, 81, 71)
    missing = await refused(Seam(plate(81)), 81)
    assert (missing.status, missing.type) == (409, PROBLEMS + "archive_plates_missing")
    failed = await refused(Seam(plate(71, status="failed")), 71)
    assert (failed.status, failed.type) == (409, PROBLEMS + "archive_not_succeeded")
    gone = await refused(Seam(), 71)
    assert (gone.status, gone.type) == (409, PROBLEMS + "archive_gone")
    assert shop.rows.quotes(shop.item.id) == []


async def test_a_library_file_item_takes_its_slots_from_the_archive(shop: QuoteShop) -> None:
    item: OrderItem = shop.lifecycle.add_library_item(
        shop.order.id,
        BY,
        LibraryFileRef(id=5_000_000_001, filename="bracket.gcode.3mf", library="native"),
        quantity=1,
        origin_archive_ids=(71,),
    )
    # One filament for every slot the print used: the item has none to name yet.
    request = ArchiveQuoteRequest(filament_id=shop.pla)
    quote = await quote_from_archive(
        shop.lifecycle, Seam(plate(71)), Book(), shop.order.id, item.id, request, BY
    )
    assert (quote.source, quote.revision_id) == ("archive", None)
    after = shop.rows.item(item.id)
    assert after is not None
    assert [(s.slot, s.colour, s.filament_id) for s in after.colours] == [
        (0, "#FF0000", shop.pla),
        (1, "#00FF00", shop.pla),
    ]


def test_archive_colours_take_every_used_slot() -> None:
    slots = [ArchiveSlot(slot_id=1, used_g=2.0, color="#123456"), ArchiveSlot(slot_id=3)]
    plates = [plate(1).model_copy(update={"slots": slots})]
    assert [(s.slot, s.colour) for s in archive_colours(plates, {0: "f"})] == [(0, "#123456")]
