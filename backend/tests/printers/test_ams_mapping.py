"""``ams_mapping`` / ``ams_mapping2`` from a native plan's trays (#3818 tier A, Task 7).
The vectors are ``fixtures/ams-mapping-vectors.json``; the module cites each encoding."""

import json
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import SlotChoice
from scadbuddy.bambuddy.trays import EXTERNAL, tray_spool_id
from scadbuddy.printers.ams_mapping import AmsMapping, AmsSlot, ams_mapping

VECTORS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "ams-mapping-vectors.json").read_text()
)["vectors"]


def _plan(*slots: tuple[int, int, int]) -> FilamentPlan:
    return FilamentPlan(
        slots=[
            SlotChoice(slot_id=slot_id, spool_id=tray_spool_id(ams_id, tray_id))
            for slot_id, ams_id, tray_id in slots
        ]
    )


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_the_vectors(vector: dict[str, Any]) -> None:
    plan = _plan(*((s["slot_id"], s["ams_id"], s["tray_id"]) for s in vector["slots"]))
    mapping = ams_mapping(plan, vector["filament_count"], dual_nozzle=vector["dual_nozzle"])
    assert mapping == AmsMapping(
        ams_mapping=vector["ams_mapping"],
        ams_mapping2=[AmsSlot(**slot) for slot in vector["ams_mapping2"]],
        use_ams=vector["use_ams"],
    )


def test_the_wire_shape() -> None:
    """What 3a's ``ProjectFile`` sends beside ``ams_mapping``: a list of plain objects."""
    mapping = ams_mapping(_plan((1, 1, 2)), 2, dual_nozzle=False)
    assert mapping.model_dump() == {
        "ams_mapping": [6, -1],
        "ams_mapping2": [{"ams_id": 1, "slot_id": 2}, {"ams_id": 255, "slot_id": 255}],
        "use_ams": True,
    }


def test_either_external_holder_is_the_main_one_on_a_single_nozzle() -> None:
    """Bambuddy sends 255 for any external spool on a single-nozzle printer, whichever
    virtual tray id it reports (``bambu_mqtt.py`` at ``0d427510``, ``:6002-6015``)."""
    mapping = ams_mapping(_plan((1, EXTERNAL, 1)), 1, dual_nozzle=False)
    assert mapping.ams_mapping2 == [AmsSlot(ams_id=255, slot_id=0)]


def test_an_inventory_spool_is_refused() -> None:
    """A native plan is trays only (D1): a positive id is a Bambuddy spool."""
    plan = FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=12)])
    with pytest.raises(ValueError, match="spool 12"):
        ams_mapping(plan, 1, dual_nozzle=False)


@pytest.mark.parametrize("slot_id", [0, 3])
def test_a_slot_outside_the_plate_is_refused(slot_id: int) -> None:
    with pytest.raises(ValueError, match=f"slot {slot_id}"):
        ams_mapping(_plan((slot_id, 0, 0)), 2, dual_nozzle=False)


def test_a_slot_chosen_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="slot 1"):
        ams_mapping(_plan((1, 0, 0), (1, 0, 1)), 1, dual_nozzle=False)


@pytest.mark.parametrize(
    ("ams_id", "tray_id", "dual_nozzle"),
    [(0, 4, False), (128, 1, True), (EXTERNAL, 2, True)],
    ids=["AMS slot 5", "HT slot 2", "a third external holder"],
)
def test_a_tray_no_printer_has_is_refused(ams_id: int, tray_id: int, dual_nozzle: bool) -> None:
    with pytest.raises(ValueError, match="no such tray"):
        ams_mapping(_plan((1, ams_id, tray_id)), 1, dual_nozzle=dual_nozzle)
