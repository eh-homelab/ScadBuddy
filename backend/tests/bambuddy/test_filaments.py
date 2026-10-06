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

from scadbuddy.bambuddy import filaments
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments import (
    COLOUR_MATCH_DISTANCE,
    FilamentOptions,
    FilamentPlan,
    SlotNeed,
    across_plates,
    build_options,
    check,
    colour_distance,
    gather_options,
    gather_plate_options,
    normalise_colour,
    queue_filaments,
)
from scadbuddy.bambuddy.models import (
    Printer,
    SlotChoice,
    SlotMaterial,
    Spool,
    SpoolAssignment,
)
from scadbuddy.core.problems import ApiError
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
    the pink is the nearer of Magenta and Hot Pink."""
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


def _plate(*slots: SlotNeed) -> FilamentOptions:
    return FilamentOptions(library_file_id=1, slots=list(slots))


def test_a_slot_only_some_plates_use_sums_only_their_grams() -> None:
    merged = across_plates(
        [
            _plate(SlotNeed(slot_id=1, used_grams=10.0), SlotNeed(slot_id=2, used_grams=5.0)),
            _plate(SlotNeed(slot_id=1, used_grams=20.0)),
            _plate(SlotNeed(slot_id=2, used_grams=7.0)),
        ]
    )

    assert {slot.slot_id: slot.used_grams for slot in merged.slots} == {1: 30.0, 2: 12.0}


def test_a_plate_with_unknown_grams_adds_nothing_to_a_known_total() -> None:
    known_first = across_plates(
        [_plate(SlotNeed(slot_id=1, used_grams=10.0)), _plate(SlotNeed(slot_id=1))]
    )
    unknown_first = across_plates(
        [_plate(SlotNeed(slot_id=1)), _plate(SlotNeed(slot_id=1, used_grams=10.0))]
    )
    all_unknown = across_plates([_plate(SlotNeed(slot_id=1)), _plate(SlotNeed(slot_id=1))])

    assert [slot.used_grams for slot in known_first.slots] == [10.0]
    assert [slot.used_grams for slot in unknown_first.slots] == [10.0]
    assert [slot.used_grams for slot in all_unknown.slots] == [None]


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
async def test_several_plates_read_the_spools_and_printer_once(bambuddy: BambuddyClient) -> None:
    """An all-plates read asks Bambuddy for each plate's slots, and for the spools,
    assignments and printer once, not once per plate (#480)."""
    spools = respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=recording("inventory-spools.json"))
    )
    assignments = respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )
    requirements = respx.get(f"{API}/library/files/62/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements.json"))
    )
    printer = respx.get(f"{API}/printers/1").mock(
        return_value=httpx.Response(200, json=recording("printer.json"))
    )
    remain = respx.get(f"{API}/printers/1/inventory-remain").mock(
        return_value=httpx.Response(200, json=recording("inventory-remain.json"))
    )

    built = await gather_plate_options(
        bambuddy, library_file_id=62, printer_id=1, plate_ids=[1, 2, 3]
    )
    assert len(built) == 3
    # The reads run concurrently, so only which plates were read is pinned, not the order.
    assert sorted(call.request.url.params.get("plate_id") for call in requirements.calls) == [
        "1",
        "2",
        "3",
    ]
    assert (spools.call_count, assignments.call_count) == (1, 1)
    assert (printer.call_count, remain.call_count) == (1, 1)


# --- the failure path (#525): shared reads sequential, plates concurrent -----


@respx.mock
async def test_an_early_spools_failure_raises_and_never_reaches_the_plates(
    bambuddy: BambuddyClient,
) -> None:
    """The shared reads run sequentially and before the plates: a failing ``spools()``
    raises immediately, and neither the rest of the shared reads nor any plate's
    requirements are ever asked for."""
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(500, json={"detail": "spools-boom"})
    )
    assignments = respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )
    requirements = respx.get(f"{API}/library/files/62/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements.json"))
    )
    printer = respx.get(f"{API}/printers/1").mock(
        return_value=httpx.Response(200, json=recording("printer.json"))
    )
    remain = respx.get(f"{API}/printers/1/inventory-remain").mock(
        return_value=httpx.Response(200, json=recording("inventory-remain.json"))
    )

    with pytest.raises(ApiError) as excinfo:
        await gather_plate_options(bambuddy, library_file_id=62, printer_id=1, plate_ids=[1, 2, 3])
    assert "spools-boom" in excinfo.value.detail

    assert not assignments.called
    assert not requirements.called
    assert not printer.called
    assert not remain.called


@respx.mock
async def test_a_printer_failure_is_raised_after_the_plates_are_read(
    bambuddy: BambuddyClient,
) -> None:
    """The printer is read after the plates, as the single-plate path always has."""
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=recording("inventory-spools.json"))
    )
    respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )
    requirements = respx.get(f"{API}/library/files/62/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements.json"))
    )
    respx.get(f"{API}/printers/1").mock(
        return_value=httpx.Response(500, json={"detail": "printer-boom"})
    )
    remain = respx.get(f"{API}/printers/1/inventory-remain").mock(
        return_value=httpx.Response(200, json=recording("inventory-remain.json"))
    )

    with pytest.raises(ApiError) as excinfo:
        await gather_plate_options(bambuddy, library_file_id=62, printer_id=1, plate_ids=[1, 2, 3])
    assert "printer-boom" in excinfo.value.detail

    assert requirements.call_count == 3
    assert not remain.called


@respx.mock
async def test_a_plate_failure_beats_a_printer_failure(
    bambuddy: BambuddyClient,
) -> None:
    """With both failing, the plate's error surfaces, as it does on the single-plate
    path, which reads the plate before the printer (#525 review)."""
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=recording("inventory-spools.json"))
    )
    respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )
    requirements = respx.get(f"{API}/library/files/62/filament-requirements").mock(
        return_value=httpx.Response(500, json={"detail": "plate-boom"})
    )
    printer = respx.get(f"{API}/printers/1").mock(
        return_value=httpx.Response(500, json={"detail": "printer-boom"})
    )

    with pytest.raises(ApiError) as excinfo:
        await gather_plate_options(bambuddy, library_file_id=62, printer_id=1, plate_ids=[1])
    assert "plate-boom" in excinfo.value.detail

    assert requirements.called
    assert not printer.called


@respx.mock
async def test_a_plate_failure_raises_the_lowest_indexed_failing_plates_error(
    bambuddy: BambuddyClient,
) -> None:
    """Two plates fail; the one earliest in ``plate_ids`` (not the lowest plate number)
    is the error that surfaces."""
    respx.get(f"{API}/inventory/spools").mock(
        return_value=httpx.Response(200, json=recording("inventory-spools.json"))
    )
    respx.get(f"{API}/inventory/assignments").mock(
        return_value=httpx.Response(200, json=recording("inventory-assignments.json"))
    )

    def answer(request: httpx.Request) -> httpx.Response:
        plate = request.url.params.get("plate_id")
        if plate == "3":
            return httpx.Response(500, json={"detail": "plate-3-boom"})
        if plate == "1":
            return httpx.Response(500, json={"detail": "plate-1-boom"})
        return httpx.Response(200, json=recording("filament-requirements.json"))

    respx.get(f"{API}/library/files/62/filament-requirements").mock(side_effect=answer)

    # plate_ids lists plate 3 before plate 1, so plate 3's failure — the earlier one in
    # this order, not the lower plate number — must be the one raised.
    with pytest.raises(ApiError) as excinfo:
        await gather_plate_options(bambuddy, library_file_id=62, plate_ids=[3, 2, 1])
    assert "plate-3-boom" in excinfo.value.detail
    assert "plate-1-boom" not in excinfo.value.detail


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
    near, far = "#230000", "#240000"
    near_distance = colour_distance(near, "#000000")
    far_distance = colour_distance(far, "#000000")
    assert near_distance is not None and far_distance is not None
    assert near_distance < COLOUR_MATCH_DISTANCE < far_distance
    black = [_spool(1, "000000FF")]
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


def _spool(spool_id: int, rgba: str | None, material: str = "PLA") -> Spool:
    return Spool.model_validate(
        {
            "id": spool_id,
            "material": material,
            "rgba": rgba,
            "label_weight": 1000,
            "weight_used": 0.0,
        }
    )


# CIEDE2000 test pairs from Sharma, Wu and Dalal (2005), Table 1.
@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, -1.3802, -84.2814), (50.0, 0.0, -82.7485), 1.0000),
        ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
        ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
    ],
)
def test_colour_distance_is_ciede2000(
    first: tuple[float, float, float], second: tuple[float, float, float], expected: float
) -> None:
    assert filaments.ciede2000(first, second) == pytest.approx(expected, abs=1e-4)


def test_a_pure_css_red_pre_selects_the_nearest_real_red_not_a_colourless_spool() -> None:
    """#943: `#FF0000` is about 50 RGB units from every real red filament, so all of
    them were excluded and a spool with no colour won the slot."""
    shelf = [
        _spool(30, None),  # Inland PLA Glow, Fluorescent Rainbow: no colour
        _spool(4, "C12E1FFF"),  # PLA Basic Red
        _spool(6, "D6001CFF", "PETG"),  # PETG Basic Red
        _spool(11, "B50011FF"),  # PLA Translucent Red, 16 away
    ]

    def pick(material: str | None) -> list[int]:
        built = build_options(
            library_file_id=1,
            spools=shelf,
            assignments=[],
            requirements=[SlotNeed(slot_id=1, colour="#FF0000", material=material)],
        )
        return [choice.spool_id for choice in built.suggested]

    assert pick(None) == [6]
    assert pick("PLA") == [4]


def test_a_coloured_slot_with_only_a_colourless_spool_is_left_unchosen() -> None:
    built = build_options(
        library_file_id=1,
        spools=[_spool(30, None)],
        assignments=[],
        requirements=[SlotNeed(slot_id=1, colour="#FF0000")],
    )
    assert built.suggested == []


def test_a_loaded_cobalt_beats_a_closer_cyan_on_the_shelf() -> None:
    """#943's second case: the loaded `#0056B8` was over the RGB cut-off, so the shelf
    cyan was suggested and the dialog asked for a reload."""
    loaded = SpoolAssignment.model_validate(
        {**recording("inventory-assignments.json")[0], "spool_id": 40, "spool": None}
    )
    built = build_options(
        library_file_id=1,
        spools=[_spool(24, "0086D6FF"), _spool(40, "0056B8FF")],
        assignments=[loaded],
        requirements=[SlotNeed(slot_id=1, colour="#0D71E3")],
        printer=printer(),
    )
    assert [choice.spool_id for choice in built.suggested] == [40]
