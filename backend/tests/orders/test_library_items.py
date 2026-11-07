"""A library file as a line item (#5544): the item names a file of the library (its id,
name and library), no template; it is priced by a slice quote of the file or by hand,
prints the file, and a file gone since it was added is refused, never a 500."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from scadbuddy.bambuddy.follow import Follower
from scadbuddy.bambuddy.outcomes import PrintOutcomeStore
from scadbuddy.bambuddy.output_reader import LocalOutputs
from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver
from scadbuddy.bambuddy.runs import PrintRun, PrintRunStore
from scadbuddy.bambuddy.subject import PrintSubject, library_slug
from scadbuddy.core.events import InProcessEventBus
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.lifecycle import LifecycleError, ManualQuoteInput
from scadbuddy.orders.models import LibraryFileRef, Order, OrderItem
from scadbuddy.orders.printing import print_check
from scadbuddy.orders.problems import PROBLEMS
from scadbuddy.orders.projects import prints
from scadbuddy.orders.quote import QuoteRun, library_colours, quote_check, quote_run, read_plate
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, resolved
from tests.orders.test_quote import (
    POLICY,
    TWO_PLATES,
    Book,
    FakeOutputs,
    FakeRenders,
    FakeSlicer,
    sliced_plate,
)
from tests.orders.test_quote import Shop as QuoteShop

pytestmark = pytest.mark.requires_postgres

FILE = LibraryFileRef(id=89, filename="bracket.3mf", library="bambuddy")


class Library:
    """The library that serves: the files it holds, by id, and each file's slot colours."""

    def __init__(self, *files: int, colours: dict[int, str] | None = None) -> None:
        self.files = set(files)
        self.slot_colours = colours or {}

    async def exists(self, file_id: int) -> bool:
        return file_id in self.files

    async def colours(self, file_id: int) -> dict[int, str]:
        return dict(self.slot_colours)


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def library_item(shop: Shop, order: Order, **fields: Any) -> OrderItem:
    return shop.lifecycle.add_library_item(order.id, BY, FILE, quantity=2, **fields)


def test_a_library_file_item_names_its_file_and_no_template(shop: Shop) -> None:
    order = shop.order()
    item = library_item(shop, order, notes="matte", sku="BR-1")
    assert (item.kind, item.reference, item.library_file) == ("library_file", None, FILE)
    assert (item.colours, item.output_id, item.current_revision_id) == ([], None, None)
    assert (item.status, item.title, item.quantity) == ("draft", "bracket.3mf", 2)
    assert (item.approval, item.requires_printing, item.sku) == ("together", True, "BR-1")
    assert shop.rows.item(item.id) == item
    [added] = [e for e in shop.rows.events(order.id) if e.kind == "item.added"]
    assert added.detail == {
        "item_id": item.id,
        "item_kind": "library_file",
        "library_file_id": 89,
    }
    # A title given wins over the file's name; the item prints, so it gets a project.
    titled = library_item(shop, order, title="Wall bracket")
    assert titled.title == "Wall bracket"
    assert prints(titled)


def test_a_library_file_item_is_priced_by_hand_and_accepted(shop: Shop) -> None:
    order = shop.order()
    item = library_item(shop, order)
    shop.quoted(order, item, price=800)
    item = shop.get_item(item)
    assert item.status == "approved"
    [quote] = shop.rows.quotes(item.id)
    assert (quote.source, quote.revision_id, quote.total_minor) == ("manual", None, 1600)
    assert shop.lifecycle.accept_order(order.id, BY).status == "accepted"
    # A new quantity drops the accepted price, as for any item.
    other = shop.order()
    second = library_item(shop, other)
    shop.quoted(other, second)
    second = shop.lifecycle.update_item(other.id, second.id, BY, quantity=3)
    assert (second.status, [q.accepted for q in shop.rows.quotes(second.id)]) == ("draft", [False])


def test_a_library_file_item_has_no_design_to_change(shop: Shop) -> None:
    order = shop.order()
    item = library_item(shop, order)
    with pytest.raises(LifecycleError) as revision:
        shop.lifecycle.add_revision(order.id, item.id, BY, note="new photos")
    assert revision.value.slug == "item_library_file"
    with pytest.raises(LifecycleError) as reference:
        shop.lifecycle.update_item(order.id, item.id, BY, reference=resolved())
    assert (reference.value.slug, reference.value.unprocessable) == ("reference", True)
    with pytest.raises(LifecycleError) as approval:
        shop.lifecycle.update_item(order.id, item.id, BY, approval="design_first")
    assert approval.value.slug == "approval"
    with pytest.raises(LifecycleError) as test_print:
        shop.lifecycle.claim_print(
            order.id,
            item.id,
            BY,
            request_id="r",
            sliced_sha256=None,
            sliced_blob_key=None,
            revision_id="nothing",
        )
    assert test_print.value.slug == "item_library_file"


def test_the_table_keeps_a_library_file_item_whole(shop: Shop, pool: PgPool) -> None:
    order = shop.order()
    item = library_item(shop, order)
    with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE order_items SET library_source = NULL WHERE id = %s", (item.id,))
    model = shop.item(order)
    with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE order_items SET library_file_id = 5 WHERE id = %s", (model.id,))


# -- the slice quote ----------------------------------------------------------------------


def library_shop(rows: OrderStore, pool: PgPool) -> tuple[QuoteShop, OrderItem]:
    shop = QuoteShop(rows, BlobRefs(pool))
    item = shop.lifecycle.add_library_item(shop.order.id, BY, FILE, quantity=2)
    return shop, item


def library_run(shop: QuoteShop, tmp_path: Path, library: Library, slicer: FakeSlicer) -> QuoteRun:
    async def create_output(body: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("a library file item renders nothing")

    return QuoteRun(
        lifecycle=shop.lifecycle,
        book=Book(),
        outputs=FakeOutputs(),
        renders=FakeRenders(),
        create_output=create_output,
        slicer=slicer,
        blobs=LocalBlobStore(tmp_path / "blobs"),
        refs=shop.refs,
        follow_every=0.0,
        library=library,
    )


def test_the_slice_quote_slices_the_file_and_gives_the_item_its_slots(
    rows: OrderStore, pool: PgPool, tmp_path: Path
) -> None:
    shop, item = library_shop(rows, pool)
    request = shop.request(s0=shop.pla, s1=shop.petg)
    # The check takes the body's filaments for an item with no slots yet.
    assert quote_check(shop.rows, POLICY, shop.order.id, item.id, request) == {
        0: shop.pla,
        1: shop.petg,
    }
    slicer = FakeSlicer({1: TWO_PLATES, 2: TWO_PLATES})
    library = Library(89, colours={1: "#FF8800FF", 2: "nonsense"})
    quote = asyncio.run(
        quote_run(library_run(shop, tmp_path, library, slicer), shop.order.id, item.id, request, BY)
    )
    assert slicer.subjects == [f"order_item:{item.id} file 89"]
    assert (quote.source, quote.revision_id, quote.quantity) == ("slice", None, 2)
    assert [plate["index"] for plate in quote.plates] == [1, 2]
    assert quote.sliced_blob_key is not None and quote.sliced_blob_key.startswith("quote-")
    item = rows.item(item.id) or item
    assert item.status == "quoted"
    assert [(s.slot, s.colour, s.filament_id) for s in item.colours] == [
        (0, "#FF8800", shop.pla),
        (1, "#808080", shop.petg),
    ]
    assert item.current_revision_id is None
    # Accepted as any quote: it names no revision, so nothing is stale.
    accepted = shop.lifecycle.accept_quote(shop.order.id, item.id, quote.id, BY)
    assert accepted.status == "approved"
    # A filament changed on its slots drops the accepted quote and makes no revision.
    changed = shop.lifecycle.update_item(shop.order.id, item.id, BY, filaments={1: shop.pla})
    assert (changed.status, changed.current_revision_id) == ("draft", None)
    assert [s.filament_id for s in changed.colours] == [shop.pla, shop.pla]
    assert not any(q.accepted for q in rows.quotes(item.id))


def test_library_colours_take_every_used_slot() -> None:
    plates = [read_plate(sliced_plate(1, 600, [4.0, 2.0]), 1)]
    colours = library_colours(plates, {1: "#00ff00"}, {0: "f1"})
    assert [(c.slot, c.colour, c.filament_id) for c in colours] == [
        (0, "#00FF00", "f1"),
        (1, "#808080", None),
    ]


def test_a_file_gone_refuses_the_quote_and_the_print(
    rows: OrderStore, pool: PgPool, tmp_path: Path
) -> None:
    shop, item = library_shop(rows, pool)
    request = shop.request(s0=shop.pla, s1=shop.petg)
    slicer = FakeSlicer({1: TWO_PLATES})
    with pytest.raises(ApiError) as quote_refused:
        asyncio.run(
            quote_run(
                library_run(shop, tmp_path, Library(), slicer),
                shop.order.id,
                item.id,
                request,
                BY,
            )
        )
    assert (quote_refused.value.status, quote_refused.value.type) == (
        409,
        PROBLEMS + "library_file_gone",
    )
    assert slicer.subjects == []
    # Priced by hand and accepted, the print checks the file before anything starts.
    quote = shop.lifecycle.record_manual_quote(
        shop.order.id, item.id, ManualQuoteInput(unit_price_minor=500), BY
    )
    shop.lifecycle.accept_quote(shop.order.id, item.id, quote.id, BY)
    # The shop's model item is not this test's.
    shop.lifecycle.cancel_item(shop.order.id, shop.item.id, BY)
    shop.lifecycle.accept_order(shop.order.id, BY)
    blobs = LocalBlobStore(tmp_path / "blobs")

    def check(library: Library | None) -> dict[str, Any]:
        return asyncio.run(
            print_check(rows, FakeOutputs(), blobs, shop.order.id, item.id, library=library)
        )

    with pytest.raises(ApiError) as print_refused:
        check(Library())
    assert (print_refused.value.status, print_refused.value.type) == (
        409,
        PROBLEMS + "library_file_gone",
    )
    assert check(Library(89)) == {
        "library_file_id": 89,
        "copies": 2,
        "quote_id": quote.id,
        "sliced_sha256": None,
        "sliced_blob_key": None,
    }


def test_two_items_of_one_file_printing_at_once_both_settle(shop: Shop, pool: PgPool) -> None:
    """#5613: both items print on the run subject ``library:89``. The follow reads the
    subject's newest run, and the older run by its own run (``Follower.read_run``), and
    records each outcome against its run, so both attempts settle by themselves."""
    order = shop.order()
    older, newer = library_item(shop, order), library_item(shop, order)
    shop.quoted(order, older)
    shop.quoted(order, newer)
    shop.lifecycle.accept_order(order.id, BY)
    subject = PrintSubject.library(FILE.id)
    runs = PrintRunStore(pool)

    async def run(run_id: str, queue_item: int) -> None:
        await runs.insert_accepted(
            run_id,
            subject=subject,
            key=f"k-{run_id}",
            slug=library_slug(subject),
            workflow_id=f"print-{run_id}",
            workflow_run_id="w1",
            retention=None,
        )
        result = PrintRunResult(library_file_id=FILE.id, copies=2, queue_item_ids=[queue_item])
        await runs.succeed(run_id, library_slug(subject), result)

    def started(item: OrderItem, run_id: str, queue_item: int) -> str:
        asyncio.run(run(run_id, queue_item))
        claim = shop.lifecycle.claim_print(
            order.id,
            item.id,
            BY,
            request_id=f"req-{run_id}",
            sliced_sha256=None,
            sliced_blob_key=None,
        )
        return shop.lifecycle.attach_print(claim, run_id, None).id

    started(older, "r-older", 51)
    started(newer, "r-newer", 52)

    def done(queue_item: int) -> PrintProgress:
        return PrintProgress(
            route="slice_queue",
            stage="done",
            settled=True,
            queue_item_id=queue_item,
            bambuddy_url="http://bambuddy.test/queue",
        )

    outcomes = PrintOutcomeStore(pool, None)

    async def read_library(subject: PrintSubject) -> PrintProgress:
        # The subject's own read is of its newest run's print.
        return done(52)

    async def read_run(subject: PrintSubject, run: PrintRun) -> PrintProgress:
        # An older run is read by its own run: the older item's queue item.
        assert run.id == "r-older"
        return done(51)

    async def unused(meta: Any) -> None:
        raise AssertionError("an output read")

    follower = Follower(
        outputs=LocalOutputs(None, None),  # type: ignore[arg-type]
        observer=ProgressObserver(InProcessEventBus()),
        read=unused,
        read_library=read_library,
        events=None,
        record_outcome=outcomes.record,
        older_runs=outcomes,
        read_run=read_run,
        min_interval=0.01,
    )
    ended = asyncio.run(
        asyncio.wait_for(follower.follow(subject.run_subject, datetime.now(UTC)), 10)
    )
    assert ended == "settled"
    for item, run_id in ((newer, "r-newer"), (older, "r-older")):
        settled = shop.lifecycle.follow_print(run_id)
        assert settled is not None and (settled.outcome, settled.quantity) == ("succeeded", 2)
        assert shop.get_item(item).status == "printed"
