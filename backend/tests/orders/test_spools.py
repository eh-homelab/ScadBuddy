"""An item print's spool per slot (#4251, spec 2026-10-18 §5.4 "Spools", plan
2026-11-03 Ruling 4), and the price book's import from ScadBuddy's own spools (#4253,
#6262 Task 9, F28). Every spool here is an SB spool by its ``number``; the numbers
differ from every Bambuddy id the tests name, so a fill that read a Bambuddy id as an
SB number would fail. An opaque and a translucent spool of one hue are two filaments
(#6316)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
import psycopg
import pytest
import respx

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import SlotChoice, SpoolAssignment
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.models import EffectivePreset, FilamentInventorySpool, TrayPlace
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.orders.bambuddy import BambuddyIntegrations
from scadbuddy.orders.integrations import NOT_LINKED, LocalDensity, NotLinked, SpoolDensity
from scadbuddy.orders.models import Actor, Filament, FilamentInput, QuoteLine
from scadbuddy.orders.printing import quoted_setup
from scadbuddy.orders.spool_import import import_spools, spool_groups
from scadbuddy.orders.spools import (
    ShelfSpool,
    SpoolChoiceError,
    fill_plan,
    item_plan,
    record_rgba,
    shelf_spools,
)
from scadbuddy.orders.store import OrderStore, SpoolLink
from tests.orders.test_lifecycle import BY, Shop
from tests.support.sb_rows import sb_spool

NOW = datetime(2026, 11, 3, tzinfo=UTC)
#: A registry printer's id.
PRINTER = "printer-h2c"
#: A Bambuddy id an older plan names; no SB spool the fill should take has this number.
BB_RED = 7


def filament(colour: str = "#FF0000", brand: str | None = "Bambu", alpha: int = 255) -> Filament:
    return Filament(
        id="f" + colour,
        material="PLA",
        subtype="Basic",
        brand=brand,
        colour_name=None,
        colour=colour,
        alpha=alpha,
        cost_per_kg="20",
        density_g_cm3=None,
        archived_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def spool(spool_id: int, rgba: str | None = "#FF0000FF", **fields: object) -> ShelfSpool:
    return ShelfSpool.model_validate(
        {
            "id": spool_id,
            "material": "pla",
            "subtype": "basic",
            "brand": "bambu",
            "rgba": rgba,
            "remaining_g": 500.0,
            **fields,
        }
    )


def test_the_spool_loaded_on_the_printer_is_taken_first() -> None:
    spools = [
        spool(41, remaining_g=900),
        spool(42, loaded_printer=PRINTER, remaining_g=100),
    ]
    assert fill_plan({0: filament()}, spools, PRINTER, {}) == {1: 42}


def test_else_the_shelf_spool_with_most_left() -> None:
    spools = [
        spool(41, remaining_g=200),
        spool(42, remaining_g=700),
        spool(43, archived=True, remaining_g=999),
    ]
    assert fill_plan({0: filament()}, spools, PRINTER, {}) == {1: 42}


def test_a_spool_loaded_elsewhere_is_never_taken() -> None:
    spools = [spool(41, loaded_printer="another", remaining_g=900)]
    with pytest.raises(SpoolChoiceError) as raised:
        fill_plan({0: filament()}, spools, PRINTER, {})
    assert (raised.value.slug, raised.value.slots) == ("needs_spool_choice", [0])


def test_another_filament_or_a_name_only_spool_is_not_the_slots() -> None:
    spools = [spool(41, rgba="#00FF00FF"), spool(42, rgba=None), spool(43, brand="Elegoo")]
    with pytest.raises(SpoolChoiceError) as raised:
        fill_plan({0: filament(), 1: filament("#0000FF")}, spools, PRINTER, {})
    assert raised.value.slots == [0, 1]


def test_a_supplied_spool_must_be_the_slots_filament() -> None:
    spools = [spool(41), spool(42, rgba="#00FF00FF")]
    assert fill_plan({0: filament()}, spools, PRINTER, {1: 41}) == {1: 41}
    with pytest.raises(SpoolChoiceError) as raised:
        fill_plan({0: filament()}, spools, PRINTER, {1: 42})
    assert (raised.value.slug, raised.value.slots) == ("spool_not_filament", [0])
    # One loaded elsewhere may be named by a person.
    elsewhere = [spool(45, loaded_printer="another")]
    assert fill_plan({0: filament()}, elsewhere, PRINTER, {1: 45}) == {1: 45}


def test_an_old_link_reads_with_the_records_alpha() -> None:
    """Fix round 3: a 7-char ``spool_colour`` an old pod wrote reads with the record's
    alpha, as `20261116T0300Z` converts it: a clear record (alpha 0) linked to `#000000`,
    its linked spools archived, never takes a black opaque spool of its names."""
    clear = filament("#000000", alpha=0)
    links = {clear.id: SpoolLink("#000000", frozenset({99}))}
    with pytest.raises(SpoolChoiceError):
        fill_plan({0: clear}, [spool(41, rgba="#000000FF")], PRINTER, {}, links)
    assert fill_plan({0: clear}, [spool(42, rgba="#00000000")], PRINTER, {}, links) == {1: 42}


def test_a_colour_set_by_hand_still_finds_its_spools() -> None:
    """#6211 review: spools are matched as the import matches them, by the record's link
    (its spool numbers, else the spools' colour), so a hand-set colour never hides them."""
    clear = filament("#EEF3F4")
    links = {clear.id: SpoolLink("#000000", frozenset({44}))}
    by_rgb = [spool(41, rgba="#000000FF", remaining_g=300)]
    assert fill_plan({0: clear}, by_rgb, PRINTER, {}, links) == {1: 41}
    by_id = [spool(44, rgba="#111111FF")]
    assert fill_plan({0: clear}, by_id, PRINTER, {}, links) == {1: 44}
    assert fill_plan({0: clear}, by_rgb, PRINTER, {1: 41}, links) == {1: 41}
    # The spool's colour, not the record's hand-set one, and never another brand's.
    with pytest.raises(SpoolChoiceError):
        fill_plan({0: clear}, [spool(42, rgba="#EEF3F4FF")], PRINTER, {}, links)
    with pytest.raises(SpoolChoiceError):
        fill_plan({0: clear}, [spool(44, rgba="#000000FF", brand="Elegoo")], PRINTER, {}, links)
    # With no link, the record's own colour, as before.
    assert fill_plan({0: clear}, [spool(42, rgba="#EEF3F4FF")], PRINTER, {}) == {1: 42}


def test_a_slot_with_no_filament_is_left_as_given() -> None:
    assert fill_plan({}, [spool(41)], PRINTER, {2: 49}) == {2: 49}


def test_a_spool_matches_a_record_of_its_alpha_only() -> None:
    """#6316 reader 3: a translucent spool is never an opaque record's, by its link or by
    colour, and the other way round."""
    assert record_rgba(filament(alpha=0x80)) == "#FF000080"
    opaque, clear = filament(), filament(alpha=0x80)
    translucent = [spool(41, rgba="#FF000080")]
    with pytest.raises(SpoolChoiceError):
        fill_plan({0: opaque}, translucent, PRINTER, {})
    assert fill_plan({0: clear}, translucent, PRINTER, {}) == {1: 41}
    with pytest.raises(SpoolChoiceError):
        fill_plan({0: clear}, [spool(42)], PRINTER, {})
    # Task 9 review ruling: a link by id is not a match by colour, so it takes its spools
    # whatever their alpha (prod's opaque stand-ins of clear spools, `20261114T1607Z`).
    linked = {opaque.id: SpoolLink("#000000", frozenset({41}))}
    assert fill_plan({0: opaque}, translucent, PRINTER, {}, linked) == {1: 41}


def test_a_spool_in_a_tray_of_a_printer_the_registry_lacks_is_not_on_the_shelf() -> None:
    """Bambuddy's assignment to a printer the registry does not hold still loads it."""
    with pytest.raises(SpoolChoiceError):
        fill_plan({0: filament()}, [spool(41, loaded=True)], PRINTER, {})


# -- the item print, on ScadBuddy's spools ---------------------------------------------


def row(
    number: int,
    rgba: str | None = "#FF0000FF",
    *,
    loaded: str | None = None,
    grams: float = 500.0,
    archived: bool = False,
    **names: Any,
) -> FilamentInventorySpool:
    """SB spool #``number`` of red Bambu PLA Basic, in a tray of registry printer
    ``loaded``, or on the shelf."""
    fields: dict[str, Any] = {"material": "PLA", "subtype": "Basic", "brand": "Bambu"} | names
    return FilamentInventorySpool(
        spool=sb_spool(number, rgba=rgba, grams=grams, archived=archived, **fields),
        loaded=None
        if loaded is None
        else TrayPlace(
            printer_id=loaded, bambuddy_printer_id=None, printer_name=None, ams_id=0, tray_id=0
        ),
    )


class Inventory:
    """`orders.spools.SpoolRows` over fixed rows, and the Bambuddy links of an older plan.
    A row with a ``bb_spool_id`` and no tray is placed by an assignment of that Bambuddy
    spool, as `FilamentInventory.rows` places it (``registry``: Bambuddy printer id to
    registry printer id)."""

    def __init__(
        self,
        *rows: FilamentInventorySpool,
        links: dict[int, int] | None = None,
        registry: dict[int, str] | None = None,
    ) -> None:
        self._rows = list(rows)
        self._links = links or {}
        self._registry = registry or {}
        self.reads = 0
        self.assignments: list[SpoolAssignment] = []

    def rows(
        self,
        *,
        include_archived: bool = False,
        bb_assignments: Sequence[SpoolAssignment] = (),
    ) -> list[FilamentInventorySpool]:
        self.reads += 1
        self.assignments = list(bb_assignments)
        assigned = {a.spool_id: a for a in bb_assignments}
        found = []
        for r in self._rows:
            if not include_archived and r.spool.archived_at is not None:
                continue
            a = assigned.get(r.bb_spool_id) if r.bb_spool_id is not None else None
            if r.loaded is None and a is not None:
                place = TrayPlace(
                    printer_id=self._registry.get(a.printer_id),
                    bambuddy_printer_id=a.printer_id,
                    printer_name=a.printer_name,
                    ams_id=a.ams_id,
                    tray_id=a.tray_id,
                )
                r = r.model_copy(update={"loaded": place})
            found.append(r)
        return found

    def bb_to_sb(self, ids: Iterable[int]) -> dict[int, int]:
        return {i: self._links[i] for i in ids if i in self._links}


def test_shelf_spools_are_the_sb_spools_by_number() -> None:
    [shelf] = shelf_spools([row(41, "#FFFFFF80", loaded=PRINTER, grams=12.5)])
    assert (shelf.id, shelf.rgba, shelf.loaded_printer, shelf.remaining_g) == (
        41,
        "#FFFFFF80",
        PRINTER,
        12.5,
    )
    assert (shelf.material, shelf.subtype, shelf.brand) == ("PLA", "Basic", "Bambu")


@pytest.fixture(name="shop")
def _shop(rows: OrderStore) -> Shop:
    return Shop(rows)


def priced_item(shop: Shop, alpha: int = 255) -> str:
    """An item whose colour slot 0 is red Bambu PLA Basic of ``alpha``."""
    record = shop.rows.insert_filament(
        FilamentInput(
            material="PLA",
            subtype="Basic",
            brand="Bambu",
            colour="#FF0000",
            alpha=alpha,
            cost_per_kg="20",
        )
    )
    order = shop.order()
    item = shop.item(order)
    shop.lifecycle.update_item(order.id, item.id, BY, filaments={0: record.id})
    return item.id


def _slots(plan: FilamentPlan | None) -> list[tuple[int, int]]:
    assert plan is not None
    assert plan.ids == "sb"
    return [(s.slot_id, s.spool_id) for s in plan.slots]


@pytest.mark.requires_postgres
async def test_the_item_print_fills_its_plan_from_its_filaments(shop: Shop) -> None:
    item_id = priced_item(shop)
    inventory = Inventory(row(41, loaded=PRINTER), row(42, grams=900))

    plan = await item_plan(
        shop.rows, inventory, NOT_LINKED, item_id, FilamentPlan(ids="sb"), PRINTER
    )

    assert _slots(plan) == [(1, 41)]
    assert plan is not None and plan.force_colour_match


class Linked(NotLinked):
    """The orders seam with a Bambuddy whose tray assignments are ``assignments``."""

    @property
    def linked(self) -> bool:
        return True

    def __init__(self, *assignments: SpoolAssignment) -> None:
        self._assignments = list(assignments)

    async def spool_assignments(self) -> list[SpoolAssignment]:
        return self._assignments


def assigned(bb_spool_id: int, bambuddy_printer: int) -> SpoolAssignment:
    return SpoolAssignment(
        id=1, spool_id=bb_spool_id, printer_id=bambuddy_printer, ams_id=0, tray_id=1
    )


def bb_linked(number: int, bb_spool_id: int, **fields: Any) -> FilamentInventorySpool:
    return row(number, **fields).model_copy(update={"bb_spool_id": bb_spool_id})


@pytest.mark.requires_postgres
async def test_a_spool_bambuddy_loads_in_another_printer_is_not_taken(shop: Shop) -> None:
    """Task 9 review ruling (Decision 3): on the Bambuddy path a spool no registry
    printer reports is where Bambuddy assigns its linked BB spool: one in printer 2 is
    not taken for a print on printer 1, whether or not the registry holds printer 2."""
    item_id = priced_item(shop)
    plan = FilamentPlan(ids="sb")
    for registry in ({2: "printer-2"}, {}):
        inventory = Inventory(bb_linked(41, 15, grams=900), registry=registry)
        with pytest.raises(ApiError) as raised:
            await item_plan(shop.rows, inventory, Linked(assigned(15, 2)), item_id, plan, PRINTER)
        assert raised.value.type.endswith("/needs_spool_choice")
        assert [a.spool_id for a in inventory.assignments] == [15]


@pytest.mark.requires_postgres
async def test_a_spool_bambuddy_loads_in_the_run_printer_is_preferred(shop: Shop) -> None:
    """On its own printer, the spool Bambuddy loads there is taken over a fuller shelf one."""
    item_id = priced_item(shop)
    inventory = Inventory(
        bb_linked(41, 15, grams=100), row(42, grams=900), registry={2: "printer-2"}
    )

    plan = await item_plan(
        shop.rows, inventory, Linked(assigned(15, 2)), item_id, FilamentPlan(ids="sb"), "printer-2"
    )

    assert _slots(plan) == [(1, 41)]


class Settings:
    """A settings store with a Bambuddy linked, or one whose read fails."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def load(self) -> StoredSettings:
        if self.error is not None:
            raise self.error
        return StoredSettings(bambuddy_url=BB, bambuddy_api_key="bb_test")


BB = "https://bambuddy.test"


def seam(settings: Settings | None = None) -> BambuddyIntegrations:
    """The real Bambuddy seam (its `read_assignments`), over ``respx``."""
    return BambuddyIntegrations(cast(SettingsStore, settings or Settings()))


@pytest.mark.requires_postgres
@respx.mock
async def test_a_refused_assignment_read_places_by_the_reports_alone(
    shop: Shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 2 (I1, M2, M4): an assignment read Bambuddy refuses, answers in no JSON
    or does not answer within `ASSIGNMENTS_TIMEOUT` places the spools by the reports
    alone: the spool Bambuddy would put in printer 2 is on the shelf, and is taken."""
    item_id = priced_item(shop)
    route = respx.get(f"{BB}/api/v1/inventory/assignments")
    for answer in (
        httpx.Response(403, json={"detail": "no"}),
        httpx.Response(200, text="<html>not json</html>"),
    ):
        route.mock(return_value=answer)
        inventory = Inventory(bb_linked(41, 15), registry={2: "printer-2"})
        plan = await item_plan(
            shop.rows, inventory, seam(), item_id, FilamentPlan(ids="sb"), PRINTER
        )
        assert _slots(plan) == [(1, 41)], answer

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json=[])

    monkeypatch.setattr("scadbuddy.filament.bambuddy_pull.ASSIGNMENTS_TIMEOUT", 0.3)
    route.mock(side_effect=hang)
    inventory = Inventory(bb_linked(41, 15), registry={2: "printer-2"})
    started = time.monotonic()
    plan = await item_plan(shop.rows, inventory, seam(), item_id, FilamentPlan(ids="sb"), PRINTER)
    assert _slots(plan) == [(1, 41)]
    assert time.monotonic() - started < 10  # far under the 30 s hang


@pytest.mark.requires_postgres
async def test_a_settings_read_that_fails_is_a_503(shop: Shop) -> None:
    """M1: the seam's settings read failing is a 503, as the spools' own read is."""
    item_id = priced_item(shop)
    down = Settings(psycopg.OperationalError("the database is not answering"))
    with pytest.raises(ApiError) as raised:
        await item_plan(shop.rows, Inventory(row(41)), seam(down), item_id, FilamentPlan(), PRINTER)
    assert raised.value.status == 503


@pytest.mark.requires_postgres
async def test_item_print_needs_no_bambuddy(shop: Shop) -> None:
    """The fill reads ScadBuddy's spools only: no seam, no Bambuddy URL."""
    item_id = priced_item(shop)
    inventory = Inventory(row(42, grams=900))

    assert _slots(
        await item_plan(shop.rows, inventory, NOT_LINKED, item_id, FilamentPlan(), None)
    ) == [(1, 42)]
    assert inventory.reads == 1


@pytest.mark.requires_postgres
async def test_item_print_fills_the_spool_of_its_alpha(shop: Shop) -> None:
    """#6316 reader 3: a translucent red loaded on the printer is not the opaque red
    record's; the opaque red on the shelf is. A translucent record takes the other."""
    opaque_item = priced_item(shop)
    inventory = Inventory(
        row(41, "#FF000080", loaded=PRINTER, grams=900), row(42, "#FF0000FF", grams=100)
    )

    plan = await item_plan(
        shop.rows, inventory, NOT_LINKED, opaque_item, FilamentPlan(ids="sb"), PRINTER
    )
    assert _slots(plan) == [(1, 42)]

    clear_item = priced_item(shop, alpha=0x80)
    plan = await item_plan(
        shop.rows, inventory, NOT_LINKED, clear_item, FilamentPlan(ids="sb"), PRINTER
    )
    assert _slots(plan) == [(1, 41)]


@pytest.mark.requires_postgres
async def test_the_item_print_keeps_force_colour_match_off_when_asked(shop: Shop) -> None:
    item_id = priced_item(shop)
    inventory = Inventory(row(41, loaded=PRINTER), row(42, grams=900))

    plan = await item_plan(
        shop.rows,
        inventory,
        NOT_LINKED,
        item_id,
        FilamentPlan(force_colour_match=False, ids="sb"),
        PRINTER,
    )

    assert plan is not None
    assert not plan.force_colour_match


@pytest.mark.requires_postgres
async def test_the_item_print_refuses_with_no_spool(shop: Shop) -> None:
    item_id = priced_item(shop)
    with pytest.raises(ApiError) as raised:
        await item_plan(
            shop.rows, Inventory(), NOT_LINKED, item_id, FilamentPlan(ids="sb"), PRINTER
        )
    assert raised.value.status == 409
    assert raised.value.type.endswith("/needs_spool_choice")


@pytest.mark.requires_postgres
async def test_a_supplied_spool_is_an_sb_number_and_an_older_plan_is_mapped(
    shop: Shop,
) -> None:
    """A supplied SB number is kept when it is of the slot's filament; an older plan's
    Bambuddy id (``ids`` ``"bb"``) is its linked SB spool's number, never SB spool #7."""
    item_id = priced_item(shop)
    decoy = row(BB_RED, "#00FF00FF")
    inventory = Inventory(row(43), decoy, row(41, loaded=PRINTER), links={BB_RED: 43})
    asked = [SlotChoice(slot_id=1, spool_id=43)]

    sb = FilamentPlan(slots=asked, ids="sb")
    assert _slots(await item_plan(shop.rows, inventory, NOT_LINKED, item_id, sb, PRINTER)) == [
        (1, 43)
    ]
    bb = FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=BB_RED)], ids="bb")
    assert _slots(await item_plan(shop.rows, inventory, NOT_LINKED, item_id, bb, PRINTER)) == [
        (1, 43)
    ]
    # A Bambuddy id with no link names no spool: the slot is filled.
    unlinked = FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=99)], ids="bb")
    assert _slots(
        await item_plan(shop.rows, inventory, NOT_LINKED, item_id, unlinked, PRINTER)
    ) == [(1, 41)]


@pytest.mark.requires_postgres
async def test_the_plan_a_stored_quote_names_is_kept_by_sb_number(shop: Shop) -> None:
    """A print that sends no plan prints its quote's (#4673), which Task 8 stored as SB
    numbers (``ids`` ``"sb"``): the shelf spool it names is kept over the loaded one."""
    item_id = priced_item(shop)
    line = QuoteLine(kind="manual", label="typed", quantity=1, unit_minor=100, amount_minor=100)
    with shop.rows.transaction() as conn:
        stored = shop.rows.insert_quote(
            conn,
            item_id,
            source="manual",
            lines=[line],
            unit_cost_minor=100,
            unit_price_minor=100,
            quantity=1,
            created_by=Actor(kind="browser"),
            choices={},
            filament_plan={
                "slots": [{"slot_id": 1, "spool_id": 44}],
                "force_colour_match": True,
                "ids": "sb",
            },
        )
    quote = shop.rows.quote(stored.id)
    assert quote is not None
    setup = quoted_setup(quote)
    assert setup is not None
    inventory = Inventory(row(41, loaded=PRINTER), row(44, grams=10), row(BB_RED, "#00FF00FF"))
    quoted = FilamentPlan.model_validate(setup["filament_plan"])

    plan = await item_plan(shop.rows, inventory, NOT_LINKED, item_id, quoted, PRINTER)

    assert _slots(plan) == [(1, 44)]


# -- the price book's import from SB spools (#6316 readers 1 and 2) --------------------


def priced(
    number: int, rgba: str, cost: str | None = "20", **fields: Any
) -> FilamentInventorySpool:
    made = row(number, rgba, **fields)
    effective = made.spool.effective.model_copy(
        update={"cost_per_kg": None if cost is None else Decimal(cost)}
    )
    return made.model_copy(update={"spool": made.spool.model_copy(update={"effective": effective})})


def book_row(store: OrderStore, alpha: int, *, first: bool = False) -> str:
    """A ``manual`` white Bambu PLA Basic record of ``alpha``; ``first`` gives it the
    lowest id, so a match by RGB alone would take it."""
    made = store.insert_filament(
        FilamentInput(
            material="PLA",
            subtype="Basic",
            brand="Bambu",
            colour="#FFFFFF",
            alpha=alpha,
            cost_per_kg="10",
        )
    )
    if not first:
        return made.id
    with store.transaction() as conn:
        conn.execute("UPDATE filaments SET id = '0-first' WHERE id = %s", (made.id,))
    return "0-first"


@pytest.mark.requires_postgres
def test_a_name_too_long_for_a_record_is_skipped(rows: OrderStore) -> None:
    """A group whose names a record cannot hold (over 100 characters) is skipped."""
    [imported] = _import(rows, [priced(41, "#FFFFFFFF", "24", material="X" * 101)])
    assert (imported["action"], imported["reason"]) == ("skipped", "too_long")


def test_groups_are_sb_spools_by_names_and_rgba() -> None:
    groups = spool_groups(
        [
            priced(41, "#FFFFFFFF", "20"),
            priced(43, "#FFFFFFFF", "26.5"),
            priced(42, "#FFFFFF80", "30"),
            priced(44, "#FFFFFFFF", None, archived=True),
        ]
    )
    assert [(g.colour, g.alpha, [n for n, _ in g.spools]) for g in groups] == [
        ("#FFFFFF", 0x80, [42]),
        ("#FFFFFF", 0xFF, [41, 43]),
    ]


def _with(
    made: FilamentInventorySpool,
    presets: Sequence[tuple[str, str, str]] = (),
    names: dict[str, str | None] | None = None,
    **effective: Any,
) -> FilamentInventorySpool:
    """``made`` with these effective values, catalog names and presets
    (``(printer model, nozzle, preset id)``)."""
    spool = made.spool.model_copy(
        update={
            "effective": made.spool.effective.model_copy(update=effective),
            "names": made.spool.names.model_copy(update=names or {}),
        }
    )
    rows = [
        EffectivePreset(
            printer_model=model,
            nozzle_diameter=nozzle,
            slicer_filament=preset,
            slicer_filament_name=None,
            overridden=True,
        )
        for model, nozzle, preset in presets
    ]
    return made.model_copy(update={"spool": spool, "presets": rows})


def test_a_group_takes_its_newest_spools_names_and_look() -> None:
    """The names, colour name, stops and effect are the newest spool's (the highest
    number); a spool known only by its colour's name is never an rgba one's (#2918)."""
    groups = spool_groups(
        [
            _with(row(41, "#FFFFFFFF"), colour_name="White"),
            _with(
                row(47, "#FFFFFFFF", brand="bambu ", subtype="basic"),
                names={"effect": "Dual Color"},
                colour_name="Jade White",
                extra_colours=["#FF0000FF", "#00FF0080"],
            ),
            _with(row(44, None), colour_name="White"),
        ]
    )
    by_numbers = {tuple(n for n, _ in g.spools): g for g in groups}
    assert set(by_numbers) == {(41, 47), (44,)}
    white = by_numbers[(41, 47)]
    assert (white.brand, white.subtype, white.colour_name, white.colour, white.rgba) == (
        "bambu",
        "basic",
        "Jade White",
        "#FFFFFF",
        "#FFFFFFFF",
    )
    assert (white.extra_colours, white.effect) == (["#FF0000FF", "#00FF0080"], "dual-color")
    assert (by_numbers[(44,)].colour, by_numbers[(44,)].rgba) == (None, None)


def test_a_group_carries_what_gives_its_density() -> None:
    """#6136: the newest spool's preset id that names one (its H2C 0.4 preset first), the
    Bambuddy local preset a numeric id names, and the newest spool's own density."""
    groups = spool_groups(
        [
            _with(row(41), [("H2C", "0.4", "GFA00")], density_g_cm3=Decimal("1.24")),
            _with(row(43), [("H2C", "0.6", "GFA05"), ("H2C", "0.4", "7")]),
            _with(row(45)),
            _with(row(51, "#000000FF"), [("X1C", "0.4", "GFL99")]),
        ],
        local={"7": LocalDensity(name="My PLA", density_g_cm3="1.25")},
    )
    by_numbers = {tuple(n for n, _ in g.spools): g for g in groups}
    red = by_numbers[(41, 43, 45)]
    assert (red.slicer_filament, red.local_preset) == (
        "7",
        LocalDensity(name="My PLA", density_g_cm3="1.25"),
    )
    assert red.spool_density == SpoolDensity(number=41, density_g_cm3="1.24")
    assert (by_numbers[(51,)].slicer_filament, by_numbers[(51,)].spool_density) == ("GFL99", None)


def _import(store: OrderStore, rows: Sequence[FilamentInventorySpool]) -> list[dict[str, Any]]:
    return [r.model_dump() for r in import_spools(store, spool_groups(rows)).rows]


@pytest.mark.requires_postgres
def test_import_by_colour_keeps_alpha_apart(rows: OrderStore) -> None:
    """#6316 reader 1: an opaque group links the opaque record of its RGB, never the
    translucent one, though the translucent one comes first."""
    clear = book_row(rows, 0x80, first=True)
    opaque = book_row(rows, 0xFF)

    [imported] = _import(rows, [priced(41, "#FFFFFFFF", "24")])

    assert (imported["action"], imported["filament_id"], imported["spool_ids"]) == (
        "updated",
        opaque,
        [41],
    )
    links = rows.spool_links([clear, opaque])
    assert links[clear].spool_ids == frozenset()
    assert links[opaque].spool_ids == frozenset({41})


@pytest.mark.requires_postgres
def test_an_old_link_finds_only_spools_of_the_records_alpha(rows: OrderStore) -> None:
    """Fix round 3: the import reads a 7-char link with the record's alpha too: a clear
    record linked to `#000000` (its spools archived) is not a black opaque group's."""
    clear = rows.insert_filament(
        FilamentInput(
            material="PLA",
            subtype="Basic",
            brand="Bambu",
            colour="#000000",
            alpha=0,
            cost_per_kg="10",
        ),
        colour_source="import",
        spool_colour="#000000",
        spool_ids=[99],
    )

    [imported] = _import(rows, [priced(41, "#000000FF", "24")])

    assert imported["action"] == "created"
    assert imported["filament_id"] != clear.id


@pytest.mark.requires_postgres
def test_an_opaque_group_is_not_colour_taken_by_a_translucent_row(rows: OrderStore) -> None:
    """#6316 reader 2: a translucent record of the RGB does not hold an opaque group's
    colour: the import makes the opaque record beside it."""
    clear = book_row(rows, 0x80)

    [imported] = _import(rows, [priced(41, "#FFFFFFFF", "24")])

    assert (imported["action"], imported["reason"]) == ("created", None)
    made = rows.filament(imported["filament_id"])
    assert made is not None and made.id != clear
    assert (made.colour, made.alpha, made.cost_per_kg) == ("#FFFFFF", 0xFF, "24")
    kept = rows.filament(clear)
    assert kept is not None and kept.cost_per_kg == "10"


@pytest.mark.requires_postgres
def test_the_newest_priced_spool_prices_the_group(rows: OrderStore) -> None:
    [imported] = _import(
        rows,
        [
            priced(45, "#00AE42FF", None),
            priced(43, "#00AE42FF", "26.5"),
            priced(41, "#00AE42FF", "24"),
        ],
    )
    assert (imported["action"], imported["cost_per_kg"], imported["spool_ids"]) == (
        "created",
        "26.5",
        [41, 43, 45],
    )
    assert (imported["min_cost_per_kg"], imported["max_cost_per_kg"]) == ("24", "26.5")
