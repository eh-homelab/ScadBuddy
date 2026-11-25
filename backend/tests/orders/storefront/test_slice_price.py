"""A storefront variant's slice price (#6153): the back office's quote path, one price per
variant by content, the capacity, and no price when unsure. The slicer and the render are
the quote tests' fakes; the rows are Postgres."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from scadbuddy.bambuddy.choices import PrinterOpening
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import NozzleChoice, PrinterStatus
from scadbuddy.bambuddy.part_settings import PartSettings
from scadbuddy.bambuddy.resolver import PrintChoices
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.models import EffectivePreset
from scadbuddy.library.settings_store import ModelPrintChoices, PrinterPlate, StoredSettings
from scadbuddy.orders.models import Filament, MachineRateBreakdown, PricingPolicy
from scadbuddy.orders.quote import QuoteRun, SlicedPlate, SliceQuoteRequest, quote_run
from scadbuddy.orders.rendering import RenderedOutput
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.held_file import PricedFile
from scadbuddy.orders.storefront.slice_price import (
    FILAMENT_GONE,
    FILE_GONE,
    GEOMETRY_DIFFERS,
    KEY_VERSION,
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
    setup_key,
)
from scadbuddy.orders.storefront.slice_profile import StorefrontSliceProfile
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobScope
from scadbuddy.store.refs import BlobRefs
from tests.bambuddy.conftest import recording
from tests.conftest import PgPool
from tests.orders.test_quote import (
    BY,
    NOW,
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
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres

PLA_TYPE = UUID("00000000-0000-4000-8000-0000000000a1")
PETG_TYPE = UUID("00000000-0000-4000-8000-0000000000a2")
PROFILE = StorefrontSliceProfile(printer="p-1", materials={"pla": PLA_TYPE, "petg": PETG_TYPE})


def _preset(size: str, filament: str) -> EffectivePreset:
    return EffectivePreset(
        printer_model="H2C",
        nozzle_diameter=size,
        slicer_filament=filament,
        slicer_filament_name=None,
        overridden=False,
    )


#: Each type's presets, as `filament_spool_type_presets` holds them (#6574).
TYPE_PRESETS = {
    PLA_TYPE: [_preset("0.4", "Bambu PLA Basic @BBL H2C"), _preset("0.6", "GFA00_06")],
    PETG_TYPE: [_preset("0.4", "Bambu PETG HF @BBL H2C")],
}
SLOT_PRESETS = {1: TYPE_PRESETS[PLA_TYPE], 2: TYPE_PRESETS[PETG_TYPE]}
CHOICES = PrintChoices(nozzles=[NozzleChoice(size="0.4")])
#: An H2C with a High Flow 0.4 mm nozzle, as Bambuddy recorded it.
STATUS = PrinterStatus.model_validate(recording("printer-status-rack.json"))
OPENING = PrinterOpening(status=STATUS, last="Textured PEI Plate")
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

#: The key's subject and setup of a template variant with nothing remembered (#6710).
TEMPLATE: dict[str, str] = {"kind": "template"}
NO_SETUP: dict[str, Any] = {
    "print_sequence": None,
    "process_overrides": None,
    "part_settings": None,
}


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


@pytest.fixture
def prices(pool: PgPool) -> SlicePrices:
    return SlicePrices(pool)


def slice_run(
    shop: Shop,
    prices: SlicePrices,
    slicer: FakeSlicer,
    *,
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
            printer="p-1", filament_plan=FilamentPlan(ids="sb"), choices=CHOICES
        ),
        slot_filaments=[shop.pla, shop.petg],
        currency="USD",
        slot_presets=SLOT_PRESETS,
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
    # The rate that priced the variant is kept with its price, as the quote keeps it, so
    # the checkout's storefront quote records the same breakdown.
    assert row.machine_rate_breakdown == quote.machine_rate_breakdown == RATE
    assert (row.print_seconds, row.grams) == (7200, {"0": "22.5", "1": "5.0"})
    assert slicer.subjects == ["storefront_price:" + "a" * 64]
    # #6574: each slot's type's presets reach the slice.
    assert slicer.slot_presets == [SLOT_PRESETS]


async def test_the_slice_run_saves_its_output_as_the_storefronts(
    shop: Shop, prices: SlicePrices
) -> None:
    """#6371: the run saves the variant's render with ``source`` ``storefront``, so the
    back office's Recent renders leave it out."""
    bodies: list[dict[str, object]] = []

    async def create_output(body: dict[str, object]) -> dict[str, object]:
        bodies.append(body)
        return {"id": "out-sf", "colors": ["#ff0000", "#00ff00"]}

    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 5.0])})
    run = replace(slice_run(shop, prices, slicer), create_output=create_output)
    row = await price_variant(run, variant_of(shop))
    assert row.status == "done"
    [body] = bodies
    assert body["source"] == "storefront"
    assert str(body["name"]).startswith("Storefront: ")


async def test_a_done_variant_is_never_sliced_again(
    shop: Shop, prices: SlicePrices, tmp_path: Path
) -> None:
    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 5.0])})
    run = slice_run(shop, prices, slicer)
    first = await price_variant(run, variant_of(shop))
    again = await price_variant(run, variant_of(shop))
    assert again == first
    # Priced at the book's rate: no breakdown to keep.
    assert first.machine_rate_breakdown is None
    assert len(slicer.subjects) == 1


async def test_an_operation_started_before_6574_still_runs(shop: Shop, prices: SlicePrices) -> None:
    """An operation's request from before #6574 names the profile's spools; it is read,
    and they are not."""
    old = variant_of(shop, "b" * 64).model_dump(mode="json")
    old["spools"] = [9, 5]
    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 5.0])})
    row = await price_variant(slice_run(shop, prices, slicer), SliceVariant.model_validate(old))
    assert row.status == "done"


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
        "filaments": {0: pla, 1: petg},
        "profile": PROFILE,
        "choices": CHOICES,
        "presets": SLOT_PRESETS,
        "currency": "USD",
        "policy": POLICY,
        "machine_rate": "0.89",
        "subject": TEMPLATE,
        "setup": NO_SETUP,
    }
    key = price_key(**base)
    assert key == price_key(**base)
    changed: list[dict[str, Any]] = [
        {"model_sha256": "f" * 64},
        {"filaments": {0: petg, 1: pla}},
        {
            "profile": PROFILE.model_copy(
                update={"materials": {"pla": PETG_TYPE, "petg": PETG_TYPE}}
            )
        },
        # #6574: a type's preset edited is what the slicer gets, so a new key.
        {"presets": {**SLOT_PRESETS, 2: [_preset("0.4", "Generic PETG @BBL H2C")]}},
        {"choices": CHOICES.model_copy(update={"bed_type": "Cool Plate"})},
        {"currency": "EUR"},
        {"policy": PricingPolicy(machine_rate_per_hour="2", setup_fee="0.5")},
        {"machine_rate": "0.90"},
    ]
    for change in changed:
        assert price_key(**{**base, **change}) != key, change
    # A preset for another nozzle is not what this slice gets.
    other = {**SLOT_PRESETS, 2: [*SLOT_PRESETS[2], _preset("0.6", "GFG99")]}
    assert price_key(**{**base, "presets": other}) == key
    dearer = pla.model_copy(update={"cost_per_kg": "25"})
    assert price_key(**{**base, "filaments": {0: dearer, 1: petg}}) != key
    # #6574: a slot's colour changes the purges, so it is in the key.
    recoloured = pla.model_copy(update={"colour": "#123456"})
    assert price_key(**{**base, "filaments": {0: recoloured, 1: petg}}) != key


def _pinned_filament(filament_id: str, material: str, colour: str) -> Filament:
    return Filament(
        id=filament_id,
        material=material,
        subtype=None,
        brand=None,
        colour_name=None,
        colour=colour,
        cost_per_kg="20",
        density_g_cm3=None,
        archived_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


#: A key's inputs, each fixed, so its digest is pinned (#6710 §7.4).
PINNED: dict[str, Any] = {
    "model_sha256": SHA,
    "filaments": {
        0: _pinned_filament("fil-pla", "PLA", "#FF0000"),
        1: _pinned_filament("fil-petg", "PETG", "#00FF00"),
    },
    "profile": PROFILE,
    "choices": CHOICES,
    "presets": SLOT_PRESETS,
    "currency": "USD",
    "policy": POLICY,
    "machine_rate": "0.89",
    "subject": TEMPLATE,
    "setup": NO_SETUP,
}
FILE_SUBJECT: dict[str, str] = {"kind": "library_file", "library": "native"}


def test_the_key_is_version_4_and_its_digests_are_pinned() -> None:
    """#6710 §7.4: `KEY_VERSION` 4, because the key's `model` now means the sha256 of the
    file the slice reads, and it holds the subject's kind and the remembered setup. A
    pinned digest per subject keeps a key from moving again by accident."""
    assert KEY_VERSION == 4
    assert price_key(**PINNED) == "b08ec9e212a909d32c1413614ac7dc6d217c36ff984404aa57c04bc5cfbf8f78"
    assert (
        price_key(**{**PINNED, "subject": FILE_SUBJECT})
        == "812f183766e7e0c66dde1bedd8833ff85a2a5df559378eb227a0230df25a0c7b"
    )


def test_two_subjects_of_one_file_and_one_setup_differ_only_by_their_kind() -> None:
    """#6710 §7.4: a template's render and a library file of the same bytes and setup
    are two prices; the same kind, bytes and setup are one (two listings of one file
    share a price)."""
    template = price_key(**PINNED)
    file = price_key(**{**PINNED, "subject": FILE_SUBJECT})
    assert template != file
    assert price_key(**{**PINNED, "subject": dict(FILE_SUBJECT)}) == file
    bambuddy = price_key(**{**PINNED, "subject": {**FILE_SUBJECT, "library": "bambuddy"}})
    assert bambuddy not in (template, file)


@pytest.mark.parametrize(
    "setup",
    [
        {"process_overrides": {"enable_support": "0"}},
        {"print_sequence": "by object"},
        {"part_settings": {"1": {"wall_loops": 4}}},
    ],
)
def test_any_remembered_setup_is_another_key(setup: dict[str, Any]) -> None:
    """#6710 §7.4: the setup the dialog seeds is what the slicer gets, so each change of
    it is another key."""
    supported = {**NO_SETUP, "process_overrides": {"enable_support": "1"}}
    keys = {
        price_key(**PINNED),
        price_key(**{**PINNED, "setup": supported}),
        price_key(**{**PINNED, "setup": {**NO_SETUP, **setup}}),
    }
    assert len(keys) == 3


def test_a_variant_with_no_profile_type_or_type_preset_has_no_plan(
    shop: Shop,
) -> None:
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
        "printer": OPENING,
        "type_presets": TYPE_PRESETS,
    }
    assert plan_variant(settings=StoredSettings(), **asked) is None
    only_pla = StoredSettings(
        storefront_slice_profile=StorefrontSliceProfile(printer="p-1", materials={"pla": PLA_TYPE})
    )
    assert plan_variant(settings=only_pla, **asked) is None
    profiled = StoredSettings(storefront_slice_profile=PROFILE)
    # #6574: a type with no preset for the H2C and the nozzle the dialog opens on (0.4).
    no_04 = {**TYPE_PRESETS, PETG_TYPE: [_preset("0.6", "GFG99")]}
    assert plan_variant(settings=profiled, **{**asked, "type_presets": no_04}) is None
    planned = plan_variant(settings=profiled, **asked)
    assert planned is not None
    assert planned.variant.slice.filament_plan.slots == []
    assert planned.variant.slice.choices.filament_overrides == {}
    assert planned.variant.slot_presets == SLOT_PRESETS
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
        "printer": OPENING,
        "type_presets": TYPE_PRESETS,
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


def _planned_choices(shop: Shop, settings: StoredSettings, opening: PrinterOpening) -> Any:
    item = shop.get_item()
    assert item.reference is not None
    pla = shop.rows.filament(shop.pla)
    petg = shop.rows.filament(shop.petg)
    assert pla is not None and petg is not None
    planned = plan_variant(
        settings=settings.model_copy(update={"storefront_slice_profile": PROFILE}),
        reference=item.reference,
        model_sha256=SHA,
        filaments=[pla, petg],
        currency="USD",
        policy=POLICY,
        machine_rate=None,
        printer=opening,
        type_presets=TYPE_PRESETS,
    )
    return None if planned is None else planned.variant.slice.choices


def test_the_nozzle_flow_comes_from_the_printers_status(shop: Shop) -> None:
    """#6574: with nothing remembered the variant is sliced on the nozzles the dialog
    opens on, which the printer's live status gives (High Flow on this H2C), never a
    standard-flow guess."""
    choices = _planned_choices(shop, StoredSettings(), OPENING)
    assert [(n.size, n.flow) for n in choices.nozzles] == [("0.4", "high_flow")] * 2
    assert (choices.tier, choices.process_name) == ("standard", None)
    assert choices.bed_type == "Textured PEI Plate"


def test_a_kept_opening_reads_the_plate_remembered_after_it(shop: Shop) -> None:
    """#6574 review: a printer opening kept for `RATE_TTL` reads the plate remembered on
    the printer each time, so a plate remembered after the read is in the next price."""
    kept = PrinterOpening(status=STATUS, printer="p1")
    assert _planned_choices(shop, StoredSettings(), kept).bed_type == "Textured PEI Plate"
    remembered = StoredSettings(printer_plates=[PrinterPlate(printer="p1", bed_type="Cool Plate")])
    assert _planned_choices(shop, remembered, kept).bed_type == "Cool Plate"


def test_the_remembered_process_is_used(shop: Shop) -> None:
    """#6574: the model's remembered nozzles and process, as the dialog opens on them (a
    remembered process opens with no tier)."""
    remembered = StoredSettings(
        model_print_choices={
            shop.get_item().reference.slug: ModelPrintChoices(  # type: ignore[union-attr]
                nozzles=[NozzleChoice(size="0.4", flow="standard")],
                tier="fine",
                process_name="0.16mm High Quality @BBL H2C",
            )
        }
    )
    choices = _planned_choices(shop, remembered, OPENING)
    assert [(n.size, n.flow) for n in choices.nozzles] == [("0.4", "standard")] * 2
    assert (choices.tier, choices.process_name) == (None, "0.16mm High Quality @BBL H2C")


def test_a_printer_status_not_read_gives_no_price(shop: Shop) -> None:
    """#6574, the owner's rule (no price when unsure): a status that cannot be read is
    no plan, not a price at the standard-flow defaults."""
    unread = PrinterOpening(status=None, last="Textured PEI Plate")
    assert _planned_choices(shop, StoredSettings(), unread) is None


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


# -- a file listing's file (#6710 §7) ------------------------------------------------------

#: A Bambuddy-range id: the live file Bambuddy holds at it may be other bytes since.
FILE_ID = 41
HELD = b"the held copy's bytes"
HELD_SHA = hashlib.sha256(HELD).hexdigest()


@pytest.fixture
async def content(tmp_path: Path, pool: PgPool) -> AsyncIterator[ContentStore]:
    store = local_content(tmp_path / "remote", pool)
    try:
        yield store
    finally:
        await store.aclose()


def priced_file(**fields: Any) -> PricedFile:
    return PricedFile.model_validate(
        {
            "id": FILE_ID,
            "library": "bambuddy",
            "filename": "keychain.3mf",
            "sha256": HELD_SHA,
            "copy_key": f"listing-file-{HELD_SHA}",
            "file_type": "3mf",
            "size": len(HELD),
            "plates": [1],
            # Three slots; the middle one prints no part.
            "used": [0, 2],
            **fields,
        }
    )


async def hold(content: ContentStore) -> None:
    await content.put(
        "media", HELD, name="keychain.3mf", scope=BlobScope(), key=f"listing-file-{HELD_SHA}"
    )


def _file_plan(shop: Shop, settings: StoredSettings, file: PricedFile | None = None) -> Any:
    pla = shop.rows.filament(shop.pla)
    petg = shop.rows.filament(shop.petg)
    assert pla is not None and petg is not None
    return plan_variant(
        settings=settings.model_copy(update={"storefront_slice_profile": PROFILE}),
        library_file=file or priced_file(),
        filaments=[pla, petg],
        currency="USD",
        policy=POLICY,
        machine_rate=None,
        printer=OPENING,
        type_presets=TYPE_PRESETS,
    )


def test_a_variant_names_a_render_or_a_file_never_both(shop: Shop) -> None:
    """#6710 §7.1: exactly one subject. An operation started before has a `reference` and
    no `library_file`, so it still reads."""
    body = variant_of(shop).model_dump(mode="json")
    assert SliceVariant.model_validate(body).library_file is None
    with pytest.raises(ValidationError):
        SliceVariant.model_validate({**body, "library_file": priced_file().model_dump()})
    with pytest.raises(ValidationError):
        SliceVariant.model_validate({**body, "reference": None})
    file = SliceVariant.model_validate(
        {**body, "reference": None, "library_file": priced_file().model_dump()}
    )
    assert file.library_file == priced_file()


def test_a_file_is_planned_on_its_own_slots_and_its_own_scope(shop: Shop) -> None:
    """#6710 §7.1, §7.3.1: the used slots take the filaments in order, by slot id (from
    1), with each filament's colour; the remembered choices are `library:<id>`'s, and the
    key's model is the pinned sha256."""
    remembered = ModelPrintChoices(
        process_overrides={"enable_support": "1"},
        print_sequence="by object",
        part_settings={3: PartSettings(wall_loops=4)},
    )
    settings = StoredSettings(model_print_choices={f"library:{FILE_ID}": remembered})
    planned = _file_plan(shop, settings)
    assert planned is not None
    variant = planned.variant
    assert variant.reference is None and variant.library_file == priced_file()
    assert variant.model_sha256 == HELD_SHA
    # Slot 0's filament reaches the slicer as slot id 1, slot 2's as slot id 3.
    assert variant.slot_presets == {1: TYPE_PRESETS[PLA_TYPE], 3: TYPE_PRESETS[PETG_TYPE]}
    assert variant.slot_colours == {1: "#FF0000", 3: "#00FF00"}
    assert variant.slot_filaments == [shop.pla, shop.petg]
    request = variant.slice
    assert request.process_overrides == {"enable_support": "1"}
    assert request.print_sequence == "by object"
    assert request.part_settings == {3: PartSettings(wall_loops=4)}
    # The same request for the template's scope is another key: the subject's kind.
    item = shop.get_item()
    assert item.reference is not None
    pla = shop.rows.filament(shop.pla)
    petg = shop.rows.filament(shop.petg)
    assert pla is not None and petg is not None
    template = plan_variant(
        settings=settings.model_copy(update={"storefront_slice_profile": PROFILE}),
        reference=item.reference,
        model_sha256=HELD_SHA,
        filaments=[pla, petg],
        currency="USD",
        policy=POLICY,
        machine_rate=None,
        printer=OPENING,
        type_presets=TYPE_PRESETS,
    )
    assert template is not None and template.key != planned.key
    assert template.variant.slot_colours == {}


def test_a_file_scope_with_no_entry_reads_the_copy_publish_took(shop: Shop) -> None:
    """#6710 decision 4: `library:<id>` while it holds an entry (also after the file is
    deleted), else the snapshot's `remembered`, else nothing."""
    at_publish = ModelPrintChoices(process_overrides={"brim_width": "4"})
    file = priced_file(remembered=at_publish.model_dump(mode="json"))
    copied = _file_plan(shop, StoredSettings(), file)
    assert copied.variant.slice.process_overrides == {"brim_width": "4"}
    now = ModelPrintChoices(process_overrides={"enable_support": "1"})
    live = _file_plan(shop, StoredSettings(model_print_choices={f"library:{FILE_ID}": now}), file)
    assert live.variant.slice.process_overrides == {"enable_support": "1"}
    assert live.key != copied.key
    bare = _file_plan(shop, StoredSettings())
    assert bare.variant.slice.process_overrides is None
    assert len({bare.key, live.key, copied.key}) == 3


def file_variant(shop: Shop, key: str = "f" * 64, **fields: Any) -> SliceVariant:
    return SliceVariant(
        price_key=key,
        model_sha256=HELD_SHA,
        library_file=priced_file(**fields),
        slice=SliceQuoteRequest(
            printer="p-1", filament_plan=FilamentPlan(ids="sb"), choices=CHOICES
        ),
        slot_filaments=[shop.pla, shop.petg],
        currency="USD",
        slot_presets={1: TYPE_PRESETS[PLA_TYPE], 3: TYPE_PRESETS[PETG_TYPE]},
        slot_colours={1: "#FF0000", 3: "#00FF00"},
        variant_key="9" * 64,
    )


async def test_a_file_variant_slices_its_held_copy_once_and_reads_no_library(
    shop: Shop, prices: SlicePrices, content: ContentStore
) -> None:
    """#6710 §7.3: `start_library` once, with the slot presets, the slot colours and the
    held copy, which answers the pinned bytes. The run has no library to read: a file
    gone from the library, or holding other bytes, gives this same row. The middle slot
    prints no part, so it is never asked to read grams."""
    await hold(content)
    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 0.0, 5.0])})
    run = replace(slice_run(shop, prices, slicer), content=content)
    row = await price_variant(run, file_variant(shop))
    assert row.status == "done", row.failure
    assert slicer.subjects == [f"storefront_price:{'f' * 64} file {FILE_ID}"]
    [start] = slicer.library_starts
    assert start["slot_presets"] == {1: TYPE_PRESETS[PLA_TYPE], 3: TYPE_PRESETS[PETG_TYPE]}
    assert start["slot_colours"] == {1: "#FF0000", 3: "#00FF00"}
    held = start["held"]
    assert held is not None and held.file == priced_file()
    async with aclosing(held.download_library_file(FILE_ID)) as chunks:
        assert b"".join([chunk async for chunk in chunks]) == HELD
    assert row.model_sha256 == HELD_SHA
    assert row.grams == {"0": "10.0", "2": "5.0"}
    # #6921: the done row keeps the variant key it was claimed for.
    assert row.variant_key == "9" * 64
    assert row.slot_filaments == [shop.pla, shop.petg]
    assert row.output_id is None


OTHER_SHA = "d" * 64


def last_row(
    pool: PgPool,
    key: str,
    listing: str | None,
    filaments: list[str],
    *,
    setup: str | None = None,
    sha: str = HELD_SHA,
    minutes: int = 0,
    template: bool = False,
) -> str:
    """A ``done`` row of ``listing``, ``minutes`` old; with ``template`` a template's."""
    price_key = key * 64
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO storefront_slice_prices (price_key, model_sha256, status, currency,"
            " unit_price_minor, total_minor, lines, listing_id, slot_filaments, setup_key,"
            " variant_key, output_id, finished_at)"
            " VALUES (%s, %s, 'done', 'USD', 100, 100, '[]', %s, %s, %s, %s, %s,"
            " clock_timestamp() - make_interval(mins => %s))",
            (
                price_key,
                sha,
                listing,
                Jsonb(filaments),
                setup,
                "7" * 64 if template else None,
                "out-1" if template else None,
                minutes,
            ),
        )
    return price_key


def last_listings(pool: PgPool, *published: str) -> None:
    with pool.connection() as conn:
        for listing in published:
            conn.execute(
                "INSERT INTO storefront_listings (id, source, title, position, published,"
                " snapshot, library_file) VALUES (%s, 'library_file', 'Clip', 0, true, '{}',"
                " '{}')",
                (listing,),
            )


def last(prices: SlicePrices, listing: str = "mine", setup: str = "s-1") -> str | None:
    """The listing's last price for choices of the red and blue PLA."""
    row = prices.listing_last(listing, HELD_SHA, filaments={"red", "blue"}, setup_key=setup)
    return None if row is None else row.price_key


def test_a_listings_last_price_is_its_newest_of_the_chosen_material(
    prices: SlicePrices, pool: PgPool
) -> None:
    """#7523: the listing's own rows, of any setup and any material, the newest whose
    filaments are all of the chosen material first."""
    last_listings(pool, "mine")
    assert last(prices) is None
    petg = last_row(pool, "a", "mine", ["petg", "petg"], setup="s-9")
    assert last(prices) == petg
    old = last_row(pool, "b", "mine", ["red", "red"], setup="s-9", minutes=20)
    assert last(prices) == old
    newer = last_row(pool, "c", "mine", ["red", "blue"], setup="s-1", minutes=10)
    assert last(prices) == newer
    # One slot of another material is another print.
    last_row(pool, "d", "mine", ["red", "petg"], setup="s-1")
    assert last(prices) == newer
    # A row from before the column is the listing's own all the same.
    before = last_row(pool, "e", "mine", ["blue", "blue"], minutes=5)
    assert last(prices) == before


def test_a_templates_row_of_the_same_bytes_is_no_files_last_price(
    prices: SlicePrices, pool: PgPool
) -> None:
    """#7523: a listing that sold a template's render of the same bytes keeps that row."""
    last_listings(pool, "mine", "other")
    last_row(pool, "a", "mine", ["red", "red"], template=True)
    last_row(pool, "b", "other", ["red", "red"], template=True)
    assert last(prices) is None


def test_another_listings_last_price_needs_the_material_and_the_setup(
    prices: SlicePrices, pool: PgPool
) -> None:
    """#7523: another published listing's row only for the chosen material and the same
    setup; a row with no setup key (from before the column) matches no other listing."""
    last_listings(pool, "mine", "other")
    last_row(pool, "a", "other", ["petg", "petg"], setup="s-1")
    last_row(pool, "b", "other", ["red", "red"], setup="s-2")
    last_row(pool, "c", "other", ["red", "red"])
    last_row(pool, "d", "other", ["red", "red"], setup="s-1", sha=OTHER_SHA)
    last_row(pool, "e", "unpublished", ["red", "red"], setup="s-1")
    assert last(prices) is None
    theirs = last_row(pool, "f", "other", ["red", "red"], setup="s-1", minutes=30)
    assert last(prices) == theirs
    assert last(prices, setup="s-2") == "b" * 64
    # The listing's own row comes first, of whatever material.
    own = last_row(pool, "0", "mine", ["petg", "petg"], setup="s-1", minutes=60)
    assert last(prices) == own


def test_a_row_of_no_listing_is_no_listings_last_price(prices: SlicePrices, pool: PgPool) -> None:
    """#7530: a done row from before #6710 PR 3 names no listing. It is never a listing's
    last price, of the chosen material and setup or not."""
    last_listings(pool, "mine", "other")
    last_row(pool, "a", None, ["red", "red"], setup="s-1")
    last_row(pool, "b", None, ["red", "blue"])
    assert last(prices) is None


async def test_a_files_row_records_its_setup_and_a_templates_none(
    shop: Shop, prices: SlicePrices, content: ContentStore
) -> None:
    """#7523: the setup key is the subject's kind and the slice request; filaments, rates
    and the book are not in it."""
    await hold(content)
    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 0.0, 5.0])})
    run = replace(slice_run(shop, prices, slicer), content=content)
    variant = file_variant(shop)
    row = await price_variant(run, variant)
    assert row.status == "done" and row.setup_key == setup_key(variant)
    template = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 5.0])})
    other = await price_variant(slice_run(shop, prices, template), variant_of(shop))
    assert other.status == "done" and other.setup_key is None


def test_the_setup_key_moves_with_the_request_and_the_subject_only(shop: Shop) -> None:
    variant = file_variant(shop)
    key = setup_key(variant)
    assert setup_key(variant.model_copy(update={"slot_filaments": [shop.petg]})) == key
    assert setup_key(variant.model_copy(update={"machine_rate": RATE})) == key
    assert setup_key(file_variant(shop, id=42)) == key
    assert setup_key(file_variant(shop, library="native")) != key
    walls = variant.slice.model_copy(update={"process_overrides": {"wall_loops": "5"}})
    assert setup_key(variant.model_copy(update={"slice": walls})) != key
    wide = variant.slice.model_copy(
        update={"choices": PrintChoices(nozzles=[NozzleChoice(size="0.6")])}
    )
    assert setup_key(variant.model_copy(update={"slice": wide})) != key
    assert setup_key(variant_of(shop)) != key


async def test_a_used_slot_at_zero_grams_gives_no_price(
    shop: Shop, prices: SlicePrices, content: ContentStore
) -> None:
    """#6710 §7.3 step 4: a used slot no plate reads grams for was not sliced."""
    await hold(content)
    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0])})
    run = replace(slice_run(shop, prices, slicer), content=content)
    row = await price_variant(run, file_variant(shop, "e" * 64))
    assert row.status == "failed"
    assert row.failure is not None and "slot 2" in row.failure


async def test_a_held_copy_missing_from_the_store_is_file_gone_and_never_slices(
    shop: Shop, prices: SlicePrices, content: ContentStore
) -> None:
    slicer = FakeSlicer({1: sliced_plate(1, 3600, [10.0, 0.0, 5.0])})
    run = replace(slice_run(shop, prices, slicer), content=content)
    row = await price_variant(run, file_variant(shop, "d" * 64))
    assert (row.status, row.failure) == ("failed", FILE_GONE)
    assert slicer.subjects == []
