"""Which extruder each filament is sliced for, and whether the print can run (#469).

Left to itself (``filament_map_mode: "Auto For Flush"``) the slicer spreads the
filaments across both extruders and slices each for the chosen nozzle size. When the
other side has a different nozzle fitted, the printer pauses at the first layer (HMS
05FE8053, "the left nozzle is not matched with slicing file"). So the run pins every
filament to an extruder whose fitted nozzle is the chosen size, and refuses before
upload when it can't.

What decides a filament's extruder depends on the printer:

* **With the Filament Track Switch** (``fila_switch.installed``) the switch routes any
  AMS to either nozzle, so where a spool is loaded constrains nothing; its inlet is only
  where it rests between prints. Every filament goes to the side with the chosen size;
  when both sides have it, the dialog may pick a side per filament (``extruders``),
  and with none picked the slicer chooses.
* **Without it**, each AMS is wired to one side, and a spool on the side with another
  nozzle fitted cannot print.

Extruder numbers are the printer's **physical** ones: 0 is the right (main) extruder,
1 the left (deputy), and ``PrinterStatus.nozzles`` is indexed the same way. A spool's
side comes from, in order (mirroring upstream Bambuddy's ``utils/fts_routing.py``):

* the external holder, assignment ``ams_id`` 255, whose tray names the side: tray 0 is
  Ext-L, tray 1 Ext-R;
* ``ams_extruder_map``, which a printer without the switch reports;
* ``ams_switch_inlet`` with the switch fitted: inlet A rests on the left extruder, B on
  the right.

Anything else is ``None``, "unknown" — never quietly the right-hand side.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, FilamentWarning
from scadbuddy.bambuddy.models import PrinterStatus, SpoolAssignment

RIGHT = 0
LEFT = 1
EXTERNAL_AMS_ID = 255
#: Upstream ``FTS_INLET_EXTRUDER``, measured on the maintainer's H2C.
FTS_INLET_EXTRUDER: dict[str, int] = {"A": LEFT, "B": RIGHT}

Side = Literal["L", "R"]


UnknownCause = Literal["no-status", "not-loaded", "no-side"]


def track_switch(status: PrinterStatus | None) -> bool:
    """Whether the Filament Track Switch is fitted, so any AMS reaches either nozzle."""
    return status is not None and status.fila_switch is not None and status.fila_switch.installed


def extruder_of(ams_id: int, tray_id: int, status: PrinterStatus | None) -> int | None:
    """The physical extruder the tray feeds (or, with the switch, rests on), or
    ``None`` when it cannot be told. An extruder id other than 0 or 1 is unknown."""
    if status is None:
        return None
    if ams_id == EXTERNAL_AMS_ID:
        return 1 - tray_id if tray_id in (0, 1) else None
    mapped = status.ams_extruder_map.get(str(ams_id))
    if mapped is not None:
        return mapped if mapped in (RIGHT, LEFT) else None
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
    """Where one slot's chosen spool is on the run's printer, and the side it feeds.
    ``unknown`` says why ``extruder`` is ``None``."""

    slot_id: int
    spool_id: int
    ams_id: int | None
    tray_id: int | None
    extruder: int | None
    unknown: UnknownCause | None = None


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
        extruder = extruder_of(row.ams_id, row.tray_id, status) if row else None
        unknown: UnknownCause | None = None
        if extruder is None:
            unknown = "no-status" if status is None else "not-loaded" if row is None else "no-side"
        sides.append(
            SlotSide(
                slot_id=slot.slot_id,
                spool_id=slot.spool_id,
                ams_id=row.ams_id if row else None,
                tray_id=row.tray_id if row else None,
                extruder=extruder,
                unknown=unknown,
            )
        )
    return sides


def _where(side: SlotSide) -> str:
    if side.ams_id == EXTERNAL_AMS_ID:
        return "the external spool holder"
    if side.ams_id is not None and side.ams_id >= 128:
        return "AMS HT"
    return f"AMS {side.ams_id}"


def _unknown_reason(side: SlotSide) -> str:
    if side.unknown == "not-loaded":
        return f"Slot {side.slot_id}'s spool isn't loaded in this printer"
    return f"The printer doesn't say which extruder {_where(side)} feeds (slot {side.slot_id})"


def _spool_errors(sides: Sequence[SlotSide], size: str, status: PrinterStatus | None) -> list[str]:
    """Without the switch: each spool whose side has another nozzle size fitted."""
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


@dataclass(frozen=True)
class ExtruderPlan:
    """What the run does about extruders: the pin (``None`` leaves the slicer to
    choose), the refusals, and the advisories."""

    extruders: tuple[int, ...] | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[FilamentWarning] = field(default_factory=list)


def _filled(by_slot: Mapping[int, int], filament_count: int) -> tuple[int, ...]:
    """One extruder per filament. A filament no slot chose a spool for follows the first
    pinned one: no plate prints it (the resolver refuses a used slot with no spool),
    but the list needs an entry for it."""
    first = by_slot[min(by_slot)]
    return tuple(by_slot.get(index + 1, first) for index in range(filament_count))


def plan_extruders(
    sides: Sequence[SlotSide],
    status: PrinterStatus | None,
    *,
    size: str,
    filament_count: int,
    chosen: Mapping[int, int] | None = None,
) -> ExtruderPlan:
    """Pin every filament to an extruder whose fitted nozzle is ``size``, or say why not.

    ``chosen`` is the dialog's per-slot extruder, where it picked one; each must name a
    side with ``size`` fitted (and, without the switch, the side the spool feeds).

    * Neither side has ``size``: refused. Nozzles the printer doesn't report: no pin,
      and a warning, since nothing can be checked.
    * One side has it: every filament goes there, whatever the spools' sides — never
      Auto, which is what paused queue item 108. Without the switch a spool on the
      other side is refused, and one of unknown side is pinned there with a warning.
    * Both have it: with the switch, the chosen sides, else Auto (either nozzle
      prints any filament). Without it, each spool's own side; one unknown side leaves
      the file on Auto with a warning, which is safe because both nozzles match.
    """
    picks = {slot: extruder for slot, extruder in (chosen or {}).items() if slot <= filament_count}
    own = [side for side in sides if side.slot_id <= filament_count]
    by_slot = {side.slot_id: side for side in own}
    switch = track_switch(status)
    fitted = {extruder: fitted_size(status, extruder) for extruder in (RIGHT, LEFT)}
    matching = [extruder for extruder in (RIGHT, LEFT) if fitted[extruder] == size]

    errors: list[str] = []
    for slot, extruder in sorted(picks.items()):
        if extruder not in matching:
            there = fitted.get(extruder)
            errors.append(
                f"Slot {slot} can't print on the {_side_word(extruder)}: "
                + (
                    f"its nozzle is {there} mm and this print is sliced for {size} mm."
                    if there
                    else "the printer doesn't report the nozzle fitted there."
                )
            )
            continue
        side = by_slot.get(slot)
        wired = side.extruder if side is not None and not switch else None
        if side is not None and wired is not None and wired != extruder:
            errors.append(
                f"Slot {slot}'s spool ({_where(side)}) feeds the {_side_word(wired)} "
                f"extruder, so it can't print on the {_side_word(extruder)}."
            )

    if not matching:
        right, left = fitted[RIGHT], fitted[LEFT]
        if right is not None and left is not None:
            sizes = " or ".join(dict.fromkeys((right, left)))
            errors.append(
                f"Neither nozzle is {size} mm: the right has {right} mm and the left "
                f"{left} mm. Choose {sizes}, or fit a {size} mm nozzle."
            )
            return ExtruderPlan(errors=errors)
        if errors:
            return ExtruderPlan(errors=errors)
        return ExtruderPlan(
            warnings=[
                FilamentWarning(
                    kind="side-unknown",
                    message=(
                        "ScadBuddy couldn't read which nozzles the printer has fitted, so the "
                        "slicer chooses the extruder for every color."
                    ),
                )
            ]
        )

    if not switch:
        errors += _spool_errors(own, size, status)
    if errors:
        return ExtruderPlan(errors=errors)

    if len(matching) == 1:
        only = matching[0]
        warnings = (
            []
            if switch
            else [
                FilamentWarning(
                    kind="side-unknown",
                    slot_id=side.slot_id,
                    message=(
                        f"{_unknown_reason(side)}, so it's sliced for the {_side_word(only)} "
                        f"extruder, the one with the {size} mm nozzle. Load it where it feeds "
                        f"the {_side_word(only)}."
                    ),
                )
                for side in own
                if side.extruder is None and side.slot_id not in picks
            ]
        )
        return ExtruderPlan(extruders=(only,) * filament_count, warnings=warnings)

    if switch:
        if not picks:
            return ExtruderPlan()
        # A filament left on Auto beside a picked one rests where its inlet does, else
        # follows the first pick: both nozzles are the chosen size, so either prints it.
        pinned = {side.slot_id: side.extruder for side in own if side.extruder is not None} | picks
        return ExtruderPlan(extruders=_filled(pinned, filament_count))

    unknown = [side for side in own if side.extruder is None and side.slot_id not in picks]
    if unknown or not own:
        return ExtruderPlan(
            warnings=[
                FilamentWarning(
                    kind="side-unknown",
                    slot_id=side.slot_id,
                    message=(
                        f"{_unknown_reason(side)}. Both nozzles are {size} mm, so the slicer "
                        "chooses the extruder for every color."
                    ),
                )
                for side in unknown
            ]
        )
    pinned = {side.slot_id: side.extruder for side in own if side.extruder is not None} | picks
    return ExtruderPlan(extruders=_filled(pinned, filament_count))


def with_sides(options: FilamentOptions, status: PrinterStatus | None) -> FilamentOptions:
    """``options`` with each spool loaded in its printer labelled with its side, and
    whether that side is only where it rests (the switch is fitted)."""
    options.track_switch = track_switch(status)
    for option in options.spools:
        loaded = option.loaded
        if loaded is None or loaded.printer_id != options.printer_id:
            continue
        option.extruder = extruder_of(loaded.ams_id, loaded.tray_id, status)
        option.side = side_of(option.extruder)
    return options
