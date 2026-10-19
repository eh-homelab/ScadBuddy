"""#2722 — what the chosen spools say in Checks: one not loaded in this printer, and a
plan that mixes material families."""

from __future__ import annotations

from scadbuddy.bambuddy.extruders import LEFT, RIGHT
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import PrinterStatus, SlotChoice, Spool, SpoolAssignment
from scadbuddy.bambuddy.nozzle_plan import NozzlePlan, PlanInput, PlannedSlot
from scadbuddy.bambuddy.print_run import inlet_warnings, plan_warnings
from scadbuddy.bambuddy.trays import tray_spool_id
from tests.bambuddy.conftest import recording
from tests.bambuddy.test_extruders import fts_status


def spool(spool_id: int, material: str = "PLA", colour: str = "Black") -> Spool:
    return Spool.model_validate(
        {
            "id": spool_id,
            "material": material,
            "brand": "Inland",
            "color_name": colour,
            "rgba": "1A1A1AFF",
            "label_weight": 1000,
            "weight_used": 0.0,
        }
    )


def loaded(spool_id: int, tray_id: int, printer_id: int = 1) -> SpoolAssignment:
    return SpoolAssignment.model_validate(
        {
            **recording("inventory-assignments.json")[0],
            "id": 100 + spool_id,
            "spool_id": spool_id,
            "printer_id": printer_id,
            "printer_name": f"printer {printer_id}",
            "tray_id": tray_id,
            "spool": None,
        }
    )


def plan(*spool_ids: int) -> FilamentPlan:
    return FilamentPlan(
        slots=[
            SlotChoice(slot_id=slot, spool_id=spool_id)
            for slot, spool_id in enumerate(spool_ids, start=1)
        ]
    )


def kinds(*spool_ids: int, assignments: list[SpoolAssignment]) -> list[tuple[str, int | None]]:
    shelf = [spool(1), spool(2, "TPU 95A"), spool(3, "PLA Matte", "Green"), spool(4)]
    found = plan_warnings(
        plan(*spool_ids), (shelf, assignments), printer_id=1, printer_name=None, status=None
    )
    return [(warning.kind, warning.slot_id) for warning in found]


def test_the_mosaic_plan_of_2461_is_flagged_twice() -> None:
    """A loaded PLA, an unloaded TPU 95A and a loaded PLA: Checks said nothing."""
    found = plan_warnings(
        plan(1, 2, 3),
        (
            [spool(1), spool(2, "TPU 95A"), spool(3, "PLA Matte", "Green")],
            [loaded(1, 0), loaded(3, 1)],
        ),
        printer_id=1,
        printer_name=None,
        status=None,
    )
    assert [(w.kind, w.slot_id) for w in found] == [("not-loaded", 2), ("mixed-materials", None)]
    assert found[1].message.startswith("This print mixes PLA and TPU.")


def test_three_families_are_named_in_order() -> None:
    """#2733 review: the list wording past two families."""
    found = plan_warnings(
        plan(1, 2, 3),
        (
            [spool(1), spool(2, "PETG"), spool(3, "TPU 95A")],
            [loaded(1, 0), loaded(2, 1), loaded(3, 2)],
        ),
        printer_id=1,
        printer_name=None,
        status=None,
    )
    assert [w.kind for w in found] == ["mixed-materials"]
    assert found[0].message.startswith("This print mixes PLA, PETG and TPU.")


def test_one_family_all_loaded_says_nothing() -> None:
    # PLA and PLA Matte are one family.
    assert kinds(1, 3, assignments=[loaded(1, 0), loaded(3, 1)]) == []


def test_a_spool_loaded_in_another_printer_is_not_loaded_here() -> None:
    assert kinds(1, 4, assignments=[loaded(1, 0), loaded(4, 2, printer_id=2)]) == [
        ("not-loaded", 2)
    ]


def test_a_tray_chosen_as_itself_is_loaded_and_counts_by_its_material() -> None:
    """AMS 0 slot 1 holds PLA in the recorded status; beside a TPU spool it mixes."""
    status = PrinterStatus.model_validate(recording("printer-status-rack.json"))
    tray_plan = FilamentPlan(
        slots=[
            SlotChoice(slot_id=1, spool_id=2),
            SlotChoice(slot_id=2, spool_id=tray_spool_id(0, 0)),
        ]
    )
    shelf = [spool(2, "TPU 95A")]
    found = plan_warnings(
        tray_plan, (shelf, [loaded(2, 3)]), printer_id=1, printer_name=None, status=status
    )
    assert [w.kind for w in found] == ["mixed-materials"]
    assert found[0].message.startswith("This print mixes TPU and PLA.")


def test_a_spool_in_another_printer_is_named_as_the_picker_names_it() -> None:
    """#2733 review: the check names the chosen printer, as the picker does."""
    found = plan_warnings(
        plan(4),
        ([spool(4)], [loaded(4, 2, printer_id=2)]),
        printer_id=1,
        printer_name="3DP-31B-598",
        status=None,
    )
    assert [w.kind for w in found] == ["not-loaded"]
    assert "is loaded in printer 2, not in 3DP-31B-598" in found[0].message


def test_a_spool_the_inventory_lacks_or_archived_says_nothing_here() -> None:
    """#2733 review: a gone or archived spool is the run's refusal, by name; here it
    neither reads as not loaded nor adds a material to mix."""
    archived = spool(2, "TPU 95A").model_copy(update={"archived_at": "2026-10-01T00:00:00Z"})
    found = plan_warnings(
        plan(1, 2, 99),
        ([spool(1), archived], [loaded(1, 0)]),
        printer_id=1,
        printer_name=None,
        status=None,
    )
    assert found == []


def sides(*placed: tuple[int, str]) -> NozzlePlan:
    return NozzlePlan.model_validate(
        {
            "slots": [
                {"slot_id": slot, "side": side, "name": f"slot {slot}"} for slot, side in placed
            ],
            "size": "0.4",
            "left_flow": "standard",
            "right_flow": "standard",
            "summary": "",
        }
    )


def mixed_message(nozzle_plan: NozzlePlan | None) -> str:
    found = plan_warnings(
        plan(1, 2),
        ([spool(1), spool(2, "TPU 95A")], [loaded(1, 0), loaded(2, 1)]),
        printer_id=1,
        printer_name=None,
        status=None,
        nozzle_plan=nozzle_plan,
    )
    assert [w.kind for w in found] == ["mixed-materials"]
    return found[0].message


def test_a_nozzle_of_its_own_is_said_only_where_the_plan_puts_each_family_on_one() -> None:
    """#2733 review: the sentence was said whatever the plan, and could be wrong."""
    assert "a nozzle of its own" in mixed_message(sides((1, "L"), (2, "R")))
    # No plan (not laid out, or no sides offered): it only says they mix.
    assert mixed_message(None) == "This print mixes PLA and TPU. Check that is what you meant."
    # Both on one side, as a hand-picked side can put them.
    assert "a nozzle of its own" not in mixed_message(sides((1, "L"), (2, "L")))
    # A slot the plan does not place.
    assert "a nozzle of its own" not in mixed_message(sides((1, "L")))


def test_an_offline_printers_tray_adds_no_material_to_the_mix() -> None:
    """#2733 review: with no status a tray chosen as itself has no known material, so a
    TPU spool beside it is not called a mix. Silence is intended: the run reads the
    tray when it has a status, and nothing here can say what the tray holds."""
    tray_plan = FilamentPlan(
        slots=[
            SlotChoice(slot_id=1, spool_id=2),
            SlotChoice(slot_id=2, spool_id=tray_spool_id(0, 0)),
        ]
    )
    found = plan_warnings(
        tray_plan,
        ([spool(2, "TPU 95A")], [loaded(2, 3)]),
        printer_id=1,
        printer_name=None,
        status=None,
    )
    assert found == []


def _sides(*sides: str, switch: bool = True) -> NozzlePlan:
    """A plan putting slot 1, 2, … on these sides, named "Glow", "Black", …."""
    names = ["Glow", "Black", "Green"]
    return NozzlePlan(
        slots=[
            PlannedSlot(slot_id=slot, side=side, name=names[slot - 1], by_hand=True)  # type: ignore[arg-type]
            for slot, side in enumerate(sides, start=1)
        ],
        size="0.4",
        left_flow="high_flow",
        right_flow="high_flow",
        track_switch=switch,
        summary="",
    )


def _resting(*rests: int | None) -> list[PlanInput]:
    return [PlanInput(slot, rests=rest) for slot, rest in enumerate(rests, start=1)]


def test_an_inlet_split_across_the_extruders_names_the_spool_to_move() -> None:
    """#4272, print #355: glow and black both on inlet A (the left's), glow put on the
    right by hand. Glow is named, with inlet B to move it to."""
    [warning] = inlet_warnings(_sides("R", "L"), _resting(LEFT, LEFT))

    assert warning.kind == "inlet-shared"
    assert warning.message.startswith(
        "Glow prints on the right but is fed through Track Switch inlet A, which also "
        "feeds Black on the left."
    )
    assert "move Glow to an AMS on inlet B, or print it on the left" in warning.message


def test_each_inlet_on_its_own_side_says_nothing() -> None:
    assert inlet_warnings(_sides("L", "R"), _resting(LEFT, RIGHT)) == []


def test_a_whole_inlet_moved_to_the_other_side_says_nothing() -> None:
    """Both of inlet A's spools on the right, inlet B's on the left: no inlet feeds both."""
    assert inlet_warnings(_sides("R", "R", "L"), _resting(LEFT, LEFT, RIGHT)) == []


def test_no_switch_one_side_or_an_unknown_rest_says_nothing() -> None:
    assert inlet_warnings(_sides("R", "L", switch=False), _resting(LEFT, LEFT)) == []
    assert inlet_warnings(_sides("L", "L"), _resting(LEFT, LEFT)) == []
    assert inlet_warnings(_sides("R", "L"), _resting(None, LEFT)) == []


def test_checks_reads_each_spools_inlet_from_the_printer() -> None:
    """The wiring: both spools loaded in AMS 2, which the switch's inlet A feeds."""
    shelf = [spool(1, colour="Glow"), spool(2)]
    on_inlet_a = [
        loaded(1, 0).model_copy(update={"ams_id": 2}),
        loaded(2, 1).model_copy(update={"ams_id": 2}),
    ]
    found = plan_warnings(
        plan(1, 2),
        (shelf, on_inlet_a),
        printer_id=1,
        printer_name=None,
        status=fts_status(),
        nozzle_plan=_sides("R", "L"),
    )
    assert [warning.kind for warning in found] == ["inlet-shared"]
