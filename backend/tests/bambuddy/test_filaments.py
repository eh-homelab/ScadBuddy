"""Issue #87 — joining Bambuddy's spool inventory, and the rules over it.

The join is the part with the traps in it, so most of this drives
:func:`build_options` directly against the recorded bodies rather than through HTTP.
The traps under test are the ones that cost real time to find: an AMS id is the
printer's own numbering, ``used_grams: 0`` and ``remain: -1`` both mean *unknown*, and
a spool row's temperature window is null while the tray's is not.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments import (
    COLOUR_MATCH_DISTANCE,
    FilamentOptions,
    FilamentPlan,
    LoadedAt,
    SlotChoice,
    SlotNeed,
    SpoolOption,
    build_options,
    check,
    colour_distance,
    gather_options,
    global_tray_id,
    inlet_extruders,
    normalise_colour,
    process_nozzle_diameter,
    queue_filaments,
    slice_filament_presets,
)
from scadbuddy.bambuddy.models import (
    PresetRef,
    Printer,
    PrinterStatus,
    SlotMaterial,
    Spool,
    SpoolAssignment,
    SpoolFilamentPreset,
)
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"


def spools() -> list[Spool]:
    return [Spool.model_validate(row) for row in recording("inventory-spools.json")]


def assignments() -> list[SpoolAssignment]:
    return [SpoolAssignment.model_validate(row) for row in recording("inventory-assignments.json")]


def slot_materials() -> list[SlotMaterial]:
    return [
        SlotMaterial.model_validate(row)
        for row in recording("inventory-remain.json")["slot_materials"]
    ]


def status() -> PrinterStatus:
    return PrinterStatus.model_validate(recording("printer-status.json"))


def printer() -> Printer:
    return Printer.model_validate(recording("printer.json"))


def keychain_slots() -> list[SlotNeed]:
    """The two-colour keychain of #87's acceptance case, as Bambuddy reads the plate.

    ``type`` is empty and ``used_grams`` zero because ScadBuddy uploads an *unsliced*
    plate — which is why both become ``None`` rather than ``""`` and ``0.0``.
    """
    return [
        SlotNeed(slot_id=1, colour="#0047BB"),
        SlotNeed(slot_id=2, colour="#FF1493"),
    ]


def options(**extra: object) -> FilamentOptions:
    kwargs: dict[str, object] = {
        "library_file_id": 62,
        "spools": spools(),
        "assignments": assignments(),
        "requirements": keychain_slots(),
        "printer": printer(),
        "status": status(),
        "slot_materials": slot_materials(),
    }
    kwargs.update(extra)
    return build_options(**kwargs)  # type: ignore[arg-type]


# --- the small pure pieces ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("688197FF", "#688197"),
        ("#688197FF", "#688197"),
        ("#688197", "#688197"),
        ("00000000", "#000000"),
        ("", None),
        (None, None),
        ("not a colour", None),
        ("68819", None),
    ],
)
def test_colours_normalise_to_six_hex_digits(raw: str | None, expected: str | None) -> None:
    """The three spellings Bambuddy uses for one colour all land on the same value."""
    assert normalise_colour(raw) == expected


def test_an_unknown_colour_has_no_distance_rather_than_a_distance_of_zero() -> None:
    """``0`` would read as "identical" and auto-select the first spool in the list."""
    assert colour_distance(None, "#FFFFFF") is None
    assert colour_distance("#000000", "#000000") == 0.0


def test_the_flat_tray_id_is_the_ams_id_itself_at_128() -> None:
    """A single-slot AMS-HT reports ``id: 128``; ``128 * 4`` would address nothing."""
    assert global_tray_id(0, 1) == 1
    assert global_tray_id(1, 0) == 4
    assert global_tray_id(128, 0) == 128


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Bambu PETG Basic @BBL H2C 0.4 nozzle", "0.4"),
        ("0.20mm Standard @BBL H2C 0.2 nozzle", "0.2"),
        ("Something with no diameter", None),
        (None, None),
    ],
)
def test_the_nozzle_diameter_is_read_out_of_the_process_preset_name(
    name: str | None, expected: str | None
) -> None:
    """Bambuddy models no diameter field anywhere, so the name is the only source."""
    assert process_nozzle_diameter(name) == expected


# --- the join ----------------------------------------------------------------


def test_a_loaded_spool_carries_where_it_is_and_what_is_left() -> None:
    built = options()
    misty = next(row for row in built.spools if row.spool_id == 9)
    assert misty.loaded is not None
    assert (misty.loaded.ams_id, misty.loaded.tray_id) == (0, 1)
    # inventory-remain's own figure, not label_weight - weight_used.
    assert misty.remaining_g == 1000.0
    assert misty.colour == "#688197"


def test_the_flat_tray_id_is_read_from_bambuddy_not_recomputed() -> None:
    """``inventory-remain`` is the authority: a printer that numbers a slot unusually
    must not be overridden by our own arithmetic."""
    odd = [
        SlotMaterial(ams_id=0, tray_id=1, global_tray_id=99, remaining_g=12.0, extruder=0),
    ]
    built = options(slot_materials=odd)
    misty = next(row for row in built.spools if row.spool_id == 9)
    assert misty.loaded is not None
    assert misty.loaded.global_tray_id == 99
    assert misty.remaining_g == 12.0


def test_an_unassigned_spool_falls_back_to_the_label_minus_what_is_used() -> None:
    built = options()
    white = next(row for row in built.spools if row.spool_id == 8)
    assert white.loaded is None
    assert white.remaining_g == pytest.approx(1000 - 34.34312826933372)


def test_the_temperature_window_comes_from_the_tray_not_the_spool_row() -> None:
    """Every spool row on the live instance has ``nozzle_temp_min/max: null``; the AMS
    tray is where the real window is, which is why "unknown" is a real answer."""
    built = options()
    misty = next(row for row in built.spools if row.spool_id == 9)
    assert (misty.nozzle_temp_min, misty.nozzle_temp_max) == (230, 260)
    assert misty.temperature_from == "tray"

    shelf = next(row for row in built.spools if row.spool_id == 5)
    assert shelf.temperature_from == "unknown"
    assert shelf.nozzle_temp_min is None


def test_loaded_spools_sort_ahead_of_the_shelf() -> None:
    built = options()
    bands = [row.loaded is not None for row in built.spools]
    assert bands == sorted(bands, reverse=True)


def test_an_archived_spool_is_not_offered() -> None:
    rows = spools()
    rows[0] = rows[0].model_copy(update={"archived_at": "2026-01-01T00:00:00"})
    built = options(spools=rows)
    assert all(row.spool_id != 9 for row in built.spools)


def test_the_inlet_to_extruder_map_is_derived_and_drops_contradictions() -> None:
    """Nothing here knows that inlet A is extruder 0 — the printer says so, or nobody
    does. Two extruders reported for one inlet means the reading is inconsistent."""

    def option(inlet: str, extruder: int, spool_id: int) -> SpoolOption:
        return SpoolOption(
            spool_id=spool_id,
            material="PLA",
            loaded=LoadedAt(
                printer_id=1,
                ams_id=spool_id,
                tray_id=0,
                global_tray_id=spool_id,
                extruder=extruder,
                inlet=inlet,
            ),
        )

    consistent = FilamentOptions(
        library_file_id=1, spools=[option("A", 0, 1), option("A", 0, 2), option("B", 1, 3)]
    )
    assert inlet_extruders(consistent) == {"A": 0, "B": 1}

    contradictory = FilamentOptions(
        library_file_id=1, spools=[option("A", 0, 1), option("A", 1, 2)]
    )
    assert inlet_extruders(contradictory) == {}


# --- auto-match --------------------------------------------------------------


def test_the_keychain_pre_selects_the_closest_blue_and_pink() -> None:
    """#87's acceptance case: two colours, no clicks. The blue is an exact match and
    the pink is within the threshold; the far-off Hot Pink is not offered."""
    built = options()
    chosen = {choice.slot_id: choice.spool_id for choice in built.suggested}
    assert chosen == {1: 5, 2: 3}


def test_one_spool_is_never_suggested_for_two_slots() -> None:
    """Without this a two-colour plate happily maps both slots onto one tray, which is
    the same rule Bambuddy's own matcher applies to trays."""
    built = options(
        requirements=[SlotNeed(slot_id=1, colour="#0047BB"), SlotNeed(slot_id=2, colour="#0047BB")]
    )
    picked = [choice.spool_id for choice in built.suggested]
    assert len(picked) == len(set(picked))


def test_a_slot_that_declares_a_material_never_matches_another_one() -> None:
    """Auto-selecting a PETG for a PLA slot is worse than selecting nothing, because it
    looks deliberate."""
    built = options(requirements=[SlotNeed(slot_id=1, material="PLA", colour="#688197")])
    chosen = [row for row in built.spools if row.spool_id in {c.spool_id for c in built.suggested}]
    assert all(row.material == "PLA" for row in chosen)


def test_a_colour_further_away_than_the_threshold_is_not_suggested() -> None:
    assert COLOUR_MATCH_DISTANCE < 441
    built = options(requirements=[SlotNeed(slot_id=1, colour="#00FF00")])
    assert built.suggested == []


# --- the rules ---------------------------------------------------------------


def shelf_spool(
    spool_id: int, *, low: int | None, high: int | None, extruder: int | None = None
) -> SpoolOption:
    return SpoolOption(
        spool_id=spool_id,
        material="PLA" if low and low < 220 else "PETG",
        nozzle_temp_min=low,
        nozzle_temp_max=high,
        temperature_from="tray" if low is not None else "unknown",
        loaded=(
            LoadedAt(
                printer_id=1, ams_id=0, tray_id=spool_id, global_tray_id=spool_id, extruder=extruder
            )
            if extruder is not None
            else None
        ),
    )


def two_slot_options(*spool_rows: SpoolOption) -> FilamentOptions:
    return FilamentOptions(
        library_file_id=1,
        printer_id=1,
        slots=[SlotNeed(slot_id=1), SlotNeed(slot_id=2)],
        spools=list(spool_rows),
    )


def plan_for(*spool_ids: int) -> FilamentPlan:
    return FilamentPlan(
        slots=[SlotChoice(slot_id=index + 1, spool_id=sid) for index, sid in enumerate(spool_ids)]
    )


def test_two_filaments_that_do_not_share_a_nozzle_temperature_are_flagged() -> None:
    """This *is* the "PLA must not share a plate with PETG" rule — 190-230 against
    230-260 — derived rather than listed, so it keeps working as filaments are added."""
    built = two_slot_options(
        shelf_spool(1, low=190, high=220, extruder=0),
        shelf_spool(2, low=230, high=260, extruder=0),
    )
    kinds = [warning.kind for warning in check(built, plan_for(1, 2))]
    assert "temperature" in kinds


def test_two_extruders_heat_independently_so_the_pair_is_not_flagged() -> None:
    """A PLA on one nozzle and a PETG on the other is what a two-extruder machine is
    for; warning about it would make the rule noise."""
    built = two_slot_options(
        shelf_spool(1, low=190, high=220, extruder=0),
        shelf_spool(2, low=230, high=260, extruder=1),
    )
    kinds = [warning.kind for warning in check(built, plan_for(1, 2))]
    assert "temperature" not in kinds


def test_an_unknown_window_is_reported_as_unknown_rather_than_assumed_compatible() -> None:
    built = two_slot_options(
        shelf_spool(1, low=None, high=None, extruder=0),
        shelf_spool(2, low=230, high=260, extruder=0),
    )
    kinds = [warning.kind for warning in check(built, plan_for(1, 2))]
    assert "unknown-temperature" in kinds
    assert "temperature" not in kinds


def test_an_unloaded_spool_says_how_to_load_it_rather_than_failing() -> None:
    built = options()
    warnings = check(built, FilamentPlan(slots=list(built.suggested)))
    not_loaded = [warning for warning in warnings if warning.kind == "not-loaded"]
    assert not_loaded
    assert "Load" in not_loaded[0].message


def test_a_spool_loaded_in_another_printer_says_which_one() -> None:
    rows = assignments()
    moved = rows[0].model_copy(update={"printer_id": 7, "printer_name": "Other"})
    built = options(assignments=[moved, *rows[1:]])
    warnings = check(built, FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=9)]))
    assert any("Other" in warning.message for warning in warnings)


def test_unknown_grams_never_produce_a_low_filament_warning() -> None:
    """``used_grams: 0`` is what an unsliced plate reports for every slot. Reading it as
    a real weight would make this rule never fire; reading it as "needs nothing" would
    make every spool look sufficient."""
    built = options()
    assert all(slot.used_grams is None for slot in built.slots)
    warnings = check(built, FilamentPlan(slots=list(built.suggested)), copies=1000)
    assert not any(warning.kind == "low-filament" for warning in warnings)


def test_known_grams_are_multiplied_by_the_copies() -> None:
    built = options(requirements=[SlotNeed(slot_id=1, colour="#0047BB", used_grams=60.0)])
    plan = FilamentPlan(slots=list(built.suggested))
    assert not any(warning.kind == "low-filament" for warning in check(built, plan, copies=1))
    assert any(warning.kind == "low-filament" for warning in check(built, plan, copies=100))


def test_a_spool_with_no_preset_for_the_sliced_nozzle_is_flagged() -> None:
    presets = {
        5: [
            SpoolFilamentPreset(
                id=1,
                spool_id=5,
                printer_model="H2C",
                nozzle_diameter="0.2",
                slicer_filament="GFA05_24",
            )
        ]
    }
    built = options(
        presets_by_spool=presets,
        process_preset_name="0.20mm Standard @BBL H2C 0.4 nozzle",
    )
    warnings = check(built, FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=5)]))
    assert any(warning.kind == "nozzle-mismatch" for warning in warnings)


def test_a_spool_routed_to_the_wrong_extruder_names_the_ams_and_the_inlet() -> None:
    """The filament-switcher check, in the one form the data can decide: this spool is
    fed to an extruder whose nozzle is not the one the pipeline sliced for."""
    built = FilamentOptions(
        library_file_id=1,
        printer_id=1,
        nozzle_diameters=["0.2", "0.4"],
        ams_switch_inlet={"0": "B"},
        process_nozzle_diameter="0.4",
        slots=[SlotNeed(slot_id=1)],
        spools=[
            SpoolOption(
                spool_id=1,
                material="PLA",
                loaded=LoadedAt(
                    printer_id=1, ams_id=0, tray_id=0, global_tray_id=0, extruder=0, inlet="B"
                ),
            )
        ],
    )
    warnings = [w for w in check(built, plan_for(1)) if w.kind == "unreachable"]
    assert warnings and "AMS 0" in warnings[0].message and "inlet B" in warnings[0].message


def test_a_single_extruder_printer_says_nothing_about_routing() -> None:
    built = FilamentOptions(
        library_file_id=1,
        printer_id=1,
        nozzle_diameters=["0.4"],
        process_nozzle_diameter="0.4",
        slots=[SlotNeed(slot_id=1)],
        spools=[
            SpoolOption(
                spool_id=1,
                material="PLA",
                loaded=LoadedAt(printer_id=1, ams_id=0, tray_id=0, global_tray_id=0, extruder=0),
            )
        ],
    )
    assert not [w for w in check(built, plan_for(1)) if w.kind == "unreachable"]


def test_a_slot_with_nothing_chosen_says_so() -> None:
    built = options()
    assert any(w.kind == "no-choice" for w in check(built, FilamentPlan()))


# --- what the plan becomes on the wire ---------------------------------------


def test_the_mapping_is_positional_and_an_unloaded_slot_keeps_its_place() -> None:
    """Dropping the slot instead would shift every later slot onto the wrong tray;
    ``-1`` is Bambuddy's own "unresolved" sentinel."""
    built = options()
    # Spool 9 is loaded (flat tray 1); spool 5 is on the shelf.
    plan = FilamentPlan(
        slots=[SlotChoice(slot_id=1, spool_id=5), SlotChoice(slot_id=2, spool_id=9)]
    )
    fields = queue_filaments(built, plan)
    assert fields.ams_mapping == [-1, 1]


def test_the_overrides_carry_the_shape_bambuddys_scheduler_reads() -> None:
    built = options(requirements=[SlotNeed(slot_id=1, colour="#0047BB", used_grams=12.5)])
    fields = queue_filaments(built, FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=5)]))
    assert fields.filament_overrides == [
        {"slot_id": 1, "type": "PLA", "color": "#0047BB", "used_grams": 12.5}
    ]
    assert fields.required_filament_types == ["PLA"]
    assert "force_color_match" not in fields.filament_overrides[0]


def test_force_colour_match_is_opt_in() -> None:
    """On a model-targeted item it can leave the job unschedulable, so it is off unless
    the caller asks."""
    built = options()
    plan = FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=5)], force_colour_match=True)
    assert queue_filaments(built, plan).filament_overrides[0]["force_color_match"] is True


def test_required_types_are_deduplicated_in_slot_order() -> None:
    built = options()
    plan = FilamentPlan(
        slots=[SlotChoice(slot_id=1, spool_id=5), SlotChoice(slot_id=2, spool_id=3)]
    )
    assert queue_filaments(built, plan).required_filament_types == ["PLA"]


def test_the_nozzle_specific_preset_wins_over_the_spools_generic_one() -> None:
    """The same spool slices as a different profile through a 0.2 than through a 0.4."""
    presets = {
        5: [
            SpoolFilamentPreset(
                id=1,
                spool_id=5,
                printer_model="H2C",
                nozzle_diameter="0.4",
                slicer_filament="GFA05_23",
            )
        ]
    }
    built = options(presets_by_spool=presets, process_preset_name="… H2C 0.4 nozzle")
    pipeline_presets = [
        PresetRef(source="cloud", id="GFA00"),
        PresetRef(source="cloud", id="GFA00"),
    ]
    resolve = {"GFA05_23": PresetRef(source="cloud", id="GFA05_23")}
    presets_out, colours, warnings = slice_filament_presets(
        built,
        FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=5)]),
        pipeline_presets=pipeline_presets,
        resolve=resolve,
    )
    assert presets_out[0] == PresetRef(source="cloud", id="GFA05_23")
    assert presets_out[1] == PresetRef(source="cloud", id="GFA00")
    assert colours[0] == "#0047BB"
    assert not warnings


def test_a_preset_id_the_catalogue_does_not_hold_keeps_the_pipelines_own() -> None:
    """Inventing a ``PresetRef`` source would send Bambuddy an id it cannot look up, and
    the slice would fail naming a preset nobody chose."""
    built = options(process_preset_name="… H2C 0.4 nozzle")
    pipeline_presets = [
        PresetRef(source="cloud", id="GFA00"),
        PresetRef(source="cloud", id="GFA00"),
    ]
    presets_out, _, warnings = slice_filament_presets(
        built,
        FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=5)]),
        pipeline_presets=pipeline_presets,
        resolve={},
    )
    assert presets_out[0] == PresetRef(source="cloud", id="GFA00")
    assert any("no slicer preset" in warning.message for warning in warnings)


# --- reading it all off a live-shaped Bambuddy --------------------------------


@respx.mock
async def test_gather_options_reads_every_source_once(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=recording("inventory-spools.json"))
    )
    respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )
    respx.get(f"{API}/library/files/62/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements.json"))
    )
    respx.get(f"{API}/printers/1").mock(
        return_value=httpx.Response(200, json=recording("printer.json"))
    )
    respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=recording("printer-status.json"))
    )
    respx.get(f"{API}/printers/1/inventory-remain").mock(
        return_value=httpx.Response(200, json=recording("inventory-remain.json"))
    )
    respx.route(method="GET", path__regex=r"/api/v1/inventory/spools/\d+/filament-presets").mock(
        return_value=httpx.Response(200, json=recording("spool-filament-presets.json"))
    )

    built = await gather_options(
        bambuddy,
        library_file_id=62,
        printer_id=1,
        process_preset_name="0.20mm Standard @BBL H2C 0.4 nozzle",
    )
    assert built.printer_model == "H2C"
    assert [slot.slot_id for slot in built.slots] == [1, 2]
    assert built.process_nozzle_diameter == "0.4"
    assert {choice.slot_id for choice in built.suggested} == {1, 2}


@respx.mock
async def test_a_3mf_bambuddy_cannot_read_falls_back_to_the_outputs_colours(
    bambuddy: BambuddyClient,
) -> None:
    """A plate whose slice info is unreadable must still open the picker; the output's
    own colour list is the same information in the same order."""
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=recording("inventory-spools.json"))
    )
    respx.get(f"{API}/inventory/assignments").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/library/files/62/filament-requirements").mock(
        return_value=httpx.Response(200, json={"file_id": 62, "filaments": []})
    )
    respx.route(method="GET", path__regex=r"/api/v1/inventory/spools/\d+/filament-presets").mock(
        return_value=httpx.Response(200, json=[])
    )

    built = await gather_options(
        bambuddy, library_file_id=62, fallback_colours=["#0047BB", "#FF1493"]
    )
    assert [(slot.slot_id, slot.colour) for slot in built.slots] == [
        (1, "#0047BB"),
        (2, "#FF1493"),
    ]
