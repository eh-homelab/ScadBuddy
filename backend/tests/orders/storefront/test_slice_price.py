"""A storefront variant's slice price (#6153): the back office's quote path, one price per
variant by content, the capacity, and no price when unsure. The slicer and the render are
the quote tests' fakes; the rows are Postgres."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import NozzleChoice, SlotChoice
from scadbuddy.bambuddy.resolver import PrintChoices
from scadbuddy.library.settings_store import ModelPrintChoices, StoredSettings
from scadbuddy.orders.models import MachineRateBreakdown, PricingPolicy
from scadbuddy.orders.quote import QuoteRun, SliceQuoteRequest, quote_run
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.slice_price import (
    FILAMENT_GONE,
    PROFILE_STALE,
    SlicePrices,
    SliceRun,
    SliceVariant,
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
) -> SliceRun:
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
