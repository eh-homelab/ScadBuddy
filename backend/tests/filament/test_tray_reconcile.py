"""An untagged tray reconciled with the printer's report and Bambuddy's assignment
(#6713): the spool placed at a tray must agree with what the printer reports there. When
it does not and Bambuddy assigns an agreeing linked spool to the tray, that spool moves in
and the stale one moves to no place; when neither agrees, nothing moves and the tray view
flags the tray (`FilamentInventory.reconcile_trays`, `FilamentInventory.view`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import respx

from scadbuddy.bambuddy.models import SpoolAssignment
from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.inventory import (
    RECONCILE_ACTOR,
    FilamentInventory,
    material_key,
    material_names,
    tray_agreement,
)
from scadbuddy.filament.models import (
    InventoryTray,
    Material,
    MaterialCreate,
    Spool,
    SpoolCreate,
    TrayMismatch,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.spools.models import SeenTray
from tests.conftest import PgPool
from tests.filament.pull_support import scheduled_pull
from tests.filament.test_bambuddy_pull import BB, _filament

pytestmark = pytest.mark.requires_postgres

#: The H2C's AMS-HT, "HT-A": one tray, id 0.
HT_A = 128
GREY = "#A7AEB2FF"
PINK = "#F772A4FF"
UUID_A = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
BB_PRINTER = 1


def _material(pool: PgPool, name: str) -> UUID:
    return next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == name
    )


def _spool(pool: PgPool, material: str, rgba: str) -> UUID:
    body = SpoolCreate(material_id=_material(pool, material), rgba=rgba)
    return FilamentSpoolStore(pool).create(body, actor="ann").id


def _printer(pool: PgPool) -> UUID:
    printer = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name, bambuddy_printer_id, lan_host)"
            " VALUES (%s, %s, 'H2C', %s, '10.0.0.9')",
            (printer, f"SN{printer.hex[:8]}", BB_PRINTER),
        )
    return printer


def _link_bb(pool: PgPool, spool_id: UUID, bb_spool_id: int) -> None:
    now = datetime.now(UTC)
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO filament_bb_links (spool_id, bb_spool_id, bb_updated_at, linked_at)"
            " VALUES (%s, %s, %s, %s)",
            (spool_id, bb_spool_id, now, now),
        )


def _tray(material: str, colour: str, **keys: Any) -> dict[str, Any]:
    return {
        "id": "0",
        "tray_type": material,
        "tray_color": colour.lstrip("#"),
        "state": 11,
        **keys,
    }


def _report(
    pool: PgPool, printer: UUID, tray: dict[str, Any], *, seen_at: datetime | None = None
) -> None:
    report = {"ams": {"ams": [{"id": str(HT_A), "tray": [tray]}]}}
    PrinterReportStore(pool).put(str(printer), report, seen_at or datetime.now(UTC))


def _seen(material: str, rgba: str, **keys: Any) -> SeenTray:
    return SeenTray(ams_id=HT_A, tray_id=0, material=material, rgba=rgba, **keys)


def _assigned(bb_spool_id: int, *, ams_id: int = HT_A, tray_id: int = 0) -> SpoolAssignment:
    return SpoolAssignment(
        id=bb_spool_id * 10,
        spool_id=bb_spool_id,
        printer_id=BB_PRINTER,
        printer_name="H2C",
        ams_id=ams_id,
        tray_id=tray_id,
    )


def _keys(pool: PgPool) -> dict[UUID, frozenset[str]]:
    with pool.connection() as conn:
        return material_names(conn)


def _get(pool: PgPool, spool_id: UUID) -> Spool:
    spool = FilamentSpoolStore(pool).get(spool_id)
    assert spool is not None
    return spool


def _moves(pool: PgPool, spool_id: UUID) -> list[tuple[str, dict[str, Any]]]:
    return [
        (e.actor, e.data)
        for e in FilamentSpoolStore(pool).events(spool_id)
        if e.kind == "moved" and e.actor == RECONCILE_ACTOR
    ]


def _view_tray(inventory: FilamentInventory, assignments: list[SpoolAssignment]) -> InventoryTray:
    (printer,) = inventory.view(bb_assignments=assignments).trays
    (tray,) = printer.trays
    return tray


def _prod_case(pool: PgPool) -> tuple[UUID, UUID, UUID, FilamentInventory]:
    """The H2C's HT-A on 2026-11-20 (#6713): the printer reports grey PETG with no tag,
    pink PLA #76 is placed there, and Bambuddy assigns its spool 121, linked to grey PETG
    #28."""
    printer = _printer(pool)
    pink = _spool(pool, "PLA", PINK)
    grey = _spool(pool, "PETG", GREY)
    _link_bb(pool, grey, 121)
    inventory = FilamentInventory(pool)
    inventory.link(printer, HT_A, 0, pink, _seen("PETG", GREY), actor="migration")
    _report(pool, printer, _tray("PETG", GREY))
    return printer, pink, grey, inventory


# -- what "agrees" means (R-6713-1) ---------------------------------------------------


def test_a_material_key_is_upper_case_alphanumerics() -> None:
    assert material_key("PETG-CF") == "PETGCF"
    assert material_key(" petg cf ") == "PETGCF"
    assert material_key("PLA") == "PLA"
    assert material_key(None) == ""
    assert material_key("PLA+") == "PLAPLUS"


def test_agreement_reads_the_material_key_and_the_colour_without_alpha(pg_pool: PgPool) -> None:
    grey = _get(pg_pool, _spool(pg_pool, "PETG", GREY))
    cf = _get(pg_pool, _spool(pg_pool, "PETG-CF", GREY))
    keys = _keys(pg_pool)
    assert tray_agreement(grey, _seen("PETG", GREY), keys) is True
    # Alpha is ignored; a near colour is within the tolerance.
    assert tray_agreement(grey, _seen("PETG", "#A7AEB200"), keys) is True
    assert tray_agreement(grey, _seen("PETG", "#A9AFB4FF"), keys) is True
    assert tray_agreement(grey, _seen("PETG", PINK), keys) is False
    assert tray_agreement(grey, _seen("PLA", GREY), keys) is False
    assert tray_agreement(cf, _seen("PETG-CF", GREY), keys) is True
    assert tray_agreement(cf, _seen("PETG", GREY), keys) is False
    # No colour on the tray: unknown, never a disagreement.
    assert tray_agreement(grey, SeenTray(ams_id=HT_A, tray_id=0, material="PETG"), keys) is None


def test_a_tray_agrees_by_any_key_of_the_spools_material(pg_pool: PgPool) -> None:
    """R-6706-23: a tray's ``tray_type`` is compared by catalog key with the material's
    name and every alias, so an alias or a merged loser's spelling agrees; ``PLA+`` is
    not ``PLA`` (R-6706-17)."""
    catalog: Any = CatalogStore(pg_pool)
    high_flow = catalog.create("materials", MaterialCreate(name="PETG High Flow"))
    catalog.set_aliases("materials", high_flow.id, ["PETG-HF"])
    # `ASA-Aero` merged into `ASA Aero`: the migration keeps no alias for a shared key.
    catalog.create("materials", MaterialCreate(name="ASA Aero"))
    hf = _get(pg_pool, _spool(pg_pool, "PETG High Flow", GREY))
    merged = _get(pg_pool, _spool(pg_pool, "ASA Aero", GREY))
    pla = _get(pg_pool, _spool(pg_pool, "PLA", GREY))
    keys = _keys(pg_pool)
    assert tray_agreement(hf, _seen("PETG-HF", GREY), keys) is True
    assert tray_agreement(hf, _seen("petg high-flow", GREY), keys) is True
    assert tray_agreement(hf, _seen("PETG", GREY), keys) is False
    assert tray_agreement(merged, _seen("ASA-Aero", GREY), keys) is True
    assert tray_agreement(pla, _seen("PLA+", GREY), keys) is False


def test_a_tray_reporting_an_alias_is_no_mismatch(pg_pool: PgPool) -> None:
    """R-6706-23: the placed spool's material holds the tray's spelling as an alias."""
    catalog: Any = CatalogStore(pg_pool)
    made = catalog.create("materials", MaterialCreate(name="PETG High Flow"))
    catalog.set_aliases("materials", made.id, ["PETG-HF"])
    printer = _printer(pg_pool)
    held = _spool(pg_pool, "PETG High Flow", GREY)
    inventory = FilamentInventory(pg_pool)
    inventory.link(printer, HT_A, 0, held, _seen("PETG-HF", GREY), actor="ann")
    _report(pg_pool, printer, _tray("PETG-HF", GREY))

    assert inventory.reconcile_trays([]) == []
    tray = _view_tray(inventory, [])
    assert (tray.spool_id, tray.mismatch) == (held, None)


# -- the moves (R-6713-3, R-6713-7) ---------------------------------------------------


def test_the_prod_case_moves_the_assigned_spool_in(pg_pool: PgPool) -> None:
    printer, pink, grey, inventory = _prod_case(pg_pool)
    tray_place = _get(pg_pool, pink).location_id
    assert tray_place is not None
    assert _view_tray(inventory, [_assigned(121)]).spool_id == pink

    moved = inventory.reconcile_trays([_assigned(121)])

    assert [(m.printer_id, m.ams_id, m.tray_id, m.removed, m.placed) for m in moved] == [
        (printer, HT_A, 0, pink, grey)
    ]
    assert _get(pg_pool, pink).location_id is None
    assert _get(pg_pool, grey).location_id == tray_place
    ((actor, out),) = _moves(pg_pool, pink)
    ((_, into),) = _moves(pg_pool, grey)
    assert actor == RECONCILE_ACTOR
    assert (out["from"], out["to"]) == (str(tray_place), None)
    assert (into["from"], into["to"]) == (None, str(tray_place))
    assert "PETG #A7AEB2FF" in out["reason"]
    assert out["reason"] == into["reason"]
    tray = _view_tray(inventory, [_assigned(121)])
    assert (tray.spool_id, tray.found_by, tray.mismatch) == (grey, "link", None)
    # A second pass finds the tray agreeing: nothing more moves.
    assert inventory.reconcile_trays([_assigned(121)]) == []
    assert len(_moves(pg_pool, grey)) == 1


def test_an_agreeing_holder_stays(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    held, other = _spool(pg_pool, "PETG", GREY), _spool(pg_pool, "PETG", GREY)
    _link_bb(pg_pool, other, 121)
    inventory = FilamentInventory(pg_pool)
    inventory.link(printer, HT_A, 0, held, _seen("PETG", GREY), actor="ann")
    _report(pg_pool, printer, _tray("PETG", GREY))

    assert inventory.reconcile_trays([_assigned(121)]) == []
    assert _moves(pg_pool, held) == [] and _moves(pg_pool, other) == []
    assert _view_tray(inventory, [_assigned(121)]).mismatch is None


@pytest.mark.parametrize("why", ["disagrees", "unlinked", "archived", "none"])
def test_neither_agreeing_moves_nothing_and_flags_the_tray(pg_pool: PgPool, why: str) -> None:
    """R-6713-4: the assignment names no linked, unarchived spool that agrees."""
    printer = _printer(pg_pool)
    pink = _spool(pg_pool, "PLA", PINK)
    other = _spool(
        pg_pool, "PLA" if why == "disagrees" else "PETG", PINK if why == "disagrees" else GREY
    )
    if why != "unlinked":
        _link_bb(pg_pool, other, 121)
    if why == "archived":
        FilamentSpoolStore(pg_pool).archive(other, True, actor="ann")
    inventory = FilamentInventory(pg_pool)
    inventory.link(printer, HT_A, 0, pink, _seen("PETG", GREY), actor="migration")
    _report(pg_pool, printer, _tray("PETG", GREY))
    assignments = [] if why == "none" else [_assigned(121)]

    assert inventory.reconcile_trays(assignments) == []
    assert _moves(pg_pool, pink) == [] and _moves(pg_pool, other) == []
    tray = _view_tray(inventory, assignments)
    assert tray.spool_id == pink
    assert tray.mismatch == TrayMismatch(reported_type="PETG", reported_rgba=GREY)


# -- what never changes (R-6713-5) ----------------------------------------------------


def test_a_tagged_tray_is_unchanged(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    tagged, grey = _spool(pg_pool, "PLA", PINK), _spool(pg_pool, "PETG", GREY)
    FilamentSpoolStore(pg_pool).bind_tag(tagged, tray_uuid=UUID_A, kind="bambu_mifare", actor="a")
    _link_bb(pg_pool, grey, 121)
    _report(pg_pool, printer, _tray("PETG", GREY, tray_uuid=UUID_A))
    inventory = FilamentInventory(pg_pool)

    assert inventory.reconcile_trays([_assigned(121)]) == []
    assert _moves(pg_pool, tagged) == [] and _moves(pg_pool, grey) == []
    tray = _view_tray(inventory, [_assigned(121)])
    assert (tray.spool_id, tray.found_by, tray.mismatch) == (tagged, "tag", None)


def test_a_tray_with_unbound_keys_is_unchanged(pg_pool: PgPool) -> None:
    """A tray that reports a tag no binding holds still resolves by its placement (the
    AMS rescan); it reports a tag, so it is not reconciled."""
    printer = _printer(pg_pool)
    pink, grey = _spool(pg_pool, "PLA", PINK), _spool(pg_pool, "PETG", GREY)
    _link_bb(pg_pool, grey, 121)
    inventory = FilamentInventory(pg_pool)
    inventory.link(printer, HT_A, 0, pink, _seen("PETG", GREY), actor="migration")
    # The link bound the tray's (absent) tag to nothing; the report now names a new one.
    _report(pg_pool, printer, _tray("PETG", GREY, tag_uid="0102030405060708"))

    assert inventory.reconcile_trays([_assigned(121)]) == []
    assert _moves(pg_pool, pink) == [] and _moves(pg_pool, grey) == []


def test_a_tray_no_printer_reports_is_unchanged(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    pink, grey = _spool(pg_pool, "PLA", PINK), _spool(pg_pool, "PETG", GREY)
    _link_bb(pg_pool, grey, 121)
    inventory = FilamentInventory(pg_pool)
    inventory.link(printer, HT_A, 0, pink, _seen("PETG", GREY), actor="migration")

    assert inventory.reconcile_trays([_assigned(121)]) == []
    assert _moves(pg_pool, pink) == [] and _moves(pg_pool, grey) == []


def test_a_stale_report_is_unchanged(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    pink, grey = _spool(pg_pool, "PLA", PINK), _spool(pg_pool, "PETG", GREY)
    _link_bb(pg_pool, grey, 121)
    inventory = FilamentInventory(pg_pool)
    inventory.link(printer, HT_A, 0, pink, _seen("PETG", GREY), actor="migration")
    _report(pg_pool, printer, _tray("PETG", GREY), seen_at=datetime.now(UTC) - timedelta(days=1))

    assert inventory.reconcile_trays([_assigned(121)]) == []
    assert _moves(pg_pool, pink) == []


# -- the scheduled pull (R-6713-2, R-6713-6) ------------------------------------------


def _bb_routes(router: respx.MockRouter) -> None:
    router.get(f"{BB}/api/v1/inventory/spools").respond(json=[])
    router.get(f"{BB}/api/v1/inventory/catalog").respond(json=[])
    router.get(f"{BB}/api/v1/inventory/usage").respond(json=[])


async def test_the_scheduled_pull_reconciles_the_prod_case(pg_pool: PgPool) -> None:
    _, pink, grey, _ = _prod_case(pg_pool)
    tray_place = _get(pg_pool, pink).location_id
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock() as router:
        _bb_routes(router)
        router.get(f"{BB}/api/v1/inventory/assignments").respond(
            json=[_assigned(121).model_dump(mode="json")]
        )
        assert await scheduled_pull(_filament(pg_pool), lambda: settings, FilamentSync) == "pulled"
    assert _get(pg_pool, pink).location_id is None
    assert _get(pg_pool, grey).location_id == tray_place


async def test_a_failed_assignment_read_moves_nothing(pg_pool: PgPool) -> None:
    _, pink, grey, _ = _prod_case(pg_pool)
    tray_place = _get(pg_pool, pink).location_id
    settings = StoredSettings(bambuddy_url=BB, bambuddy_api_key="k")
    with respx.mock() as router:
        _bb_routes(router)
        assignments = router.get(f"{BB}/api/v1/inventory/assignments").respond(503)
        assert await scheduled_pull(_filament(pg_pool), lambda: settings, FilamentSync) == "pulled"
        assert assignments.called
    assert _get(pg_pool, pink).location_id == tray_place
    assert _get(pg_pool, grey).location_id is None
    assert _moves(pg_pool, pink) == [] and _moves(pg_pool, grey) == []


def test_an_assigned_spool_another_tray_holds_stays(pg_pool: PgPool) -> None:
    """Bambuddy's assignment is stale when another reported tray holds its spool by
    its tag: nothing moves, and the tray is flagged."""
    printer = _printer(pg_pool)
    pink, grey = _spool(pg_pool, "PLA", PINK), _spool(pg_pool, "PETG", GREY)
    FilamentSpoolStore(pg_pool).bind_tag(grey, tray_uuid=UUID_A, kind="bambu_mifare", actor="a")
    _link_bb(pg_pool, grey, 121)
    inventory = FilamentInventory(pg_pool)
    inventory.link(printer, HT_A, 0, pink, _seen("PETG", GREY), actor="migration")
    report = {
        "ams": {
            "ams": [
                {"id": str(HT_A), "tray": [_tray("PETG", GREY)]},
                {"id": "0", "tray": [_tray("PETG", GREY, tray_uuid=UUID_A)]},
            ]
        }
    }
    PrinterReportStore(pg_pool).put(str(printer), report, datetime.now(UTC))

    assert inventory.reconcile_trays([_assigned(121)]) == []
    assert _moves(pg_pool, pink) == [] and _moves(pg_pool, grey) == []
    (trays,) = inventory.view(bb_assignments=[_assigned(121)]).trays
    flagged = next(t for t in trays.trays if t.ams_id == HT_A)
    assert flagged.mismatch == TrayMismatch(reported_type="PETG", reported_rgba=GREY)
