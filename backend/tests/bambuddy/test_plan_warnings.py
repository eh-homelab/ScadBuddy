"""#2722 — what the chosen spools say in Checks: one not loaded in this printer, and a
plan that mixes material families."""

from __future__ import annotations

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import PrinterStatus, SlotChoice, Spool, SpoolAssignment
from scadbuddy.bambuddy.print_run import plan_warnings
from scadbuddy.bambuddy.trays import tray_spool_id
from tests.bambuddy.conftest import recording


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
