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

The slice also states the flow chosen for each side (#484), as Bambu Studio does:
``nozzle_volume_type`` (:func:`slicer_volume_types`), and each side's nozzles named by
that flow in ``extruder_nozzle_stats``. One advisory stays, by the owner's ruling on #723
(queue item 149 paused on a High Flow nozzle sliced as Standard) and #797: a side the
slice may use whose mounted nozzle is of the chosen size but not of the flow sliced
there is warned about (:func:`high_flow_warnings`), never refused, since a print may be
set up before its nozzle is fitted.

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
from scadbuddy.bambuddy.models import FlowType, NozzleChoice, PrinterStatus

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


def _fitted_flow(status: PrinterStatus | None, extruder: int) -> FlowType | None:
    """The flow of the nozzle mounted on ``extruder``, or ``None`` when its type code is
    too short to say, as ``rack.rank.eligible`` reads one."""
    if status is None or extruder >= len(status.nozzles):
        return None
    if len(status.nozzles[extruder].nozzle_type or "") < 2:
        return None
    return "high_flow" if status.nozzles[extruder].high_flow else "standard"


def high_flow_warning(extruder: int, flow: FlowType) -> FilamentWarning:
    """The ``hf-mounted`` warning for ``extruder``, sliced for ``flow`` while the nozzle
    mounted there is of the other flow."""
    side = _side_word(extruder)
    sliced = VOLUME_TYPE[flow]
    mounted = VOLUME_TYPE["standard" if flow == "high_flow" else "high_flow"]
    return FilamentWarning(
        kind="hf-mounted",
        message=(
            f"The {side} nozzle is {mounted} and this print is sliced for a {sliced} "
            f"nozzle there, so if it prints on the {side}, the printer pauses at the first "
            f'layer ("the {side} nozzle is not matched with slicing file"). Fit a {sliced} '
            "nozzle there before it starts."
        ),
    )


def high_flow_warnings(
    status: PrinterStatus | None,
    nozzles: Sequence[NozzleChoice],
    *,
    rack_picked: bool = False,
    laid_out: bool = True,
) -> list[FilamentWarning]:
    """A warning for each side the slice may use whose mounted nozzle is of the chosen
    size but not of the flow sliced there (#723, #797, #484), never a refusal.

    Queue item 149 paused at the first layer on a High Flow left sliced as Standard.
    The slice states the flow chosen for each side (:func:`slicer_volume_types`) and,
    when one side alone has a nozzle of that flow, offers the slicer only that side
    (:func:`slicer_nozzle_stats`). A side it may use can still have the other flow
    mounted: the one side offered when no side has the chosen flow, only the size;
    either side when the slicer is left to choose; and the rack side, whose spares count
    as its nozzles. A print may be set up before its nozzle is fitted, so the owner
    chose a warning, and the dialog shows it in Simple and Advanced mode alike. An
    unreadable status knows no nozzle, and a nozzle with no type code no flow, so
    neither is warned of.

    ``rack_picked``: a rack position is picked for the rack side, which only ever picks
    a hotend of the size and of the flow sliced there (``rack.rank.eligible``), so the
    hotend mounted there now is swapped out and is not warned about (#1238).

    ``laid_out``: the file is an output the run lays out, which states the side
    offered and each side's flow. A library file prints as its author left it (#313):
    the slicer may use either side, and it slices as Standard (:func:`_sliced_flows`)."""
    if not nozzles:
        return []
    size = nozzles[0].size
    flows = _sliced_flows(nozzles, laid_out=laid_out)
    offered = _offered_side(status, nozzles) if laid_out else None
    return [
        high_flow_warning(extruder, flows[extruder])
        for extruder in (RIGHT, LEFT)
        if offered in (None, extruder)
        and fitted_size(status, extruder) == size
        and _fitted_flow(status, extruder) not in (None, flows[extruder])
        and not (rack_picked and extruder == RACK_SIDE)
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
#: Each flow as Bambu Studio 02.08.02.61 spells it in ``nozzle_volume_type`` and
#: ``extruder_nozzle_stats`` (#484).
VOLUME_TYPE: dict[FlowType, str] = {"standard": "Standard", "high_flow": "High Flow"}


def _flows(nozzles: Sequence[NozzleChoice]) -> dict[int, FlowType]:
    """The flow chosen for each physical side. The dialog lists the left side first and
    the right second; a single choice is both sides'."""
    return {LEFT: nozzles[0].flow, RIGHT: nozzles[-1].flow}


def _sliced_flows(nozzles: Sequence[NozzleChoice], *, laid_out: bool) -> dict[int, FlowType]:
    """The flow each side is sliced for. An output the run lays out states the flow
    chosen for each (:func:`slicer_volume_types`). A library file prints as its author
    left it (#313) and states none unless they saved it High Flow, which is not read,
    so it is taken as Standard on both sides, as every slice was before #484."""
    if laid_out:
        return _flows(nozzles)
    return {LEFT: "standard", RIGHT: "standard"}


def slicer_volume_types(nozzles: Sequence[NozzleChoice]) -> list[str]:
    """``nozzle_volume_type`` for the 3MF: the flow chosen for each extruder, in the
    slicer's order (#484).

    The one key Bambu Studio 02.08.02.61 changes per extruder when a project is High
    Flow (``default_nozzle_volume_type`` stays Standard). The CLI keeps the 3MF's value
    because the stock H2C printer preset has none (``BambuStudio.cpp``, around lines
    3420-3430), and with it written the slice's printer and process come out "Direct
    Drive High Flow" (measured on live Bambuddy, 2026-10-06).

    Always two entries, the H2C's: the run refuses any other printer model before
    anything is sliced, since its presets are the only ones the resolver knows. A
    single-nozzle printer would need one."""
    flows = _flows(nozzles)
    return [VOLUME_TYPE[flows[extruder]] for extruder in SLICER_ORDER]


def rack_volume_type(nozzles: Sequence[NozzleChoice], *, laid_out: bool = True) -> str:
    """The flow the rack side is sliced for, which Bambuddy re-checks a rack pick
    against at dispatch: the right's, the side the rack swaps onto. ``laid_out`` as
    for :func:`high_flow_warnings`: a library file's is taken as Standard."""
    return VOLUME_TYPE[_sliced_flows(nozzles, laid_out=laid_out)[RACK_SIDE]]


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


def _offered_side(status: PrinterStatus | None, nozzles: Sequence[NozzleChoice]) -> int | None:
    """The one side :func:`slicer_nozzle_stats` offers the slicer, or ``None`` when it
    leaves the slicer to choose."""
    if status is None or not two_nozzles(status):
        return None
    size = nozzles[0].size
    chosen = _flows(nozzles)
    found = {extruder: _nozzles_on(status, extruder, size) for extruder in (RIGHT, LEFT)}
    for has in (
        {e for e, flows in found.items() if (chosen[e] == "high_flow") in flows},
        {e for e, flows in found.items() if flows},
    ):
        if len(has) == 1:
            return has.pop()
        if has:
            return None
    return None


def slicer_nozzle_stats(
    status: PrinterStatus | None, nozzles: Sequence[NozzleChoice]
) -> list[str] | None:
    """``extruder_nozzle_stats`` naming only the side that has a nozzle of the chosen
    size (#834), each side by the flow chosen for it (#484).

    Bambu's H2C presets state one size on both extruders, and the slicer's "Auto For
    Flush" grouping spreads the filaments over every extruder its nozzle stats offer —
    the ``filament_map`` in the 3MF is not followed (#745). Queue item 159 was sliced
    so, with a filament on the left 0.4 as if it were a 0.2, and paused with "the left
    nozzle is not matched with slicing file". Bambu Studio avoids that by stating what
    each side has: archive 36, which printed, carries ["Standard#0|High Flow#0",
    "Standard#1"] and every filament on the right. Measured against the deployed slicer
    (2026-09-30), this key in the 3MF steers its grouping the same way.

    A nozzle of the flow chosen for its side is preferred: queue item 149 paused on a
    High Flow left sliced as Standard. One of the other flow still beats a side without
    the size, since nothing is refused on the mounted nozzles (#768), and
    :func:`high_flow_warnings` warns of it when it is mounted (#723, #797). Each side is
    named by the flow chosen for it (``High Flow#1``, as Bambu Studio writes it), the
    same flow :func:`slicer_volume_types` states; without ``nozzle_volume_type`` the
    deployed slicer (bambu-studio-api bambuddy-1.2.5.6, 2026-09-30) read the label as a
    count only and sliced Standard.

    When both sides or neither side has the size, or the status cannot be read, this
    is ``None`` and the file is left as it was: the slicer keeps its own choice, and
    nothing is refused on the mounted nozzles (#768). A side the printer reports no
    size for counts as not having it; only the other side is then offered.
    """
    offered = _offered_side(status, nozzles)
    if offered is None:
        return None
    chosen = _flows(nozzles)
    return [
        f"{VOLUME_TYPE[chosen[extruder]]}#{int(extruder == offered)}" for extruder in SLICER_ORDER
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
