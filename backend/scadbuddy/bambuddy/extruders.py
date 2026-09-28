"""Which extruder each spool feeds, and whether the print can run (#469).

Left to itself (``filament_map_mode: "Auto For Flush"``) the slicer spreads the
filaments across both extruders and slices each for the chosen nozzle size. When the
other side has a different nozzle fitted, the printer pauses at the first layer (HMS
05FE8053, "the left nozzle is not matched with slicing file").

ScadBuddy can't hold a filament to one extruder. Bambu Studio ignores a Manual
``filament_map`` in ``project_settings.config``, and one at plate level in
``model_settings.config`` crashes it (measured 2026-09-28 through Bambuddy's slicer).
So the run refuses, before upload, what would pause the printer:

* a size neither mounted nozzle has and the rack holds no spare of;
* more than one filament on the plates printed when only one side can have the chosen
  size and the other reports another, since the slicer would spread them onto it;
* without the Filament Track Switch, a spool whose AMS is wired to the side with
  another size, when no spare could replace it.

A side counts as having a size when its mounted nozzle is that size, or when the H2C's
hotend rack holds a spare of it: the printer swaps the rack hotend matching the sliced
size onto the extruder at print start (spool-first spec §6; queue item 108 paused with
no spare 0.2 in the rack). Rack ids 0 and 1 are the mounted pair, mirroring
``nozzles``, so spares are the other ids (measured 2026-09-27/28).

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
from scadbuddy.bambuddy.models import NozzleInfo, NozzleRackSlot, PrinterStatus, SpoolAssignment

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


def rack_spares(status: PrinterStatus | None) -> list[NozzleRackSlot]:
    """The hotends in the H2C's rack besides the mounted pair (rack ids 0 and 1 mirror
    ``nozzles``), which the printer swaps on for a sliced size neither mounted one has.
    Empty on a printer without a rack, or one that reports none."""
    if status is None:
        return []
    return [
        slot for slot in status.nozzle_rack if slot.id not in (RIGHT, LEFT) and slot.nozzle_diameter
    ]


def spare_count(status: PrinterStatus | None, size: str) -> int:
    """How many spare hotends of ``size`` the rack holds: each can go on one side."""
    return sum(1 for slot in rack_spares(status) if slot.nozzle_diameter == size)


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
    """Without the switch: each spool whose side has another nozzle size fitted. With a
    spare of ``size`` in the rack the printer can swap it onto that side, so nothing is
    refused here; whether every side the print needs can be made to fit is decided by
    the caller."""
    if spare_count(status, size):
        return []
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

    A side *has* ``size`` when its mounted nozzle is that size, or when the rack holds a
    spare of it for the printer to swap on (each spare serves one side).

    * Neither side has ``size``: refused. A single-nozzle printer whose one nozzle is
      another size: refused too, since nothing is unknown about it. Nozzles the printer
      doesn't report: a warning, since nothing can be checked.
    * Without the switch, a spool on the side with another size: refused, unless a
      spare could replace it.
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
    # The sides a spare from the rack can go on: reported, and of another size. The
    # sides the chosen spools feed come first, so a lone spare serves the side that
    # needs it rather than the right by default.
    resting_on = {side.extruder for side in own if side.extruder is not None}
    swappable = sorted(
        (extruder for extruder in (RIGHT, LEFT) if fitted[extruder] not in (None, size)),
        key=lambda extruder: (extruder not in resting_on, extruder),
    )[: spare_count(status, size)]
    fits = [extruder for extruder in (RIGHT, LEFT) if extruder in matching or extruder in swappable]

    if not fits:
        if right is not None and left is not None:
            sizes = " or ".join(dict.fromkeys((right, left)))
            return ExtruderPlan(
                errors=[
                    f"Neither nozzle is {size} mm: the right has {right} mm and the left "
                    f"{left} mm. Choose {sizes}, or fit a {size} mm nozzle."
                ]
            )
        known_side = RIGHT if right is not None else LEFT if left is not None else None
        if known_side == RIGHT and not two_nozzles(status):
            # A single-nozzle printer: its one nozzle is known and wrong, and there is
            # no other side to be unsure about (review of #538).
            return ExtruderPlan(
                errors=[
                    f"The nozzle is {right} mm, not {size} mm. Choose {right}, or fit a "
                    f"{size} mm nozzle."
                ]
            )
        if known_side is not None:
            known = right if known_side == RIGHT else left
            other_side = LEFT if known_side == RIGHT else RIGHT
            return ExtruderPlan(
                warnings=[
                    FilamentWarning(
                        kind="side-unknown",
                        message=(
                            f"The {_side_word(known_side)} nozzle is {known} mm, not {size} mm, "
                            f"and the printer didn't report the {_side_word(other_side)} one, so "
                            f"nothing checks that the slicer's extruders match them."
                        ),
                    )
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
    if errors or len(fits) == 2:
        return ExtruderPlan(errors=errors)

    only = fits[0]
    other = LEFT if only == RIGHT else RIGHT
    # How that one side has ``size``: mounted, or a spare the printer would swap on.
    has = (
        f"The {_side_word(only)} nozzle is {size} mm"
        if only in matching
        else f"Neither mounted nozzle is {size} mm, and the rack holds one spare {size} mm "
        f"hotend, which can go on one side"
    )
    if fitted[other] is None:
        if only == RIGHT and not two_nozzles(status):
            return ExtruderPlan()
        return ExtruderPlan(
            warnings=[
                FilamentWarning(
                    kind="side-unknown",
                    message=(
                        f"{has}, but the printer didn't report the {_side_word(other)} one, "
                        f"so nothing checks that the slicer's extruders match them."
                    ),
                )
            ]
        )
    if len(used_slots) > 1:
        have = f"a {right} mm nozzle on the right and {left} mm on the left"
        if only in matching:
            spread = (
                f"The slicer spreads a multi-color print across both, and ScadBuddy can't "
                f"keep it on the {_side_word(only)}, so the {fitted[other]} mm side would "
                f"pause it at the first layer."
            )
        else:
            have += f", and one spare {size} mm hotend in the rack"
            spread = (
                "The slicer spreads a multi-color print across both, and ScadBuddy can't "
                "keep it on one side, so the other would pause it at the first layer."
            )
        return ExtruderPlan(
            errors=[
                f"This printer has {have}. {spread} Fit a {size} mm nozzle on both sides, "
                f"or print in one color."
            ]
        )
    picks = (
        f"Only the {_side_word(only)} nozzle is {size} mm, and the slicer picks the "
        f"extruder. If it picks the {_side_word(other)}, the printer pauses at the first "
        f"layer."
        if only in matching
        else f"{has}. The slicer picks the extruder, and the printer swaps the spare onto "
        f"it; a spool wired to the other side can't feed it, and the printer pauses at "
        f"the first layer."
    )
    return ExtruderPlan(warnings=[FilamentWarning(kind="side-unknown", message=picks)])


def with_sides(options: FilamentOptions, status: PrinterStatus | None) -> FilamentOptions:
    """``options`` with each spool loaded in its printer labelled with its side, whether
    that side is only where it rests (the switch is fitted), and the rack's spares."""
    options.track_switch = track_switch(status)
    options.rack = [
        NozzleInfo(nozzle_type=slot.nozzle_type, nozzle_diameter=slot.nozzle_diameter)
        for slot in rack_spares(status)
    ]
    for option in options.spools:
        loaded = option.loaded
        if loaded is None or loaded.printer_id != options.printer_id:
            continue
        option.extruder = extruder_of(loaded.ams_id, loaded.tray_id, status)
        option.side = side_of(option.extruder)
    return options
