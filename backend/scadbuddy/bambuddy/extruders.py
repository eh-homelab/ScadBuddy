"""Which extruder a chosen spool feeds, and what that means for the slice (#469).

On a dual-nozzle printer every AMS is wired to one side, so a spool's extruder is fixed
by the tray it is loaded in, not chosen by the slicer. Left to itself (``filament_map_mode:
"Auto For Flush"``) the slicer spreads the filaments across both extruders and slices
each for the chosen nozzle size. When the other side has a different nozzle fitted, the
printer pauses at the first layer (HMS 05FE8053, "the left nozzle is not matched with
slicing file").

So the run reads each spool's side from Bambuddy, refuses a spool whose side has the
wrong nozzle before anything is uploaded, and pins the slice to the sides otherwise.

Extruder numbers are the printer's **physical** ones: 0 is the right (main) extruder,
1 the left (deputy), and ``PrinterStatus.nozzles`` is indexed the same way. The side
comes from, in order (mirroring upstream Bambuddy's ``utils/fts_routing.py``):

* the external holder, assignment ``ams_id`` 255, whose tray names the side: tray 0 is
  Ext-L, tray 1 Ext-R;
* ``ams_extruder_map``, which a printer without the Filament Track Switch reports;
* ``ams_switch_inlet`` with the switch fitted: inlet A feeds the left extruder, B the
  right, in the switch's rest position.

Anything else is ``None``, "unknown" — never quietly the right-hand side.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, FilamentWarning
from scadbuddy.bambuddy.models import PrinterStatus, SpoolAssignment

RIGHT = 0
LEFT = 1
EXTERNAL_AMS_ID = 255
#: Upstream ``FTS_INLET_EXTRUDER``, measured on the maintainer's H2C.
FTS_INLET_EXTRUDER: dict[str, int] = {"A": LEFT, "B": RIGHT}

Side = Literal["L", "R"]


def extruder_of(ams_id: int, tray_id: int, status: PrinterStatus | None) -> int | None:
    """The physical extruder the tray feeds, or ``None`` when it cannot be told."""
    if status is None:
        return None
    if ams_id == EXTERNAL_AMS_ID:
        return 1 - tray_id if tray_id in (0, 1) else None
    mapped = status.ams_extruder_map.get(str(ams_id))
    if mapped is not None:
        return mapped
    inlet = status.ams_switch_inlet.get(str(ams_id))
    return FTS_INLET_EXTRUDER.get(inlet.upper()) if inlet else None


def side_of(extruder: int | None) -> Side | None:
    """The letter the printer and Bambuddy show for a side."""
    if extruder == RIGHT:
        return "R"
    if extruder == LEFT:
        return "L"
    return None


def _side_word(extruder: int) -> str:
    return "left" if extruder == LEFT else "right"


def fitted_size(status: PrinterStatus | None, extruder: int) -> str | None:
    """The nozzle size mounted on ``extruder``, when the printer reports one."""
    if status is None or extruder >= len(status.nozzles):
        return None
    return status.nozzles[extruder].nozzle_diameter or None


@dataclass(frozen=True)
class SlotSide:
    """Where one slot's chosen spool is on the run's printer, and the side it feeds."""

    slot_id: int
    spool_id: int
    ams_id: int | None
    tray_id: int | None
    extruder: int | None


def slot_sides(
    plan: FilamentPlan,
    assignments: list[SpoolAssignment],
    status: PrinterStatus | None,
    *,
    printer_id: int,
) -> list[SlotSide]:
    """One row per planned slot, in slot order. A spool not loaded in *this* printer
    has no side here. The last assignment of a spool wins, as in the picker: Bambuddy
    keeps history rows."""
    here = {row.spool_id: row for row in assignments if row.printer_id == printer_id}
    sides: list[SlotSide] = []
    for slot in sorted(plan.slots, key=lambda choice: choice.slot_id):
        row = here.get(slot.spool_id)
        sides.append(
            SlotSide(
                slot_id=slot.slot_id,
                spool_id=slot.spool_id,
                ams_id=row.ams_id if row else None,
                tray_id=row.tray_id if row else None,
                extruder=extruder_of(row.ams_id, row.tray_id, status) if row else None,
            )
        )
    return sides


def _where(side: SlotSide) -> str:
    if side.ams_id == EXTERNAL_AMS_ID:
        return "the external spool holder"
    if side.ams_id is not None and side.ams_id >= 128:
        return "AMS HT"
    return f"AMS {side.ams_id}"


def mismatch_errors(
    sides: Sequence[SlotSide], size: str, status: PrinterStatus | None
) -> list[str]:
    """Why each slot cannot print at ``size``: its spool feeds a side whose mounted
    nozzle is another size. A slot with no known side, or a side whose nozzle the
    printer does not report, is not refused — the slicer chooses there."""
    errors: list[str] = []
    for side in sides:
        if side.extruder is None:
            continue
        fitted = fitted_size(status, side.extruder)
        if fitted is None or fitted == size:
            continue
        other = RIGHT if side.extruder == LEFT else LEFT
        way_out = (
            f"Pick a spool on the {_side_word(other)}, or choose {fitted}."
            if fitted_size(status, other) == size
            else f"Choose {fitted}, or fit a {size} mm nozzle on the {_side_word(side.extruder)}."
        )
        errors.append(
            f"Slot {side.slot_id}'s spool ({_where(side)}, {_side_word(side.extruder)}) is on "
            f"the {fitted} mm nozzle; this print is sliced for {size} mm. {way_out}"
        )
    return errors


def unknown_side_warnings(sides: Sequence[SlotSide]) -> list[FilamentWarning]:
    return [
        FilamentWarning(
            kind="side-unknown",
            slot_id=side.slot_id,
            message=(
                f"Slot {side.slot_id}'s spool isn't in an AMS of this printer, so the slicer "
                "chooses which extruder prints every slot."
            ),
        )
        for side in sides
        if side.extruder is None
    ]


def extruder_map(sides: Sequence[SlotSide], *, filament_count: int) -> tuple[int, ...] | None:
    """One physical extruder per filament of the file, or ``None`` to leave the choice
    to the slicer — which is what any slot with an unknown side means for the whole
    file, since one pinned filament and one auto-placed one is not a mode the slicer
    has. A filament no slot chose a spool for follows the first pinned one: no plate
    prints it (the resolver refuses a used slot with no spool), but the list needs an
    entry for it."""
    by_slot: dict[int, int] = {}
    for side in sides:
        if side.slot_id > filament_count:
            continue
        if side.extruder is None:
            return None
        by_slot[side.slot_id] = side.extruder
    if not by_slot:
        return None
    first = next(iter(by_slot.values()))
    return tuple(by_slot.get(index + 1, first) for index in range(filament_count))


def with_sides(options: FilamentOptions, status: PrinterStatus | None) -> FilamentOptions:
    """``options`` with each spool loaded in its printer labelled with its side."""
    for option in options.spools:
        loaded = option.loaded
        if loaded is None or loaded.printer_id != options.printer_id:
            continue
        option.extruder = extruder_of(loaded.ams_id, loaded.tray_id, status)
        option.side = side_of(option.extruder)
    return options
