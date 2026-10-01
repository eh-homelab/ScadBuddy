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
was measured not to follow on 2026-09-28 (#745). What the slicer does follow is which
extruders have the nozzle, and the run now states that (:func:`slicer_nozzle_stats`,
#834).

One advisory stays, by the owner's ruling on #723 (queue item 149 paused on it) and #797:
a mounted High Flow nozzle of the chosen size is warned about (:func:`high_flow_warnings`),
whatever flow is chosen, since the slice is always Standard flow until Bambuddy supports
High Flow presets (#484), never refused, since a print may be set up before its nozzle is
fitted.

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

from collections.abc import Sequence
from typing import Literal

from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentWarning
from scadbuddy.bambuddy.models import NozzleChoice, PrinterStatus

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
    return status.nozzles[extruder].high_flow


def high_flow_warnings(
    status: PrinterStatus | None, nozzles: Sequence[NozzleChoice]
) -> list[FilamentWarning]:
    """A warning for each mounted High Flow nozzle of the chosen size, whatever flow is
    chosen (#723, #797), never a refusal. Only ``nozzles[0].size`` is read here: the
    slice is always Standard flow (#484), so it pauses on a mounted High Flow nozzle
    regardless of the flow the choices ask for.

    The printer paused a print at the first layer on a side whose nozzle type the slice
    didn't match (queue item 149). A print may be set up before its nozzle is fitted, so
    the owner chose a warning, and the dialog shows it in Simple and Advanced mode alike.
    An unreadable status knows no nozzle, so it warns nothing."""
    if not nozzles:
        return []
    size = nozzles[0].size
    return [
        FilamentWarning(
            kind="hf-mounted",
            message=(
                f"The {_side_word(extruder)} nozzle is High Flow and this print is sliced "
                "for Standard flow (High Flow slicing isn't supported yet, #484), so if it "
                f"prints on the {_side_word(extruder)}, the printer pauses at the first "
                f'layer ("the {_side_word(extruder)} nozzle is not matched with slicing '
                'file"). Fit a standard nozzle there before it starts.'
            ),
        )
        for extruder in (RIGHT, LEFT)
        if fitted_size(status, extruder) == size and fitted_high_flow(status, extruder)
    ]


#: The side the H2C's nozzle rack swaps hotends onto: physical extruder 0, the right.
#: Upstream Bambuddy measured it from Bambu Studio's own dispatch (``bambu_mqtt.py``,
#: 2026-08-14), and the H2C preset agrees: ``extruder_max_nozzle_count`` ["1", "6"] names
#: the slicer's second extruder, which ``physical_extruder_map`` ["1", "0"] makes
#: physical 0. ``PrinterStatus.nozzle_rack`` ids 0 and 1 are the two mounted hotends,
#: and the higher ids are the rack's positions.
RACK_SIDE = RIGHT
#: The printer's sides in the slicer's extruder order: the H2C preset's
#: ``physical_extruder_map`` is ["1", "0"], so the slicer's extruder 1 is the left.
SLICER_ORDER = (LEFT, RIGHT)


def _nozzles_on(status: PrinterStatus, extruder: int, size: str) -> list[bool]:
    """One bool per ``size`` nozzle that ``extruder`` can print with, True where that
    nozzle is High Flow and False where it is standard.

    The nozzles counted are the one mounted on ``extruder`` and, on the rack side, each
    spare in the rack. An empty list means the side has no nozzle of ``size``.
    """
    found = []
    if fitted_size(status, extruder) == size:
        found.append(fitted_high_flow(status, extruder))
    if extruder == RACK_SIDE:
        found += [
            slot.high_flow
            for slot in status.nozzle_rack
            if slot.id > LEFT and slot.nozzle_diameter == size
        ]
    return found


def slicer_nozzle_stats(status: PrinterStatus | None, size: str) -> list[str] | None:
    """``extruder_nozzle_stats`` naming only the side that has a ``size`` nozzle (#834).

    Bambu's H2C presets state one size on both extruders, and the slicer's "Auto For
    Flush" grouping spreads the filaments over every extruder its nozzle stats offer —
    the ``filament_map`` in the 3MF is not followed (#745). Queue item 159 was sliced
    so, with a filament on the left 0.4 as if it were a 0.2, and paused with "the left
    nozzle is not matched with slicing file". Bambu Studio avoids that by stating what
    each side has: archive 36, which printed, carries ["Standard#0|High Flow#0",
    "Standard#1"] and every filament on the right. Measured against the deployed slicer
    (2026-09-30), this key in the 3MF steers its grouping the same way.

    A standard nozzle of the size is preferred, since ScadBuddy slices standard flow
    (#484); a High Flow one of the size still beats a side without the size, and #723
    warns of it. That side is still stated as ``Standard#1``, never ``High Flow#1``:
    the file is sliced with a Standard process, and the deployed slicer
    (bambu-studio-api bambuddy-1.2.5.6, 2026-09-30) treats the label as a count only.
    ["Standard#0", "High Flow#1"], Studio's ["Standard#0|High Flow#0",
    "Standard#0|High Flow#1"] and ["Standard#0", "Standard#1"] all slice, all put every
    filament in one group on the right with ``volume_type="Standard"``, and all come
    back rewritten to ["Standard#0", "Standard#1"]; the G-code differs only in its time
    estimates. The mirrored left case is the same. The flow mismatch is left to the
    ``hf-mounted`` warning from ``high_flow_warnings`` (#723, #797).

    When both sides or neither side has the size, or the status cannot be read, this
    is ``None`` and the file is left as it was: the slicer keeps its own choice, and
    nothing is refused on the mounted nozzles (#768). A side the printer reports no
    size for counts as not having it; only the other side is then offered.
    """
    if status is None or not two_nozzles(status):
        return None
    found = {extruder: _nozzles_on(status, extruder, size) for extruder in (RIGHT, LEFT)}
    for has in (
        {extruder for extruder, flows in found.items() if False in flows},
        {extruder for extruder, flows in found.items() if flows},
    ):
        if len(has) == 1:
            return [f"Standard#{int(extruder in has)}" for extruder in SLICER_ORDER]
        if has:
            return None
    return None


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
