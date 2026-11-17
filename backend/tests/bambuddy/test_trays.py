"""#2164 — a filled tray Bambuddy has no spool for: found, and its likely spools ranked.

The recorded printer (``printer-status-fts.json``) has AMS 0 slot 3 (``tray_id`` 2)
loaded with an untagged black PLA, ``27272C``, as the maintainer's AMS-D slot 4 was."""

from __future__ import annotations

from uuid import uuid4

import pytest

from scadbuddy.bambuddy.filaments import (
    FilamentOptions,
    LoadedAt,
    SlotNeed,
    SpoolOption,
    colours_match,
    family,
    suggest,
)
from scadbuddy.bambuddy.models import AmsTray, AmsUnit, PrinterStatus, SlotChoice
from scadbuddy.bambuddy.trays import (
    colour_word,
    loaded_trays,
    seen_trays,
    tray_label,
    tray_material_colour,
    tray_of_spool_id,
    tray_spool_id,
    unknown_trays,
    with_trays,
)
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.models import (
    Brand,
    BrandCreate,
    Material,
    SpoolCreate,
    SpoolEventAdd,
    SpoolTypeCreate,
)
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.types import SpoolTypeStore
from tests.bambuddy.test_extruders import fts_status
from tests.conftest import PgPool

UUID_A = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


def _loaded(spool_id: int, ams_id: int, tray_id: int, **extra: object) -> SpoolOption:
    return SpoolOption.model_validate(
        {
            "spool_id": spool_id,
            "material": "PLA",
            "loaded": LoadedAt(printer_id=1, ams_id=ams_id, tray_id=tray_id),
            **extra,
        }
    )


def _every_other_tray_assigned() -> list[SpoolOption]:
    """Every filled tray of the recording has a spool, but AMS 0 slot 3 and AMS 1 slot 1."""
    status = fts_status()
    spools: list[SpoolOption] = []
    for unit in status.ams:
        for tray in unit.tray:
            if (unit.id, tray.id) not in ((0, 2), (1, 0)):
                spools.append(_loaded(100 + unit.id * 4 + tray.id, unit.id, tray.id))
    return spools


INLAND_BLACK = SpoolOption(
    spool_id=16,
    material="PLA",
    brand="Inland",
    color_name="Black",
    colour="#1A1A1A",
    remaining_g=515,
)
EMPTY_BLACK = SpoolOption(
    spool_id=18, material="PLA", brand="Inland", color_name="Black", colour="#1A1A1A", remaining_g=0
)
BLACK_PETG = SpoolOption(spool_id=20, material="PETG", colour="#1A1A1A", remaining_g=900)
WHITE_PLA = SpoolOption(spool_id=21, material="PLA", colour="#FFFFFF", remaining_g=900)


def test_a_filled_tray_with_no_spool_is_found_with_its_likely_spools() -> None:
    spools = [*_every_other_tray_assigned(), EMPTY_BLACK, INLAND_BLACK, BLACK_PETG, WHITE_PLA]

    trays = {(tray.ams_id, tray.tray_id): tray for tray in unknown_trays(fts_status(), 1, spools)}

    black = trays[(0, 2)]
    assert black.label == "AMS-A slot 3"
    assert black.material == "PLA"
    assert black.colour == "#27272C"
    assert black.colour_word == "black"
    # Same material, close colour, on the shelf; the empty one last.
    assert black.candidates == [16, 18]
    assert black.spool_id == tray_spool_id(0, 2)
    assert (1, 0) in trays


def test_an_assigned_or_empty_tray_is_not_asked_about() -> None:
    spools = _every_other_tray_assigned()
    spools += [_loaded(1, 0, 2), _loaded(2, 1, 0)]
    assert unknown_trays(fts_status(), 1, spools) == []
    assert unknown_trays(None, 1, []) == []


def test_a_tray_goes_by_a_negative_id_that_names_it() -> None:
    assert tray_of_spool_id(tray_spool_id(3, 3)) == (3, 3)
    assert tray_of_spool_id(tray_spool_id(128, 0)) == (128, 0)
    assert tray_of_spool_id(16) is None
    assert tray_material_colour(tray_spool_id(0, 2), fts_status()) == ("PLA", "#27272C")


def test_trays_are_named_as_bambuddy_names_them() -> None:
    assert tray_label(3, 3) == "AMS-D slot 4"
    assert tray_label(128, 0) == "HT-A"
    assert tray_label(255, 0) == "External slot 1"


def test_colour_words_and_families() -> None:
    assert colour_word("#F5F5F0") == "white"
    assert colour_word("#3F8E43") == "green"
    assert colour_word(None) is None
    assert family("PETG HF") == "PETG"


def test_each_unknown_tray_is_also_offered_as_itself() -> None:
    options = FilamentOptions(
        library_file_id=1, printer_id=1, spools=[*_every_other_tray_assigned(), INLAND_BLACK]
    )

    with_trays(options, fts_status(), 1)

    offered = {spool.spool_id: spool for spool in options.spools if spool.tray_only}
    black = offered[tray_spool_id(0, 2)]
    assert black.material == "PLA"
    assert black.colour == "#27272C"
    assert black.color_name == "what's in AMS-A slot 3"
    assert black.remaining_g is None
    # The switch is fitted: AMS 0 rests on inlet B, the right.
    assert black.side == "R"
    # Not twice.
    with_trays(options, fts_status(), 1)
    assert len([spool for spool in options.spools if spool.tray_only]) == len(offered)


# --- a native run's trays over the SB spools (#6262 Task 5) ---------------------------


def _native_status(*trays: AmsTray) -> PrinterStatus:
    return PrinterStatus(id=0, name="Garage", connected=True, ams=[AmsUnit(id=0, tray=list(trays))])


def _filled(tray_id: int, colour: str, **extra: object) -> AmsTray:
    return AmsTray.model_validate(
        {"id": tray_id, "tray_type": "PLA", "tray_color": colour, "state": 11, **extra}
    )


@pytest.mark.requires_postgres
def test_a_tray_option_carries_its_sb_spool(pg_pool: PgPool) -> None:
    """The tagged tray holds SB spool #N: the option names it, with the brand its type
    gives, the ledger's grams and the spool's own alpha; the tray's colour stays its
    ``#RRGGBB`` (the render splits by RGB). The untagged tray holds none."""
    catalog = CatalogStore(pg_pool)
    material = next(
        r.id for r in catalog.list("materials") if isinstance(r, Material) and r.name == "PLA"
    )
    brand = catalog.create("brands", BrandCreate(name="Polymaker"))
    assert isinstance(brand, Brand)
    kind = SpoolTypeStore(pg_pool).create(
        SpoolTypeCreate(
            code="PM-TEAL",
            brand_id=brand.id,
            material_id=material,
            colour_name="Teal",
            rgba="#00808080",
            label_weight_g=1000,
        )
    )
    spools = FilamentSpoolStore(pg_pool)
    spool = spools.create(SpoolCreate(type_id=kind.id), actor="ann")
    spools.add_event(spool.id, SpoolEventAdd(kind="weighed", grams=640), actor="ann")
    spools.bind_tag(spool.id, tray_uuid=UUID_A, kind="bambu_mifare", actor="ann")
    printer = uuid4()
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (%s, %s, 'Garage')",
            (printer, f"SN{printer.hex[:8]}"),
        )
    status = _native_status(_filled(0, "00808080", tray_uuid=UUID_A), _filled(1, "FFFFFFFF"))
    held = FilamentInventory(pg_pool).trays(printer, seen_trays(status))

    tagged, plain = loaded_trays(status, 0, "Garage", held)

    assert (tagged.sb_spool_id, tagged.sb_number) == (spool.id, spool.number)
    assert (tagged.brand, tagged.color_name) == ("Polymaker", "Teal")
    assert (tagged.remaining_g, tagged.label_weight_g) == (640, 1000)
    assert (tagged.rgba, tagged.colour, tagged.needs_type) == ("#00808080", "#008080", False)
    assert (plain.sb_spool_id, plain.sb_number, plain.brand, plain.remaining_g) == (
        None,
        None,
        None,
        None,
    )
    assert plain.rgba == "#FFFFFFFF"


def test_an_opaque_slot_does_not_suggest_a_translucent_spool() -> None:
    """Review Focus 2, plan Decision 12: a white slot opens on the opaque white tray, not
    the translucent one, though their RGB is the same and the translucent tray is first."""
    status = _native_status(_filled(0, "FFFFFF80"), _filled(1, "FFFFFFFF"))
    translucent, opaque = loaded_trays(status, 0, "Garage")
    assert (translucent.rgba, opaque.rgba) == ("#FFFFFF80", "#FFFFFFFF")
    slot = SlotNeed(slot_id=1, colour="#FFFFFF")

    assert suggest([slot], [translucent, opaque], printer_id=0) == [
        SlotChoice(slot_id=1, spool_id=opaque.spool_id)
    ]
    assert not colours_match("#FFFFFF", translucent.colour, spool_rgba=translucent.rgba)
    assert colours_match("#FFFFFF", opaque.colour, spool_rgba=opaque.rgba)
