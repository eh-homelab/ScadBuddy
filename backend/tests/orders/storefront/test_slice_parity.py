"""#6574: a storefront variant is sliced with the choices and presets a back-office quote
of spools of the same filament types on the same printer gets, and priced the same.

The back office's side is the print dialog's own read (``choices_for``, over Bambuddy's
recorded answers: an H2C with a High Flow 0.4 mm nozzle) and the quote it opens on, with
an SB spool per slot, each of a filament type with no presets of its own (so it takes its
type's, `filament/presets.py` `effective_presets`). The storefront's side reads the same
printer (``printer_opening``) and plans the variant (``plan_variant``): no spool, each
slot with its material's type's presets. On prod before #6574 the storefront sliced at
standard flow with one tray in every slot (5.3 g, $1.00) where the back office sliced at
High Flow with a filament per slot (25.2 g, $4.00).

#6710 (the pricing owner's constraint 4): a file listing's library file is sliced and
priced as a back-office quote of a `library_file` item of that file is, with the same
remembered setup, through the same `start_library`; only the bytes' source differs (the
listing's held copy), and they are the same bytes."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
import respx

from scadbuddy.bambuddy.choices import choices_for, printer_opening
from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.filaments import (
    FilamentOptions,
    FilamentPlan,
    SlotNeed,
    SpoolOption,
)
from scadbuddy.bambuddy.models import SlotChoice
from scadbuddy.bambuddy.output_reader import TemplateSettings
from scadbuddy.bambuddy.part_settings import PartSettings
from scadbuddy.bambuddy.preview import PreviewStarted
from scadbuddy.bambuddy.resolver import resolve
from scadbuddy.filament.models import EffectivePreset
from scadbuddy.library.settings_store import ModelPrintChoices, StoredSettings
from scadbuddy.orders.models import LibraryFileRef, MachineRateBreakdown
from scadbuddy.orders.quote import (
    QuoteRun,
    SliceQuoteRequest,
    SlotColours,
    SlotPresets,
    quote_run,
)
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.held_file import HeldFile, PricedFile
from scadbuddy.orders.storefront.slice_price import (
    SlicePrices,
    SliceRun,
    plan_variant,
    price_variant,
)
from scadbuddy.orders.storefront.slice_profile import StorefrontSliceProfile
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobScope
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from tests.bambuddy.conftest import BASE_URL
from tests.bambuddy.test_choices_reads import API, _filament_routes, _ok, _source
from tests.bambuddy.test_resolver import recorded
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
from tests.orders.test_quote import Shop as QuoteShop
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres

PLA_TYPE = UUID("00000000-0000-4000-8000-0000000000a1")
PETG_TYPE = UUID("00000000-0000-4000-8000-0000000000a2")


def _type_preset(filament: str) -> EffectivePreset:
    """A type's preset row for the H2C and a 0.4 mm nozzle, as a spool of the type with
    no row of its own reads it (``overridden`` false)."""
    return EffectivePreset(
        printer_model="H2C",
        nozzle_diameter="0.4",
        slicer_filament=filament,
        slicer_filament_name=None,
        overridden=False,
    )


#: Bambu PLA Basic and Bambu PETG HF, each in the recorded catalogue for the H2C 0.4.
TYPE_PRESETS = {PLA_TYPE: [_type_preset("GFSA00_22")], PETG_TYPE: [_type_preset("GFSG02_25")]}
SHA = "e" * 64
#: The registry printer: a registry id is a UUID.
P1 = "00000000-0000-4000-8000-000000000001"
RATE = MachineRateBreakdown(
    printer=P1,
    printer_name="H2C",
    source="profile",
    rate_per_hour="0.89",
    currency="USD",
    depreciation_per_hour="0.89",
    intervals="typed",
)
SETTINGS = StoredSettings(
    bambuddy_url=BASE_URL,
    bambuddy_api_key="s3cret",
    storefront_slice_profile=StorefrontSliceProfile(
        printer=P1, materials={"pla": PLA_TYPE, "petg": PETG_TYPE}
    ),
)
#: The render's colour per slot, which the visitor picked.
RED, GREEN = "#FF0000", "#00FF00"


class Recording(FakeSlicer):
    """The quote fakes' slicer, which keeps each request it was asked to slice."""

    def __init__(self, files: dict[int, bytes]) -> None:
        super().__init__(files)
        self.requests: list[SliceQuoteRequest] = []

    async def start_library(
        self,
        file_id: int,
        request: SliceQuoteRequest,
        subject: str,
        *,
        slot_presets: SlotPresets | None = None,
        slot_colours: SlotColours | None = None,
        held: HeldFile | None = None,
    ) -> list[PreviewStarted]:
        self.requests.append(request)
        return await super().start_library(
            file_id,
            request,
            subject,
            slot_presets=slot_presets,
            slot_colours=slot_colours,
            held=held,
        )

    async def start(
        self,
        output_id: str,
        request: SliceQuoteRequest,
        subject: str,
        *,
        slot_presets: SlotPresets | None = None,
        template_settings: TemplateSettings | None = None,
    ) -> list[PreviewStarted]:
        self.requests.append(request)
        return await super().start(
            output_id,
            request,
            subject,
            slot_presets=slot_presets,
            template_settings=template_settings,
        )


def _printers() -> Any:
    """The registry: printer ``P1``, which links Bambuddy printer 1 (no LAN route)."""
    row = SimpleNamespace(id=P1, bambuddy_printer_id=1)
    return SimpleNamespace(store=SimpleNamespace(get=lambda _id: row), reports=None)


def _bambuddy_routes() -> None:
    _filament_routes()
    respx.get(f"{API}/printers/").mock(return_value=_ok("printers.json"))
    respx.get(f"{API}/printers/1/status").mock(return_value=_ok("printer-status-rack.json"))
    respx.get(f"{API}/slicer/presets").mock(return_value=_ok("slicer-presets-h2c.json"))
    respx.get(f"{API}/local-presets/").mock(return_value=_ok("local-presets.json"))


@respx.mock
async def test_the_storefront_slices_and_prices_as_the_back_office_quote(
    rows: OrderStore, pool: PgPool, tmp_path: Path
) -> None:
    _bambuddy_routes()
    shop = Shop(rows, BlobRefs(pool))
    item = shop.get_item()
    assert item.reference is not None
    pla = rows.filament(shop.pla)
    petg = rows.filament(shop.petg)
    assert pla is not None and petg is not None

    # The back office: the dialog opens on the printer, and the quote names an SB spool
    # per slot: #7 of the PLA type, #8 of the PETG type.
    async with BambuddyClient(BambuddyConfig(base_url=BASE_URL, api_key="s3cret")) as client:
        view = await choices_for(
            client,
            _source(),
            SETTINGS,
            remembered=SETTINGS.model_print_choices.get(item.reference.slug),
            printer_id=1,
            cache=tmp_path / "cache",
            catalogue_key=None,
        )
    assert view.opening_choices is not None
    back_office = SliceQuoteRequest(
        printer=P1,
        filament_plan=FilamentPlan(
            slots=[SlotChoice(slot_id=1, spool_id=7), SlotChoice(slot_id=2, spool_id=8)],
            ids="sb",
        ),
        choices=view.opening_choices,
        filaments={0: shop.pla, 1: shop.petg},
    )

    # The storefront: the same printer, read as the dialog reads it.
    opening = await printer_opening(SETTINGS, _printers(), P1)
    assert opening is not None
    planned = plan_variant(
        settings=SETTINGS,
        reference=item.reference,
        model_sha256=SHA,
        filaments=[pla, petg],
        currency="USD",
        policy=POLICY,
        machine_rate=RATE,
        printer=opening,
        type_presets=TYPE_PRESETS,
    )
    assert planned is not None
    storefront = planned.variant.slice

    # The choices are the dialog's: High Flow from the printer's status and the plate it
    # picks. The rest of the request is the same but the spools.
    assert storefront.choices == back_office.choices
    assert [(n.size, n.flow) for n in storefront.choices.nozzles] == [("0.4", "high_flow")] * 2
    unspooled = {"filament_plan", "filaments"}
    assert storefront.model_dump(exclude=unspooled) == back_office.model_dump(exclude=unspooled)

    # What the slicer gets is the same: each slot the preset a spool of its type takes,
    # in the slot's colour, so every slot keeps its own colour and the purges are real.
    options = FilamentOptions(
        library_file_id=None,
        slots=[SlotNeed(slot_id=1, colour=RED), SlotNeed(slot_id=2, colour=GREEN)],
        spools=[
            SpoolOption(spool_id=7, sb_number=7, material="PLA", colour=RED),
            SpoolOption(spool_id=8, sb_number=8, material="PETG", colour=GREEN),
        ],
    )
    catalogue = recorded()
    quoted = resolve(
        options,
        back_office.filament_plan,
        back_office.choices,
        catalogue,
        {7: TYPE_PRESETS[PLA_TYPE], 8: TYPE_PRESETS[PETG_TYPE]},
    )
    priced = resolve(
        options.model_copy(update={"spools": []}),
        storefront.filament_plan,
        storefront.choices,
        catalogue,
        {},
        slot_presets=planned.variant.slot_presets,
    )
    assert quoted.errors == [] and priced.errors == []
    assert priced.filament_presets == quoted.filament_presets
    assert [ref.id for ref in priced.filament_presets] == ["GFSA00_22", "GFSG02_25"]
    assert priced.filament_colours == quoted.filament_colours == [RED, GREEN]

    # And so is the price, to the cent.
    files = {1: sliced_plate(1, 5400, [12.5, 3.0]), 2: sliced_plate(2, 1800, [10.0, 2.0])}
    quoted_by = Recording(files)
    run = replace(
        run_of(shop, tmp_path, files, machine_rate=FakeMachineRate(RATE)), slicer=quoted_by
    )
    quote = await quote_run(run, shop.order.id, shop.item.id, back_office, BY)

    priced_by = Recording(files)

    async def create_output(body: dict[str, object]) -> dict[str, object]:
        return {"id": "out-sf", "colors": ["#ff0000", "#00ff00"]}

    async def rendered_sha256(_rendered: object) -> str | None:
        return SHA

    row = await price_variant(
        SliceRun(
            prices=SlicePrices(pool),
            filament=rows.filament,
            book=Book(),
            outputs=FakeOutputs(),
            renders=FakeRenders(),
            create_output=create_output,
            slicer=priced_by,
            rendered_sha256=rendered_sha256,
            follow_every=0.0,
        ),
        planned.variant,
    )
    assert priced_by.slot_presets == [planned.variant.slot_presets]
    assert row.status == "done"
    assert (row.total_minor, row.unit_price_minor, row.unit_cost_minor) == (
        quote.total_minor,
        quote.unit_price_minor,
        quote.unit_cost_minor,
    )
    assert row.lines == quote.lines


@respx.mock
async def test_a_printer_bambuddy_cannot_read_has_no_status() -> None:
    """#6574: Bambuddy's status read fails, so the storefront has no status to open on
    (and gives no price), never the standard-flow defaults."""
    respx.get(f"{API}/printers/1/status").respond(503)
    respx.get(f"{API}/archives/").mock(return_value=_ok("archives.json"))
    opening = await printer_opening(SETTINGS, _printers(), P1)
    assert opening is not None and opening.status is None
    gone = SimpleNamespace(store=SimpleNamespace(get=lambda _id: None), reports=None)
    assert await printer_opening(SETTINGS, gone, P1) is None  # type: ignore[arg-type]


# -- a file listing's file (#6710, constraint 4) -----------------------------------------

FILE_ID = 42
#: The library file's bytes, which the listing's held copy holds too.
FILE_BYTES = b"library file 42, a two-colour 3MF"
FILE_SHA = hashlib.sha256(FILE_BYTES).hexdigest()
#: The file's own slot colours, which the author chose; the customer picks others.
AUTHOR = {1: "#FFFFFF", 2: "#000000"}
#: What the dialog remembers for the file: a process override, a print sequence and a
#: setting per part, which it seeds into the slice (`usePrintChoices.ts` `seedDialog`).
REMEMBERED = ModelPrintChoices(
    process_overrides={"enable_support": "1"},
    print_sequence="by object",
    part_settings={2: PartSettings(wall_loops=4)},
)
FILE_SETTINGS = SETTINGS.model_copy(
    update={"model_print_choices": {f"library:{FILE_ID}": REMEMBERED}}
)


class Library:
    """The library that serves: file 42, in the author's colours."""

    async def exists(self, file_id: int) -> bool:
        return file_id == FILE_ID

    async def colours(self, file_id: int) -> dict[int, str]:
        return dict(AUTHOR)


@pytest.fixture
async def content(tmp_path: Path, pool: PgPool) -> AsyncIterator[ContentStore]:
    store = local_content(tmp_path / "remote", pool)
    try:
        yield store
    finally:
        await store.aclose()


def _library_run(shop: QuoteShop, tmp_path: Path, slicer: Recording) -> QuoteRun:
    async def create_output(body: dict[str, object]) -> dict[str, object]:
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
        library=Library(),
        machine_rate=FakeMachineRate(RATE),
    )


async def _read(held: HeldFile) -> bytes:
    async with aclosing(held.download_library_file(FILE_ID)) as chunks:
        return b"".join([chunk async for chunk in chunks])


def _presets(refs: Sequence[object]) -> list[str]:
    return [ref.id for ref in refs]  # type: ignore[attr-defined]


@respx.mock
async def test_a_file_listing_slices_and_prices_as_the_back_office_quote_of_its_file(
    rows: OrderStore, pool: PgPool, tmp_path: Path, content: ContentStore
) -> None:
    _bambuddy_routes()
    shop = QuoteShop(rows, BlobRefs(pool))
    item = shop.lifecycle.add_library_item(
        shop.order.id,
        BY,
        LibraryFileRef(id=FILE_ID, filename="keychain.3mf", library="bambuddy"),
        quantity=1,
    )
    pla = rows.filament(shop.pla)
    petg = rows.filament(shop.petg)
    assert pla is not None and petg is not None
    remembered = FILE_SETTINGS.model_print_choices[f"library:{FILE_ID}"]

    # The back office: the dialog opens on the file and the printer, seeds the
    # remembered setup, and the quote names an SB spool per slot, of each filament's
    # colour.
    async with BambuddyClient(BambuddyConfig(base_url=BASE_URL, api_key="s3cret")) as client:
        view = await choices_for(
            client,
            _source(),
            FILE_SETTINGS,
            remembered=remembered,
            printer_id=1,
            cache=tmp_path / "cache",
            catalogue_key=None,
        )
    assert view.opening_choices is not None
    back_office = SliceQuoteRequest(
        printer=P1,
        filament_plan=FilamentPlan(
            slots=[SlotChoice(slot_id=1, spool_id=7), SlotChoice(slot_id=2, spool_id=8)],
            ids="sb",
        ),
        choices=view.opening_choices,
        filaments={0: shop.pla, 1: shop.petg},
        print_sequence=remembered.print_sequence,
        process_overrides=remembered.process_overrides,
        part_settings=remembered.part_settings,
    )

    # The storefront: the same printer, the file as its subject, its held copy in the
    # content store with the library file's bytes.
    key = f"listing-file-{FILE_SHA}"
    await content.put("media", FILE_BYTES, name="keychain.3mf", scope=BlobScope(), key=key)
    file = PricedFile(
        id=FILE_ID,
        library="bambuddy",
        filename="keychain.3mf",
        sha256=FILE_SHA,
        copy_key=key,
        file_type="3mf",
        size=len(FILE_BYTES),
        plates=[1, 2],
        used=[0, 1],
    )
    opening = await printer_opening(FILE_SETTINGS, _printers(), P1)
    assert opening is not None
    planned = plan_variant(
        settings=FILE_SETTINGS,
        library_file=file,
        filaments=[pla, petg],
        currency="USD",
        policy=POLICY,
        machine_rate=RATE,
        printer=opening,
        type_presets=TYPE_PRESETS,
    )
    assert planned is not None
    storefront = planned.variant.slice

    # The same request but the spools: the choices and the three seeded fields.
    assert storefront.choices == back_office.choices
    unspooled = {"filament_plan", "filaments"}
    assert storefront.model_dump(exclude=unspooled) == back_office.model_dump(exclude=unspooled)
    assert storefront.process_overrides == {"enable_support": "1"}

    # What the slicer gets is the same: the type's preset per slot, in the colour the
    # customer picked (the spool's in the back office), not the author's.
    options = FilamentOptions(
        library_file_id=FILE_ID,
        slots=[SlotNeed(slot_id=1, colour=AUTHOR[1]), SlotNeed(slot_id=2, colour=AUTHOR[2])],
        spools=[
            SpoolOption(spool_id=7, sb_number=7, material="PLA", colour=pla.colour),
            SpoolOption(spool_id=8, sb_number=8, material="PETG", colour=petg.colour),
        ],
    )
    catalogue = recorded()
    quoted = resolve(
        options,
        back_office.filament_plan,
        back_office.choices,
        catalogue,
        {7: TYPE_PRESETS[PLA_TYPE], 8: TYPE_PRESETS[PETG_TYPE]},
    )
    priced = resolve(
        options.model_copy(update={"spools": []}),
        storefront.filament_plan,
        storefront.choices,
        catalogue,
        {},
        slot_presets=planned.variant.slot_presets,
        slot_colours=planned.variant.slot_colours,
    )
    assert quoted.errors == [] and priced.errors == []
    assert _presets(priced.filament_presets) == _presets(quoted.filament_presets)
    assert _presets(priced.filament_presets) == ["GFSA00_22", "GFSG02_25"]
    assert priced.filament_colours == quoted.filament_colours == [RED, GREEN]

    # On a LAN printer the back office needs loaded trays (negative ids, measured on prod
    # 2026-11-21): trays of other colours with the type's preset chosen under Advanced
    # slice with the presets the storefront's slots get.
    trays = options.model_copy(
        update={
            "spools": [
                SpoolOption(spool_id=-1, material="PLA", colour="#EE1111"),
                SpoolOption(spool_id=-2, material="PETG", colour="#11EE11"),
            ]
        }
    )
    overridden = back_office.choices.model_copy(
        update={"filament_overrides": dict(enumerate(priced.filament_presets, start=1))}
    )
    on_trays = resolve(
        trays,
        FilamentPlan(
            slots=[SlotChoice(slot_id=1, spool_id=-1), SlotChoice(slot_id=2, spool_id=-2)]
        ),
        overridden,
        catalogue,
        {},
    )
    assert on_trays.errors == []
    assert _presets(on_trays.filament_presets) == _presets(priced.filament_presets)

    # And so is the price, to the cent, through `start_library` on both sides.
    files = {1: sliced_plate(1, 5400, [12.5, 3.0]), 2: sliced_plate(2, 1800, [10.0, 2.0])}
    quoted_by = Recording(files)
    quote = await quote_run(
        _library_run(shop, tmp_path, quoted_by), shop.order.id, item.id, back_office, BY
    )
    priced_by = Recording(files)
    row = await price_variant(
        SliceRun(
            prices=SlicePrices(pool),
            filament=rows.filament,
            book=Book(),
            outputs=FakeOutputs(),
            renders=FakeRenders(),
            create_output=lambda body: None,  # type: ignore[arg-type,return-value]
            slicer=priced_by,
            rendered_sha256=lambda rendered: None,  # type: ignore[arg-type,return-value]
            follow_every=0.0,
            content=content,
        ),
        planned.variant,
    )
    [office_start] = quoted_by.library_starts
    [store_start] = priced_by.library_starts
    assert quoted_by.subjects == [f"order_item:{item.id} file {FILE_ID}"]
    assert priced_by.subjects == [f"storefront_price:{planned.key} file {FILE_ID}"]
    assert office_start["held"] is None
    held = store_start["held"]
    assert held is not None
    # The bytes the storefront's slice reads are the library file's.
    assert hashlib.sha256(await _read(held)).hexdigest() == FILE_SHA
    assert (await held.library_file(FILE_ID)).file_hash == FILE_SHA
    assert store_start["slot_presets"] == planned.variant.slot_presets
    assert store_start["slot_colours"] == {1: RED, 2: GREEN}
    assert quoted_by.requests == [back_office]
    assert row.status == "done", row.failure
    assert (row.total_minor, row.unit_price_minor, row.unit_cost_minor) == (
        quote.total_minor,
        quote.unit_price_minor,
        quote.unit_cost_minor,
    )
    assert row.lines == quote.lines
