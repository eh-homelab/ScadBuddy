"""Which extruder each spool feeds, for the picker's side labels (#469).

The print run does not check the mounted nozzles (#768). It used to refuse, before
upload, a size neither mounted nozzle had, a multi-colour print when only one side had
the sliced size, and a spool on the side with another size, and to warn when a side was
unknown or a mounted nozzle was High Flow. Those rested on the premise that the slicer
spreads a multi-colour print across both extruders and the printer pauses on the side
whose nozzle differs. Measured by the maintainer's test print, 2026-09-29: a two-colour
print sliced for 0.2 mm printed through the one 0.2 mm nozzle while the other extruder
had a different size fitted. The printer handles its nozzles itself, and the H2C swaps
hotends from its rack. That print was sliced in Bambu Studio, which saved every filament
on one extruder; the run writes the same map
(:func:`scadbuddy.render.bambu3mf.one_extruder_map`), which Bambuddy's headless slicer
was measured not to follow on 2026-09-28 (#745).

One advisory stays, by the owner's ruling on #723 (queue item 149 paused on it): a
mounted High Flow nozzle of the sliced size is warned about (:func:`high_flow_warnings`),
never refused, since a print may be set up before its nozzle is fitted.

What is left besides is a label: the side each loaded spool feeds, so the picker can show it as
the printer and Bambuddy do. With the Filament Track Switch (``fila_switch.installed``)
any AMS reaches either nozzle, so a spool's side is only where it rests between prints.

Extruder numbers are the printer's **physical** ones: 0 is the right (main) extruder,
1 the left (deputy), and ``PrinterStatus.nozzles`` is indexed the same way. A spool's
side comes from, in order (mirroring upstream Bambuddy's ``utils/fts_routing.py``):

* the external holder, assignment ``ams_id`` 255, whose tray names the side: tray 0 is
  Ext-L, tray 1 Ext-R (on a printer without a left extruder it feeds the one nozzle);
* ``ams_extruder_map``, which a printer without the switch reports;
* ``ams_switch_inlet`` with the switch fitted: inlet A rests on the left extruder, B on
  the right.

Anything else is ``None``, "unknown" — never quietly the right-hand side.
"""

from __future__ import annotations

from typing import Literal

from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentWarning
from scadbuddy.bambuddy.models import PrinterStatus

RIGHT = 0
LEFT = 1
EXTERNAL_AMS_ID = 255
#: Upstream ``FTS_INLET_EXTRUDER``, measured on the maintainer's H2C.
FTS_INLET_EXTRUDER: dict[str, int] = {"A": LEFT, "B": RIGHT}

Side = Literal["L", "R"]


def track_switch(status: PrinterStatus | None) -> bool:
    """Whether the Filament Track Switch is fitted, so any AMS reaches either nozzle."""
    return status is not None and status.fila_switch is not None and status.fila_switch.installed


def extruder_of(ams_id: int, tray_id: int, status: PrinterStatus | None) -> int | None:
    """The physical extruder the tray feeds (or, with the switch, rests on), or
    ``None`` when it cannot be told. An extruder id other than 0 or 1 is unknown."""
    if status is None:
        return None
    if ams_id == EXTERNAL_AMS_ID:
        if not two_nozzles(status):
            return RIGHT
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


def _side_word(extruder: int) -> str:
    return "left" if extruder == LEFT else "right"


def fitted_size(status: PrinterStatus | None, extruder: int) -> str | None:
    """The nozzle size mounted on ``extruder``, when the printer reports one."""
    if status is None or extruder >= len(status.nozzles):
        return None
    return status.nozzles[extruder].nozzle_diameter or None


def fitted_high_flow(status: PrinterStatus | None, extruder: int) -> bool:
    """Whether the nozzle mounted on ``extruder`` is High Flow (``HH01``; ``HS01`` is
    standard), the same reading as :mod:`scadbuddy.bambuddy.hardware`."""
    if status is None or extruder >= len(status.nozzles):
        return False
    nozzle_type = status.nozzles[extruder].nozzle_type or ""
    return len(nozzle_type) > 1 and nozzle_type[1] == "H"


def high_flow_warnings(status: PrinterStatus | None, size: str) -> list[FilamentWarning]:
    """A warning for each mounted High Flow nozzle of ``size`` (#723), never a refusal.

    ScadBuddy slices for standard nozzles until High Flow slicing works (#484), and the
    printer paused a print at the first layer on a side whose nozzle type the slice
    didn't match (queue item 149). A print may be set up before its nozzle is fitted, so
    the owner chose a warning; the dialog shows it in Advanced mode only (#772)."""
    return [
        FilamentWarning(
            kind="hf-unsupported",
            message=(
                f"The {_side_word(extruder)} nozzle is High Flow. ScadBuddy slices for "
                "standard nozzles until High Flow slicing is supported (#484), so if the "
                f"print uses the {_side_word(extruder)}, the printer pauses at the first "
                f'layer ("the {_side_word(extruder)} nozzle is not matched with slicing '
                f'file"). Fit a standard nozzle there before it starts.'
            ),
        )
        for extruder in (RIGHT, LEFT)
        if fitted_size(status, extruder) == size and fitted_high_flow(status, extruder)
    ]


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
