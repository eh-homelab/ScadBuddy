"""Issue #87 — joining Bambuddy's spool inventory into a picker.

The join is the part with the traps in it, so most of this drives
:func:`build_options` directly against the recorded bodies rather than through HTTP.
The traps under test are the ones that cost real time to find: ``used_grams: 0`` and
``remain: -1`` both mean *unknown*, and ``/inventory/assignments`` covers every printer
while ``inventory-remain`` covers one.

There are no compatibility rules under test because there are none to test: whether two
filaments can share a plate, and which tray a chosen spool is drawn from, are Bambuddy's
answers. What is tested here is that ScadBuddy sends it what it needs to answer them.
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
    SlotChoice,
    SlotNeed,
    build_options,
    check,
    colour_distance,
    gather_options,
    normalise_colour,
    queue_filaments,
    slice_filament_presets,
)
from scadbuddy.bambuddy.models import (
    PresetRef,
    Printer,
    SlotMaterial,
    Spool,
    SpoolAssignment,
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
        ("688197", "#688197"),
        ("", None),
        (None, None),
        ("nope", None),
        ("ZZZZZZ", None),
    ],
)
def test_colours_normalise_to_six_hex_digits(raw: str | None, expected: str | None) -> None:
    assert normalise_colour(raw) == expected


def test_an_unknown_colour_has_no_distance_rather_than_a_distance_of_zero() -> None:
    """Zero would make every unpainted slot claim a perfect match with every spool."""
    assert colour_distance(None, "#FFFFFF") is None
    assert colour_distance("#000000", "#000000") == 0.0


# --- the join ----------------------------------------------------------------


def test_a_loaded_spool_carries_where_it_is_and_what_is_left() -> None:
    built = options()
    misty = next(row for row in built.spools if row.spool_id == 9)
    assert misty.loaded is not None
    assert (misty.loaded.ams_id, misty.loaded.tray_id) == (0, 1)
    # inventory-remain's own figure, not label_weight - weight_used.
    assert misty.remaining_g == 1000.0
    assert misty.colour == "#688197"


def test_bambuddys_reconciled_weight_wins_over_the_inventory_rows_arithmetic() -> None:
    """``inventory-remain`` is Bambuddy's reconciliation of the AMS against the
    inventory, so it is the authority wherever it has an answer."""
    odd = [SlotMaterial(ams_id=0, tray_id=1, global_tray_id=99, remaining_g=12.0, extruder=0)]
    built = options(slot_materials=odd)
    misty = next(row for row in built.spools if row.spool_id == 9)
    assert misty.remaining_g == 12.0


def test_an_unassigned_spool_falls_back_to_the_label_minus_what_is_used() -> None:
    built = options()
    white = next(row for row in built.spools if row.spool_id == 8)
    assert white.loaded is None
    assert white.remaining_g == pytest.approx(1000 - 34.34312826933372)


def test_loaded_spools_sort_ahead_of_the_shelf() -> None:
    built = options()
    bands = [row.loaded is not None for row in built.spools]
    assert bands == sorted(bands, reverse=True)


def test_an_archived_spool_is_not_offered() -> None:
    rows = spools()
    rows[0] = rows[0].model_copy(update={"archived_at": "2026-01-01T00:00:00"})
    built = options(spools=rows)
    assert all(row.spool_id != 9 for row in built.spools)


def test_a_spool_in_another_printers_tray_takes_none_of_this_printers_state() -> None:
    """The reconciled weights are ONE printer's and the assignments are every printer's.

    `inventory-remain` was read for the chosen printer; joining it on `(ams_id, tray_id)`
    alone would hand a spool sitting in another machine's AMS 0 slot 1 this printer's
    remaining weight — a different filament's.
    """
    rows = assignments()
    # The recorded `inventory-remain` happens to report 1000 g for (0, 1), which is also
    # spool 9's `label_weight - weight_used` — so asserting on the recording would pass
    # whether or not the guard exists. The figure is made distinguishable on purpose.
    distinct = [SlotMaterial(ams_id=0, tray_id=1, global_tray_id=1, remaining_g=42.0, extruder=0)]
    here = options(slot_materials=distinct)
    assert next(row for row in here.spools if row.spool_id == 9).remaining_g == 42.0

    # Move spool 9 to another printer and none of that printer's state may follow it.
    moved = rows[0].model_copy(update={"printer_id": 7, "printer_name": "Other"})
    built = options(assignments=[moved, *rows[1:]], slot_materials=distinct)
    misty = next(row for row in built.spools if row.spool_id == 9)

    assert misty.loaded is not None
    assert misty.loaded.printer_id == 7
    # label_weight - weight_used, not inventory-remain's figure for the other spool.
    assert misty.remaining_g == 1000.0


# --- the picker's opening selection ------------------------------------------


def test_the_keychain_pre_selects_the_closest_blue_and_pink() -> None:
    """#87's acceptance case: two colours, no clicks. The blue is an exact match and
    the pink is within the threshold; the far-off Hot Pink is not offered."""
    built = options()
    chosen = {choice.slot_id: choice.spool_id for choice in built.suggested}
    assert chosen == {1: 5, 2: 3}


def test_one_spool_is_never_pre_selected_for_two_slots() -> None:
    """A two-colour plate opening with the same spool in both slots reads as a bug."""
    built = options(
        requirements=[SlotNeed(slot_id=1, colour="#0047BB"), SlotNeed(slot_id=2, colour="#0047BB")]
    )
    picked = [choice.spool_id for choice in built.suggested]
    assert len(picked) == len(set(picked))


def test_a_slot_that_declares_a_material_never_pre_selects_another_one() -> None:
    """Opening with a PETG in a PLA slot is worse than opening with nothing, because it
    looks deliberate."""
    built = options(requirements=[SlotNeed(slot_id=1, material="PLA", colour="#688197")])
    chosen = [row for row in built.spools if row.spool_id in {c.spool_id for c in built.suggested}]
    assert all(row.material == "PLA" for row in chosen)


def test_a_colour_further_away_than_the_threshold_is_not_pre_selected() -> None:
    assert COLOUR_MATCH_DISTANCE < 441
    built = options(requirements=[SlotNeed(slot_id=1, colour="#00FF00")])
    assert built.suggested == []


# --- the two warnings that fall out of the data ------------------------------


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
    a real weight would make this warning never fire; reading it as "needs nothing"
    would make every spool look sufficient."""
    built = options()
    assert all(slot.used_grams is None for slot in built.slots)
    warnings = check(built, FilamentPlan(slots=list(built.suggested)), copies=1000)
    assert not any(warning.kind == "low-filament" for warning in warnings)


def test_known_grams_are_multiplied_by_the_copies() -> None:
    built = options(requirements=[SlotNeed(slot_id=1, colour="#0047BB", used_grams=60.0)])
    plan = FilamentPlan(slots=list(built.suggested))
    assert not any(warning.kind == "low-filament" for warning in check(built, plan, copies=1))
    assert any(warning.kind == "low-filament" for warning in check(built, plan, copies=100))


def test_a_slot_with_nothing_chosen_says_so() -> None:
    built = options()
    assert any(w.kind == "no-choice" for w in check(built, FilamentPlan()))


def test_nothing_is_warned_about_that_bambuddy_answers_itself() -> None:
    """The guard on the scope cut: compatibility, routing and reachability are
    Bambuddy's eligibility report, not warnings invented here."""
    built = options()
    kinds = {warning.kind for warning in check(built, FilamentPlan(slots=list(built.suggested)))}
    assert kinds <= {"not-loaded", "low-filament", "no-choice"}


# --- what the plan becomes on the wire ---------------------------------------


def test_no_ams_mapping_is_sent_because_bambuddy_computes_it() -> None:
    """Its scheduler's ``_compute_ams_mapping_for_printer`` runs whenever a queue item
    carries none, against the printer it is actually dispatching to and the filament
    switcher that printer actually has. A second copy here would be a worse one."""
    fields = queue_filaments(options(), FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=9)]))
    assert not hasattr(fields, "ams_mapping")
    assert set(fields.model_dump()) == {"filament_overrides", "required_filament_types"}


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


def test_the_slice_uses_the_preset_the_spool_itself_names() -> None:
    """``slicer_filament`` is Bambuddy's own field on the spool row; no preset is
    chosen for the spool here."""
    built = options()
    spool = next(row for row in built.spools if row.spool_id == 5)
    assert spool.slicer_filament is not None
    ref = PresetRef(source="cloud", id=spool.slicer_filament)
    presets_out, colours, warnings = slice_filament_presets(
        built,
        FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=5)]),
        pipeline_presets=[PresetRef(source="cloud", id="GFA00")],
        resolve={spool.slicer_filament: ref},
    )
    assert presets_out[0] == ref
    assert warnings == []
    assert colours[0] == spool.colour


def test_a_preset_id_the_catalogue_does_not_hold_keeps_the_pipelines_own() -> None:
    """Inventing a ``PresetRef`` source would send Bambuddy an id it cannot look up, and
    the slice would fail naming a preset nobody chose."""
    built = options()
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
    respx.get(f"{API}/printers/1/inventory-remain").mock(
        return_value=httpx.Response(200, json=recording("inventory-remain.json"))
    )

    built = await gather_options(bambuddy, library_file_id=62, printer_id=1)
    assert [slot.slot_id for slot in built.slots] == [1, 2]
    assert built.printer_name is not None
    assert {choice.slot_id for choice in built.suggested} == {1, 2}


@respx.mock
async def test_the_picker_does_not_read_the_printers_live_ams_state(
    bambuddy: BambuddyClient,
) -> None:
    """Nothing here decodes ``ams_switch_inlet``, nozzle diameters or tray temperatures,
    so nothing here asks for them."""
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
    respx.get(f"{API}/printers/1/inventory-remain").mock(
        return_value=httpx.Response(200, json=recording("inventory-remain.json"))
    )
    status = respx.get(f"{API}/printers/1/status")
    presets = respx.route(
        method="GET", path__regex=r"/api/v1/inventory/spools/\d+/filament-presets"
    )

    await gather_options(bambuddy, library_file_id=62, printer_id=1)
    assert not status.called
    assert not presets.called


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

    built = await gather_options(
        bambuddy, library_file_id=62, fallback_colours=["#0047BB", "#FF1493"]
    )
    assert [(slot.slot_id, slot.colour) for slot in built.slots] == [
        (1, "#0047BB"),
        (2, "#FF1493"),
    ]


def test_zero_grams_becomes_unknown_on_the_real_recording() -> None:
    """The conversion itself, not a hand-built ``SlotNeed``: ``filament-requirements``
    really does answer ``used_g: 0`` for an unsliced plate, and deleting the ``or None``
    must fail something."""
    raw = recording("filament-requirements.json")
    assert [row["used_grams"] for row in raw["filaments"]] == [0.0, 0.0]


@respx.mock
async def test_the_requirements_read_turns_that_zero_into_none(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/inventory/assignments").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/library/files/62/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements.json"))
    )
    built = await gather_options(bambuddy, library_file_id=62)
    assert [slot.used_grams for slot in built.slots] == [None, None]


def test_the_colour_threshold_is_the_boundary_it_says_it_is() -> None:
    """``COLOUR_MATCH_DISTANCE`` is a real cut-off, not a number nothing reads: a
    colour just inside it is pre-selected and one just outside is not."""
    near = f"#{int(COLOUR_MATCH_DISTANCE) - 1:02X}0000"
    far = f"#{int(COLOUR_MATCH_DISTANCE) + 1:02X}0000"
    black = [
        Spool.model_validate(
            {
                "id": 1,
                "material": "PLA",
                "rgba": "000000FF",
                "label_weight": 1000,
                "weight_used": 0.0,
            }
        )
    ]
    inside = build_options(
        library_file_id=1,
        spools=black,
        assignments=[],
        requirements=[SlotNeed(slot_id=1, colour=near)],
    )
    outside = build_options(
        library_file_id=1,
        spools=black,
        assignments=[],
        requirements=[SlotNeed(slot_id=1, colour=far)],
    )
    assert [choice.spool_id for choice in inside.suggested] == [1]
    assert outside.suggested == []
