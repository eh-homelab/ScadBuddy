"""#6574: a storefront variant is sliced with the request a back-office quote of the same
filaments on the same printer sends, and priced the same.

The back office's side is the print dialog's own read (``choices_for``, over Bambuddy's
recorded answers: an H2C with a High Flow 0.4 mm nozzle) and the quote it opens on, with
each slot's filament named as a quote names one that is not loaded: no spool, the
filament's preset as the slot's Advanced preset. The storefront's side reads the same
printer (``printer_opening``) and plans the variant (``plan_variant``). On prod before
#6574 the storefront sliced at standard flow with one tray in every slot (5.3 g, $1.00)
where the back office sliced at High Flow with a filament per slot (25.2 g, $4.00)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import respx

from scadbuddy.bambuddy.choices import choices_for, printer_opening
from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.preview import PreviewStarted
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.orders.models import MachineRateBreakdown
from scadbuddy.orders.operations import preview_request
from scadbuddy.orders.quote import SliceQuoteRequest, quote_run
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.slice_price import (
    SlicePrices,
    SliceRun,
    plan_variant,
    price_variant,
)
from scadbuddy.orders.storefront.slice_profile import StorefrontSliceProfile
from scadbuddy.store.refs import BlobRefs
from tests.bambuddy.conftest import BASE_URL
from tests.bambuddy.test_choices_reads import API, _filament_routes, _ok, _source
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

PLA = PresetRef(source="standard", id="Bambu PLA Basic @BBL H2C")
PETG = PresetRef(source="standard", id="Bambu PETG HF @BBL H2C")
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
        printer=P1, materials={"pla": PLA, "petg": PETG}
    ),
)


class Recording(FakeSlicer):
    """The quote fakes' slicer, which keeps each request it was asked to slice."""

    def __init__(self, files: dict[int, bytes]) -> None:
        super().__init__(files)
        self.requests: list[SliceQuoteRequest] = []

    async def start(
        self, output_id: str, request: SliceQuoteRequest, subject: str
    ) -> list[PreviewStarted]:
        self.requests.append(request)
        return await super().start(output_id, request, subject)


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

    # The back office: the dialog opens on the printer, and the quote names a filament
    # per slot.
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
        filament_plan=FilamentPlan(),
        choices=view.opening_choices.model_copy(update={"filament_overrides": {1: PLA, 2: PETG}}),
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
    )
    assert planned is not None
    storefront = planned.variant.slice

    # What the slicer gets is the same: High Flow from the printer's status, the
    # plate the dialog picks, and each slot's own preset with no spool.
    assert preview_request(storefront) == preview_request(back_office)
    assert [(n.size, n.flow) for n in storefront.choices.nozzles] == [("0.4", "high_flow")] * 2
    assert storefront.choices.filament_overrides == {1: PLA, 2: PETG}

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
    [sliced_quote] = quoted_by.requests
    [sliced_price] = priced_by.requests
    assert preview_request(sliced_price) == preview_request(sliced_quote)
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
