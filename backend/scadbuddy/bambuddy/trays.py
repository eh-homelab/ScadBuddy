"""A filled tray Bambuddy has no spool for (#2164).

A tray loaded with third-party filament has no RFID tag: the printer reports what
someone set on it (a material and a colour) but no ``tag_uid`` or ``tray_uuid``, so
Bambuddy cannot tell which inventory spool is in it and assigns none. The spool in it
then shows as a shelf spool. ScadBuddy cannot know which spool it is either, so the
print dialog asks one plain question, and on the person's "yes" records the answer in
Bambuddy (``POST /inventory/assignments``, :meth:`BambuddyClient.assign_spool`).

Until then, the tray itself is a choice: a :class:`~scadbuddy.bambuddy.filaments.SpoolOption`
with a negative :func:`tray_spool_id`, of the tray's material and colour, with no
weight tracked. Bambuddy's scheduler maps a queue item by type and colour, so the
print draws from that tray.
"""

from __future__ import annotations

import re

from scadbuddy.bambuddy.extruders import extruder_of, side_of
from scadbuddy.bambuddy.filaments import (
    FilamentOptions,
    LoadedAt,
    SpoolOption,
    UnknownTray,
    colour_distance,
    normalise_colour,
)
from scadbuddy.bambuddy.models import AmsTray, PrinterStatus

#: A tray's state when it holds filament (Bambu firmware: 11 loaded, 10 present but
#: not fed, 9 empty), as Bambuddy's own ``assign_spool`` reads it.
FILLED_STATES = frozenset({10, 11})
#: The CIEDE2000 distance under which a spool's colour is "close" to the tray's: wider
#: than the auto-match's, since a colour someone set on the printer by hand is rough.
CLOSE_COLOUR = 20.0
#: The external spool holder's ``ams_id``, whose trays arrive in ``vt_tray``.
EXTERNAL = 255

#: Basic colour words, for "black PLA" (#2164's question).
_WORDS: dict[str, tuple[int, int, int]] = {
    "black": (25, 25, 25),
    "white": (245, 245, 245),
    "grey": (128, 128, 128),
    "red": (200, 30, 30),
    "orange": (240, 130, 30),
    "yellow": (240, 220, 40),
    "green": (40, 160, 60),
    "blue": (30, 90, 200),
    "purple": (130, 50, 170),
    "pink": (240, 120, 180),
    "brown": (110, 70, 40),
}


_SPLIT = re.compile(r"[-\s_]+")


def family(material: str | None) -> str:
    """What can share a hotend (#2166): the material's first word (PLA, PETG, ABS,
    TPU ...), so ``PLA Basic``, ``PLA-Matte`` and ``PLA-CF`` are one family."""
    words = [word for word in _SPLIT.split((material or "").upper()) if word]
    return words[0] if words else ""


def tray_spool_id(ams_id: int, tray_id: int) -> int:
    """The negative id a tray with no spool goes by in the picker and the plan."""
    return -(ams_id * 16 + tray_id + 1)


def tray_of_spool_id(spool_id: int) -> tuple[int, int] | None:
    """``(ams_id, tray_id)`` of a :func:`tray_spool_id`, else ``None``."""
    if spool_id >= 0:
        return None
    index = -spool_id - 1
    return index // 16, index % 16


def colour_word(colour: str | None) -> str | None:
    """The nearest basic colour word for ``#RRGGBB``, as a person would say it."""
    hex_ = normalise_colour(colour)
    if hex_ is None:
        return None
    best = min(
        _WORDS.items(),
        key=lambda item: colour_distance(hex_, "#{:02X}{:02X}{:02X}".format(*item[1])) or 0.0,
    )
    return best[0]


def unit_label(ams_id: int) -> str:
    """An AMS unit as Bambuddy names it (#2163): AMS-A, AMS-D, HT-A, External."""
    if ams_id == EXTERNAL:
        return "External"
    if ams_id >= 128:
        return f"HT-{chr(65 + ams_id - 128)}"
    return f"AMS-{chr(65 + ams_id)}"


def tray_label(ams_id: int, tray_id: int) -> str:
    """ "AMS-D slot 4": the slot counted from 1, as Bambu's own UI counts it."""
    if ams_id >= 128 and ams_id != EXTERNAL:
        return unit_label(ams_id)
    return f"{unit_label(ams_id)} slot {tray_id + 1}"


def _filled(tray: AmsTray) -> bool:
    if not (tray.tray_type or "").strip():
        return False
    if tray.state is not None:
        return tray.state in FILLED_STATES
    return tray.exists is not False


def _trays(status: PrinterStatus) -> list[tuple[int, AmsTray]]:
    found = [(unit.id, tray) for unit in status.ams for tray in unit.tray]
    # The external holders: vt_tray ids 254 and 255 are Ext-L and Ext-R, trays 0 and 1.
    found += [(EXTERNAL, tray.model_copy(update={"id": tray.id - 254})) for tray in status.vt_tray]
    return found


def unknown_trays(
    status: PrinterStatus | None, printer_id: int, spools: list[SpoolOption]
) -> list[UnknownTray]:
    """Every filled tray of ``printer_id`` no spool is assigned to (#2164)."""
    if status is None:
        return []
    taken = {
        (spool.loaded.ams_id, spool.loaded.tray_id)
        for spool in spools
        if spool.loaded is not None and spool.loaded.printer_id == printer_id
    }
    found: list[UnknownTray] = []
    for ams_id, tray in _trays(status):
        if (ams_id, tray.id) in taken or not _filled(tray):
            continue
        material = (tray.tray_type or "").strip()
        colour = normalise_colour(tray.tray_color)
        candidates = [
            spool
            for spool in spools
            if spool.loaded is None
            and spool.spool_id > 0
            and family(spool.material) == family(material)
            and (distance := colour_distance(spool.colour, colour)) is not None
            and distance <= CLOSE_COLOUR
        ]
        candidates.sort(
            key=lambda spool: (
                not spool.remaining_g,
                -(spool.remaining_g or 0),
                spool.spool_id,
            )
        )
        found.append(
            UnknownTray(
                ams_id=ams_id,
                tray_id=tray.id,
                label=tray_label(ams_id, tray.id),
                material=material,
                colour=colour,
                colour_word=colour_word(colour),
                fingerprint=f"{material}|{colour or ''}|{'' if tray.state is None else tray.state}",
                spool_id=tray_spool_id(ams_id, tray.id),
                candidates=[spool.spool_id for spool in candidates],
            )
        )
    return found


def with_trays(
    options: FilamentOptions, status: PrinterStatus | None, printer_id: int
) -> FilamentOptions:
    """``options`` with the printer's unknown trays (#2164), each also offered as a
    choice of its own (:func:`tray_option`), labelled with its side as a spool is."""
    if any(spool.tray_only for spool in options.spools):
        return options
    options.trays = unknown_trays(status, printer_id, options.spools)
    for tray in options.trays:
        option = tray_option(tray, printer_id, options.printer_name)
        option.extruder = extruder_of(tray.ams_id, tray.tray_id, status)
        option.side = side_of(option.extruder)
        options.spools.append(option)
    return options


def tray_colours(spool_ids: list[int], status: PrinterStatus | None) -> dict[int, str]:
    """The colour of each tray a :func:`tray_spool_id` among ``spool_ids`` names."""
    found: dict[int, str] = {}
    for spool_id in spool_ids:
        tray = tray_material_colour(spool_id, status)
        if tray is not None and tray[1] is not None:
            found[spool_id] = tray[1]
    return found


def tray_option(tray: UnknownTray, printer_id: int, printer_name: str | None) -> SpoolOption:
    """The tray itself as a choice: its material and colour, no weight tracked."""
    return SpoolOption(
        spool_id=tray.spool_id,
        material=tray.material,
        colour=tray.colour,
        color_name=f"what's in {tray.label}",
        loaded=LoadedAt(
            printer_id=printer_id,
            printer_name=printer_name,
            ams_id=tray.ams_id,
            tray_id=tray.tray_id,
        ),
        tray_only=True,
    )


def tray_material_colour(
    spool_id: int, status: PrinterStatus | None
) -> tuple[str, str | None] | None:
    """The material and colour of the tray a :func:`tray_spool_id` names, from the
    printer's status, else ``None``."""
    where = tray_of_spool_id(spool_id)
    if where is None or status is None:
        return None
    for ams_id, tray in _trays(status):
        if (ams_id, tray.id) == where and (tray.tray_type or "").strip():
            return (tray.tray_type or "").strip(), normalise_colour(tray.tray_color)
    return None
