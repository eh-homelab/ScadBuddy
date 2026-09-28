"""Which extruder each spool feeds, and whether the print can run (#469).

Left to itself (``filament_map_mode: "Auto For Flush"``) the slicer spreads the
filaments across both extruders and slices each for the chosen nozzle size. When the
other side has a different nozzle fitted, the printer pauses at the first layer (HMS
05FE8053, "the left nozzle is not matched with slicing file").

ScadBuddy can't hold a filament to one extruder. Bambu Studio ignores a Manual
``filament_map`` in ``project_settings.config``, and one at plate level in
``model_settings.config`` crashes it (measured 2026-09-28 through Bambuddy's slicer).
So the run refuses, before upload, what would pause the printer:

* a size neither mounted nozzle has;
* more than one filament on the plates printed when only one side has the chosen size
  and the other reports another, since the slicer would spread them onto it;
* without the Filament Track Switch, a spool whose AMS is wired to the side with
  another size.

With the switch (``fila_switch.installed``) any AMS reaches either nozzle, so a
spool's side is only where it rests between prints.

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

from collections.abc import Collection, Sequence
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


def two_nozzles(status: PrinterStatus | None) -> bool:
    """Whether the printer has a left extruder at all. Bambuddy reports a second
    ``nozzles`` entry, empty, for a single-nozzle printer (X1C, P1S, A1) too, so a left
    side counts only when something names it: a nozzle type or size, an AMS wired to
    it, or the Filament Track Switch."""
    if status is None:
        return False
    if len(status.nozzles) > LEFT and (
        status.nozzles[LEFT].nozzle_type or status.nozzles[LEFT].nozzle_diameter
    ):
        return True
    return LEFT in status.ams_extruder_map.values() or track_switch(status)


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
    """The refusals and the advisories the nozzles and spool sides make."""

    errors: list[str] = field(default_factory=list)
    warnings: list[FilamentWarning] = field(default_factory=list)


def plan_extruders(
    sides: Sequence[SlotSide],
    status: PrinterStatus | None,
    *,
    size: str,
    used_slots: Collection[int],
) -> ExtruderPlan:
    """Refuse a print the nozzles can't take, or say what nobody could check.

    ``used_slots`` are the filaments (1-based) the plate or plates being printed use.

    * Neither side has ``size``: refused. Nozzles the printer doesn't report: a
      warning, since nothing can be checked.
    * Without the switch, a spool on the side with another size: refused.
    * One side has ``size``: more than one filament is refused, because the slicer
      spreads them across both extruders — what paused queue item 108. One filament
      prints with a warning, since the slicer, not ScadBuddy, picks its extruder.
    * One side has ``size`` and the other reports none: a single-nozzle printer prints
      any filament through it; on a two-nozzle printer that side is unknown, so a
      warning, as when nothing is reported.
    * Both have it: either extruder prints any filament.
    """
    own = [side for side in sides if side.slot_id in used_slots]
    fitted = {extruder: fitted_size(status, extruder) for extruder in (RIGHT, LEFT)}
    matching = [extruder for extruder in (RIGHT, LEFT) if fitted[extruder] == size]
    right, left = fitted[RIGHT], fitted[LEFT]

    if not matching:
        if right is not None and left is not None:
            sizes = " or ".join(dict.fromkeys((right, left)))
            return ExtruderPlan(
                errors=[
                    f"Neither nozzle is {size} mm: the right has {right} mm and the left "
                    f"{left} mm. Choose {sizes}, or fit a {size} mm nozzle."
                ]
            )
        return ExtruderPlan(
            warnings=[
                FilamentWarning(
                    kind="side-unknown",
                    message=(
                        "ScadBuddy couldn't read which nozzles the printer has fitted, so "
                        "nothing checks that the slicer's extruders match them."
                    ),
                )
            ]
        )

    errors = [] if track_switch(status) else _spool_errors(own, size, status)
    if errors or len(matching) == 2:
        return ExtruderPlan(errors=errors)

    only = matching[0]
    other = LEFT if only == RIGHT else RIGHT
    if fitted[other] is None:
        if only == RIGHT and not two_nozzles(status):
            return ExtruderPlan()
        return ExtruderPlan(
            warnings=[
                FilamentWarning(
                    kind="side-unknown",
                    message=(
                        f"The {_side_word(only)} nozzle is {size} mm, but the printer didn't "
                        f"report the {_side_word(other)} one, so nothing checks that the "
                        f"slicer's extruders match them."
                    ),
                )
            ]
        )
    if len(used_slots) > 1:
        return ExtruderPlan(
            errors=[
                f"This printer has a {right} mm nozzle on the right and {left} mm on the "
                f"left. The slicer spreads a multi-color print across both, and ScadBuddy "
                f"can't keep it on the {_side_word(only)}, so the {fitted[other]} mm side "
                f"would pause it at the first layer. Fit a {size} mm nozzle on both sides, "
                f"or print in one color."
            ]
        )
    return ExtruderPlan(
        warnings=[
            FilamentWarning(
                kind="side-unknown",
                message=(
                    f"Only the {_side_word(only)} nozzle is {size} mm, and the slicer picks "
                    f"the extruder. If it picks the {_side_word(other)}, the printer pauses "
                    f"at the first layer."
                ),
            )
        ]
    )


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
