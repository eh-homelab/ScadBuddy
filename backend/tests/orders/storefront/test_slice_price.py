"""A storefront variant's slice price (#6153): the back office's quote path, one price per
variant by content, the capacity, and no price when unsure. The slicer and the render are
the quote tests' fakes; the rows are Postgres."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import NozzleChoice, SlotChoice
from scadbuddy.bambuddy.resolver import PrintChoices
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import ModelPrintChoices, StoredSettings
from scadbuddy.orders.models import MachineRateBreakdown, PricingPolicy
from scadbuddy.orders.quote import QuoteRun, SlicedPlate, SliceQuoteRequest, quote_run
from scadbuddy.orders.rendering import RenderedOutput
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.slice_price import (
    FILAMENT_GONE,
    GEOMETRY_DIFFERS,
    PROFILE_STALE,
    SLICE_PRICE_KIND,
    SlicePrices,
    SliceRun,
    SliceUnavailableError,
    SliceVariant,
    _start,
    live_rate,
    plan_variant,
    price_key,
    price_variant,
    storefront_choices,
)
from scadbuddy.orders.storefront.slice_profile import StorefrontSliceProfile
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_quote import (
    BY,
    POLICY,
    Book,
    FakeMachineRate,
    FakeOutputs,
    FakeRenders,
    FakeSlicer,
    Shop,
    run_of,
    sliced_plate,
)

pytestmark = pytest.mark.requires_postgres

PROFILE = StorefrontSliceProfile(printer="p-1", spools={"pla": 9, "petg": 5})
CHOICES = PrintChoices(nozzles=[NozzleChoice(size="0.4")])
SHA = "e" * 64
RATE = MachineRateBreakdown(
    printer="p-1",
    printer_name="H2C",
    source="profile",
    rate_per_hour="0.89",
    currency="USD",
    depreciation_per_hour="0.89",
    intervals="typed",
)


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


@pytest.fixture
def prices(pool: PgPool) -> SlicePrices:
    return SlicePrices(pool)


def offering(spools: Collection[int] | None) -> Callable[[str], Awaitable[Collection[int] | None]]:
    async def offered(printer: str) -> Collection[int] | None:
        return spools

    return offered


def slice_run(
    shop: Shop,
    prices: SlicePrices,
    slicer: FakeSlicer,
    *,
    offered: Collection[int] | None = (9, 5),
    book: Book | None = None,
    rendered: str | None = SHA,
) -> SliceRun:
    async def rendered_sha256(_rendered: RenderedOutput) -> str | None:
        return rendered

    async def create_output(body: dict[str, object]) -> dict[str, object]:
        return {"id": "out-sf", "colors": ["#ff0000", "#00ff00"]}

    return SliceRun(
        prices=prices,
        filament=shop.rows.filament,
        book=book or Book(),
        outputs=FakeOutputs(),
        renders=FakeRenders(),
        create_output=create_output,
        slicer=slicer,
        offered=offering(offered),
        rendered_sha256=rendered_sha256,
        follow_every=0.0,
    )


def variant_of(
    shop: Shop, key: str = "a" * 64, *, machine_rate: MachineRateBreakdown | None = None
) -> SliceVariant:
    item = shop.get_item()
    assert item.reference is not None
    return SliceVariant(
        price_key=key,
        model_sha256=SHA,
        reference=item.reference,
        slice=SliceQuoteRequest(
            printer="p-1",
            filament_plan=FilamentPlan(
                slots=[SlotChoice(slot_id=1, spool_id=9), SlotChoice(slot_id=2, spool_id=5)]
            ),
            choices=CHOICES,
        ),
        slot_filaments=[shop.pla, shop.petg],
        currency="USD",
        spools=[9, 5],
        machine_rate=machine_rate,
    )


async def test_a_storefront_price_equals_the_back_office_quote_to_the_cent(
    shop: Shop, prices: SlicePrices, tmp_path: Path
) -> None:
    """The owner's rule (2026-11-14): one render, the same filaments by slot, the same
    printer and spools, the same slice: the storefront price is the quote's, line for
    line."""
    files = {1: sliced_plate(1, 5400, [12.5, 3.0]), 2: sliced_plate(2, 1800, [10.0, 2.0])}
    machine = FakeMachineRate(RATE)
    run: QuoteRun = run_of(shop, tmp_path, files, machine_rate=machine)
    request = shop.request(s0=shop.pla, s1=shop.petg).model_copy(update={"printer": "p-1"})
    quote = await quote_run(run, shop.order.id, shop.item.id, request, BY)

    slicer = FakeSlicer(files)
    row = await price_variant(slice_run(shop, prices, slicer), variant_of(shop, machine_rate=RATE))
    assert row.status == "done"
    assert row.total_minor == quote.total_minor
    assert row.unit_price_minor == quote.unit_price_minor
    assert row.unit_cost_minor == quote.unit_cost_minor
    assert row.lines == quote.lines
    assert row.printer == "p-1"
    assert (row.print_seconds, row.grams) == (7200, {"0": "22.5", "1": "5.0"})
    assert slicer.subjects == ["storefront_price:" + "a" * 64]


async def test_a_done_variant_is_never_sliced_again(
    shop: Shop, prices: SlicePrices, tmp_path: Path
) -> None:
    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 5.0])})
    run = slice_run(shop, prices, slicer)
    first = await price_variant(run, variant_of(shop))
    again = await price_variant(run, variant_of(shop))
    assert again == first
    assert len(slicer.subjects) == 1


async def test_a_profile_spool_no_longer_offered_gives_no_price(
    shop: Shop, prices: SlicePrices
) -> None:
    """#6255 review: a loaded tray's negative id moves when a spool is swapped, so the
    profile is checked against what the printer offers before it prices."""
    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 5.0])})
    row = await price_variant(slice_run(shop, prices, slicer, offered=(9,)), variant_of(shop))
    assert (row.status, row.failure, row.total_minor) == ("failed", PROFILE_STALE, None)
    assert slicer.subjects == []
    gone = await price_variant(
        slice_run(shop, prices, slicer, offered=None), variant_of(shop, "b" * 64)
    )
    assert gone.failure == PROFILE_STALE


async def test_an_archived_filament_gives_no_price(shop: Shop, prices: SlicePrices) -> None:
    shop.rows.archive_filament(shop.petg)
    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 5.0])})
    row = await price_variant(slice_run(shop, prices, slicer), variant_of(shop))
    assert (row.status, row.failure) == ("failed", FILAMENT_GONE)


async def test_a_plate_that_cannot_be_priced_gives_no_price(
    shop: Shop, prices: SlicePrices
) -> None:
    """A slot at 0 g was not sliced: never a price at nothing."""
    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 0.0])})
    row = await price_variant(slice_run(shop, prices, slicer), variant_of(shop))
    assert row.status == "failed"
    assert row.failure is not None and "0 g" in row.failure


def test_one_claim_per_variant_and_the_capacity(prices: SlicePrices) -> None:
    first = prices.claim("1" * 64, SHA, limit=1)
    assert first.claimed and first.row is not None and first.row.status == "running"
    # A second read of the same variant joins it.
    again = prices.claim("1" * 64, SHA, limit=1)
    assert not again.claimed and again.row is not None and again.row.attempt == 1
    # Another variant waits while the one slice runs.
    waiting = prices.claim("2" * 64, SHA, limit=1)
    assert (waiting.claimed, waiting.row) == (False, None)
    # A second slot lets it run; the capacity is read at each claim.
    assert prices.claim("2" * 64, SHA, limit=2).claimed


def test_a_failed_variant_waits_before_it_is_tried_again(prices: SlicePrices) -> None:
    prices.fail("3" * 64, SHA, "the slice failed")
    stood = prices.claim("3" * 64, SHA, limit=1)
    assert not stood.claimed and stood.row is not None and stood.row.status == "failed"
    with prices._pool.connection() as conn:
        conn.execute(
            "UPDATE storefront_slice_prices SET finished_at = finished_at - interval '2 hours'"
        )
    retried = prices.claim("3" * 64, SHA, limit=1)
    assert retried.claimed and retried.row is not None and retried.row.attempt == 2


def test_a_lost_slice_frees_its_place(prices: SlicePrices) -> None:
    prices.claim("4" * 64, SHA, limit=1)
    with prices._pool.connection() as conn:
        conn.execute(
            "UPDATE storefront_slice_prices SET started_at = started_at - interval '3 hours'"
        )
    assert prices.claim("5" * 64, SHA, limit=1).claimed
    assert prices.claim("4" * 64, SHA, limit=2).claimed


def test_the_key_changes_with_the_book_the_rate_the_filaments_and_the_profile(
    shop: Shop,
) -> None:
    pla = shop.rows.filament(shop.pla)
    petg = shop.rows.filament(shop.petg)
    assert pla is not None and petg is not None
    base: dict[str, Any] = {
        "model_sha256": SHA,
        "filaments": [pla, petg],
        "profile": PROFILE,
        "choices": CHOICES,
        "currency": "USD",
        "policy": POLICY,
        "machine_rate": "0.89",
    }
    key = price_key(**base)
    assert key == price_key(**base)
    changed: list[dict[str, Any]] = [
        {"model_sha256": "f" * 64},
        {"filaments": [petg, pla]},
        {"profile": PROFILE.model_copy(update={"spools": {"pla": 9, "petg": 6}})},
        {"choices": CHOICES.model_copy(update={"bed_type": "Cool Plate"})},
        {"currency": "EUR"},
        {"policy": PricingPolicy(machine_rate_per_hour="2", setup_fee="0.5")},
        {"machine_rate": "0.90"},
    ]
    for change in changed:
        assert price_key(**{**base, **change}) != key, change
    dearer = pla.model_copy(update={"cost_per_kg": "25"})
    assert price_key(**{**base, "filaments": [dearer, petg]}) != key


def test_a_variant_with_no_profile_or_no_spool_for_a_material_has_no_plan(shop: Shop) -> None:
    item = shop.get_item()
    assert item.reference is not None
    pla = shop.rows.filament(shop.pla)
    petg = shop.rows.filament(shop.petg)
    assert pla is not None and petg is not None
    asked: dict[str, Any] = {
        "reference": item.reference,
        "model_sha256": SHA,
        "filaments": [pla, petg],
        "currency": "USD",
        "policy": POLICY,
        "machine_rate": None,
    }
    assert plan_variant(settings=StoredSettings(), **asked) is None
    only_pla = StoredSettings(
        storefront_slice_profile=StorefrontSliceProfile(printer="p-1", spools={"pla": 9})
    )
    assert plan_variant(settings=only_pla, **asked) is None
    planned = plan_variant(settings=StoredSettings(storefront_slice_profile=PROFILE), **asked)
    assert planned is not None
    assert planned.variant.spools == [9, 5]
    assert planned.variant.slice.printer == "p-1"
    # The profile names no support or process setting: the template's apply.
    assert planned.variant.slice.supports is None
    assert planned.variant.slice.part_settings is None


async def test_a_live_rate_change_is_a_new_key_and_the_old_price_is_not_reused(
    shop: Shop, prices: SlicePrices
) -> None:
    """#6153: the key holds the live rate that prices the slice, so a rate change gives a
    new key, and the done row of the old rate is never the answer."""
    item = shop.get_item()
    assert item.reference is not None
    pla = shop.rows.filament(shop.pla)
    petg = shop.rows.filament(shop.petg)
    assert pla is not None and petg is not None
    settings = StoredSettings(storefront_slice_profile=PROFILE)
    asked: dict[str, Any] = {
        "settings": settings,
        "reference": item.reference,
        "model_sha256": SHA,
        "filaments": [pla, petg],
        "currency": "USD",
        "policy": POLICY,
    }
    dearer = RATE.model_copy(update={"rate_per_hour": "5.00"})
    old = plan_variant(**asked, machine_rate=RATE)
    new = plan_variant(**asked, machine_rate=dearer)
    assert old is not None and new is not None
    assert old.key != new.key
    assert new.variant.machine_rate == dearer
    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 5.0])})
    run = slice_run(shop, prices, slicer)
    first = await price_variant(run, old.variant)
    assert first.status == "done"
    assert prices.get(new.key) is None
    second = await price_variant(run, new.variant)
    assert len(slicer.subjects) == 2
    assert second.total_minor is not None and first.total_minor is not None
    assert second.total_minor > first.total_minor


def test_the_choices_are_the_dialogs_remembered_ones(shop: Shop) -> None:
    remembered = StoredSettings(
        model_print_choices={
            "demo": ModelPrintChoices(
                nozzles=[NozzleChoice(size="0.6", flow="high_flow")],
                tier="fine",
                process_name="P",
            )
        }
    )
    choices = storefront_choices(remembered, "demo", "p-1")
    assert [n.size for n in choices.nozzles] == ["0.6", "0.6"]
    assert (choices.tier, choices.process_name) == ("fine", "P")
    fresh = storefront_choices(StoredSettings(), "demo", "p-1")
    assert [(n.size, n.flow) for n in fresh.nozzles] == [("0.4", "standard")] * 2
    assert fresh.tier == "standard"


class UnavailableSlicer(FakeSlicer):
    """The slicer's file read answers a passing 503."""

    async def fetch(self, sliced: SlicedPlate) -> bytes:
        raise ApiError(503, "the content store is not there yet")


async def test_a_passing_failure_is_tried_again_not_priced_by_hand(
    shop: Shop, prices: SlicePrices
) -> None:
    """#6370 review: a 503 only means "try again later", so the run raises, the
    operation tries again, and the row stays ``running``."""
    key = "c" * 64
    assert prices.claim(key, SHA, limit=1).claimed
    slicer = UnavailableSlicer({1: sliced_plate(1, 3600, [10.0, 5.0])})
    with pytest.raises(SliceUnavailableError):
        await price_variant(slice_run(shop, prices, slicer), variant_of(shop, key))
    row = prices.get(key)
    assert row is not None and row.status == "running"


def test_each_claim_has_its_own_key_and_a_failed_start_puts_the_row_back(
    prices: SlicePrices,
) -> None:
    """#6370 review's sequence: attempt 1 fails, a claim an hour later fails to start,
    and the next claim must not reuse attempt 1's operation key."""
    key = "d" * 64
    first = prices.claim(key, SHA, limit=1)
    assert first.claimed and first.row is not None and first.row.claim_key is not None
    prices.lost(key, first.row.claim_key, "attempt 1 failed")
    with prices._pool.connection() as conn:
        conn.execute(
            "UPDATE storefront_slice_prices SET finished_at = finished_at - interval '2 hours'"
        )
    second = prices.claim(key, SHA, limit=1)
    assert second.claimed and second.row is not None and second.row.claim_key is not None
    assert second.previous is not None and second.previous.status == "failed"
    assert second.row.claim_key != first.row.claim_key
    # Its start fails: the failed row comes back as it was, not deleted.
    prices.release(key, second.row.claim_key, second.previous)
    back = prices.get(key)
    assert back is not None and back.status == "failed"
    assert (back.failure, back.finished_at) == (
        second.previous.failure,
        second.previous.finished_at,
    )
    third = prices.claim(key, SHA, limit=1)
    assert third.claimed and third.row is not None
    assert third.row.claim_key not in (first.row.claim_key, second.row.claim_key)
    assert third.row.attempt > second.row.attempt
    # A claim that made the row gives it back by deleting it.
    fresh = prices.claim("e" * 64, SHA, limit=2)
    assert fresh.claimed and fresh.previous is None and fresh.row is not None
    assert fresh.row.claim_key is not None
    prices.release("e" * 64, fresh.row.claim_key, None)
    assert prices.get("e" * 64) is None


def test_a_stale_failure_waits_for_room_instead_of_no_price(prices: SlicePrices) -> None:
    """#6370 review: with the capacity full, a variant whose failure is past
    `RETRY_FAILED_AFTER` waits for a place; it is not answered as no price."""
    prices.fail("f" * 64, SHA, "the slice failed")
    with prices._pool.connection() as conn:
        conn.execute(
            "UPDATE storefront_slice_prices SET finished_at = finished_at - interval '2 hours'"
        )
    assert prices.claim("0" * 64, SHA, limit=1).claimed
    waiting = prices.claim("f" * 64, SHA, limit=1)
    assert (waiting.claimed, waiting.row) == (False, None)


async def test_the_live_rate_is_read_once_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    """#6370 review: a page's re-reads while its slice runs ask for the rate once."""
    asked: list[str | None] = []

    class Rates:
        async def for_quote(
            self, printer: str | None, printer_id: int | None
        ) -> MachineRateBreakdown | None:
            asked.append(printer)
            return RATE

    rates = Rates()
    assert await live_rate(rates, "p-1") == RATE
    assert await live_rate(rates, "p-1") == RATE
    assert asked == ["p-1"]
    import scadbuddy.orders.storefront.slice_price as slice_price

    monkeypatch.setattr(slice_price, "RATE_TTL", 0.0)
    await live_rate(rates, "p-1")
    assert asked == ["p-1", "p-1"]


@pytest.mark.parametrize("may_have_started", [True, False])
async def test_a_start_that_may_have_run_keeps_its_place(
    shop: Shop, prices: SlicePrices, monkeypatch: pytest.MonkeyPatch, may_have_started: bool
) -> None:
    """#6370 review: a start whose answer says the operation may still run keeps the row
    ``running``, so it counts against the capacity; one that surely did not start gives
    the claim back."""
    import scadbuddy.api.operations as api_operations
    from scadbuddy.orders.storefront.slice_price import Planned

    async def refused(*_args: Any, **_kwargs: Any) -> None:
        raise ApiError(503, "Temporal did not answer", may_have_started=may_have_started)

    monkeypatch.setattr(api_operations, "run_operation", refused)
    key = "9" * 64
    claim = prices.claim(key, SHA, limit=1)
    assert claim.row is not None and claim.row.claim_key is not None
    kind = SimpleNamespace(name=SLICE_PRICE_KIND, unkeyed=frozenset())
    ops = SimpleNamespace(kinds={SLICE_PRICE_KIND: kind})
    state: Any = SimpleNamespace(components=SimpleNamespace(get=lambda _name: ops))
    planned = Planned(key=key, variant=variant_of(shop, key))
    await _start(state, prices, planned, claim.row.claim_key, claim.previous)
    row = prices.get(key)
    if may_have_started:
        assert row is not None and row.status == "running"
        assert row.operation_key is not None
    else:
        assert row is None


@pytest.mark.parametrize("rendered", ["0" * 64, None], ids=["another-3mf", "unreadable"])
async def test_a_render_of_another_3mf_than_the_key_gets_no_price(
    shop: Shop, prices: SlicePrices, rendered: str | None
) -> None:
    """#6370 review: the run renders the variant again; when that render's 3MF is not the
    one the key names (or cannot be read), no firm price is given and nothing is sliced."""
    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 5.0])})
    row = await price_variant(
        slice_run(shop, prices, slicer, rendered=rendered), variant_of(shop, "8" * 64)
    )
    assert (row.status, row.failure, row.total_minor) == ("failed", GEOMETRY_DIFFERS, None)
    assert slicer.subjects == []
