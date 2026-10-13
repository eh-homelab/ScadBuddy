"""#2164 — a filled tray Bambuddy has no spool for: found, and its likely spools ranked.

The recorded printer (``printer-status-fts.json``) has AMS 0 slot 3 (``tray_id`` 2)
loaded with an untagged black PLA, ``27272C``, as the maintainer's AMS-D slot 4 was."""

from __future__ import annotations

from scadbuddy.bambuddy.filaments import FilamentOptions, LoadedAt, SpoolOption
from scadbuddy.bambuddy.trays import (
    colour_word,
    family,
    tray_label,
    tray_material_colour,
    tray_of_spool_id,
    tray_spool_id,
    unknown_trays,
    with_trays,
)
from tests.bambuddy.test_extruders import fts_status


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
