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

On a native run (#3818 tier A, plan 2026-10-18 D1) there is no inventory to pick from:
every filled tray is a choice of its own (:func:`loaded_trays`), with the spool overlay's
provider records of the spool in it, and :func:`seen_trays` is what that overlay reads.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from scadbuddy.bambuddy.extruders import extruder_of, side_of, two_nozzles
from scadbuddy.bambuddy.filaments import (
    FilamentOptions,
    LoadedAt,
    SpoolOption,
    UnknownTray,
    colour_distance,
    family,
    normalise_colour,
)
from scadbuddy.bambuddy.models import AmsTray, PrinterStatus
from scadbuddy.spools.models import (
    Overlay,
    SeenTray,
    ShelfSpool,
    SpoolSnapshot,
    clean_tag_uid,
    clean_tray_uuid,
)

#: A tray's ``state`` is a bitfield, not an enum (ha-bambulab ``pybambu/const.py:321-328``
#: at ``0e027ff1``, ``AMSTrayStateFlags``): SPOOL 0x01, METADATA 0x02, MOTION 0x04,
#: STEADY 0x08, RFID 0x10; states up to ``AMS_TRAY_STATE_LEGACY_MAX`` (3) are an older
#: firmware's. Read as ha-bambulab's ``utils.py:487-493`` ``ams_tray_spool_loaded``
#: reads it (:func:`spool_loaded`). The enum this replaced (``{10, 11}``, after Bambuddy's
#: ``assign_spool`` comment "11=loaded, 9=empty, 10=spool present but filament not in
#: feeder", ``backend/app/api/routes/inventory.py:2134`` at maziggy/bambuddy
#: ``0d427510``) read the recorded RFID tray at 27 (0x1B) as empty, and an AMS-HT's
#: loaded tray, which reports 9 (Bambuddy's own #2594, the same file, ``:2245-2248``).
TRAY_SPOOL = 0x01
TRAY_STEADY = 0x08
TRAY_STATE_LEGACY_MAX = 3
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


def spool_loaded(state: int) -> bool:
    """Whether ``state`` says a spool is in the slot and the tray is not loading or
    scanning: ha-bambulab's ``ams_tray_spool_loaded``, as cited at :data:`TRAY_SPOOL`."""
    if not state & TRAY_SPOOL:
        return False
    if state <= TRAY_STATE_LEGACY_MAX:
        return state == TRAY_STATE_LEGACY_MAX
    return bool(state & TRAY_STEADY)


def _filled(tray: AmsTray) -> bool:
    """A tray with a material whose spool is loaded. A presence bit that says no spool
    (``exists`` false: the firmware's ``tray_exist_bits``, "authoritative where it
    exists", Bambuddy ``services/ams_slot_presence.py`` at ``0d427510``) vetoes before the
    state is read: an AMS-HT echoes a stale ``tray_type`` after its spool is pulled
    (Bambuddy #2670). A ``True`` bit does not overrule a state without SPOOL (10), as
    Bambuddy lets it overrule only its own stamped 9 (``inventory.py:2270-2283``)."""
    if not (tray.tray_type or "").strip():
        return False
    if tray.exists is False:
        return False
    if tray.state is not None:
        return spool_loaded(tray.state)
    return True


def _trays(status: PrinterStatus) -> list[tuple[int, AmsTray]]:
    found = [(unit.id, tray) for unit in status.ams for tray in unit.tray]
    # The external holders: vt_tray ids 254 and 255 are Ext-L and Ext-R, trays 0 and 1.
    found += [(EXTERNAL, tray.model_copy(update={"id": tray.id - 254})) for tray in status.vt_tray]
    return found


def close_spools(
    spools: Iterable[SpoolOption], material: str, colour: str | None
) -> list[SpoolOption]:
    """The spools an untagged tray of ``material`` and ``colour`` may hold: the same
    material family, a colour within :data:`CLOSE_COLOUR`, most filament first and empty
    ones last."""
    found = [
        spool
        for spool in spools
        if family(spool.material) == family(material)
        and (distance := colour_distance(spool.colour, colour)) is not None
        and distance <= CLOSE_COLOUR
    ]
    found.sort(key=lambda spool: (not spool.remaining_g, -(spool.remaining_g or 0), spool.spool_id))
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
        candidates = close_spools(
            (spool for spool in spools if spool.loaded is None and spool.spool_id > 0),
            material,
            colour,
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


def _external_label(side: str | None, status: PrinterStatus) -> str:
    """The external holder as a person reads it: "External", or "External L"/"R" by the
    side it feeds on a printer with two nozzles (254 the left, 255 the right,
    `extruders.extruder_of`), so the label never disagrees with ``side``."""
    return f"External {side}" if side is not None and two_nozzles(status) else "External"


def _native_label(ams_id: int, tray_id: int, status: PrinterStatus) -> str:
    """A native run's tray as a person reads it: :func:`tray_label`, an external holder
    by its side (:func:`_external_label`)."""
    if ams_id == EXTERNAL:
        return _external_label(side_of(extruder_of(ams_id, tray_id, status)), status)
    return tray_label(ams_id, tray_id)


def _sorted_trays(status: PrinterStatus) -> list[tuple[int, AmsTray]]:
    """The filled trays in AMS order, the external holders last: units arrive unsorted."""
    return sorted(
        ((ams_id, tray) for ams_id, tray in _trays(status) if _filled(tray)),
        key=lambda found: (found[0] == EXTERNAL, found[0], found[1].id),
    )


def seen_trays(status: PrinterStatus) -> list[SeenTray]:
    """Every filled tray as the spool overlay reads it (`SpoolStore.overlay`): its RFID
    keys (cleaned there, so zeros are no key), material, colour and ``tray_info_idx``;
    its fingerprint is `spools.models.tray_fingerprint`, never its state."""
    return [
        SeenTray(
            ams_id=ams_id,
            tray_id=tray.id,
            tray_uuid=clean_tray_uuid(tray.tray_uuid),
            tag_uid=clean_tag_uid(tray.tag_uid),
            material=(tray.tray_type or "").strip(),
            colour=normalise_colour(tray.tray_color),
            tray_info_idx=(tray.tray_info_idx or "").strip() or None,
        )
        for ams_id, tray in _sorted_trays(status)
    ]


def loaded_trays(
    status: PrinterStatus,
    printer_id: int,
    printer_name: str | None,
    overlay: Overlay | None = None,
) -> list[SpoolOption]:
    """The native run's filament choices (#3818 tier A, plan 2026-10-18 D1): every filled
    tray as itself, under its :func:`tray_spool_id`, with what the overlay knows of the
    spool in it. ``slicer_filament`` is the tray's ``tray_info_idx`` (the BBL preset's
    ``filament_id``, `resolver._by_filament_id`); the weight, brand and label weight are
    the first provider record's that has them, in the overlay's (priority) order."""
    by_tray = {(tray.ams_id, tray.tray_id): tray for tray in (overlay.trays if overlay else [])}
    options: list[SpoolOption] = []
    for ams_id, tray in _sorted_trays(status):
        spool = by_tray.get((ams_id, tray.id))
        records = spool.records if spool is not None else {}
        extruder = extruder_of(ams_id, tray.id, status)
        options.append(
            SpoolOption(
                spool_id=tray_spool_id(ams_id, tray.id),
                material=(tray.tray_type or "").strip(),
                colour=normalise_colour(tray.tray_color),
                brand=_first(records, "brand"),
                color_name=_first(records, "name"),
                slicer_filament=(tray.tray_info_idx or "").strip() or None,
                remaining_g=_first(records, "remaining_g"),
                label_weight_g=_first(records, "label_weight_g"),
                loaded=LoadedAt(
                    printer_id=printer_id,
                    printer_name=printer_name,
                    ams_id=ams_id,
                    tray_id=tray.id,
                ),
                extruder=extruder,
                side=side_of(extruder),
                tray_only=True,
                label=_native_label(ams_id, tray.id, status),
                colour_word=colour_word(tray.tray_color),
                remain_percent=tray.remain if tray.remain >= 0 else None,
                physical_spool_id=spool.physical_spool_id if spool is not None else None,
                records=records,
            )
        )
    return options


def shelf_options(shelf: Sequence[ShelfSpool]) -> list[SpoolOption]:
    """The overlay's shelf (`Overlay.shelf`) as picker rows, numbered from 1 in this
    answer only: for an untagged tray's question to name, never a print choice. The link
    a person makes names the row's ``physical_spool_id``, else one of its ``records``
    (``PUT /printers/{id}/trays/{ams}/{tray}/spool``)."""
    return [
        SpoolOption(
            spool_id=index,
            material=_first(spool.records, "material") or "",
            colour=normalise_colour(_first(spool.records, "colour")),
            brand=_first(spool.records, "brand"),
            color_name=_first(spool.records, "name"),
            remaining_g=_first(spool.records, "remaining_g"),
            label_weight_g=_first(spool.records, "label_weight_g"),
            physical_spool_id=spool.physical_spool_id,
            records=spool.records,
        )
        for index, spool in enumerate(shelf, start=1)
    ]


def tray_questions(
    status: PrinterStatus, overlay: Overlay | None
) -> tuple[list[UnknownTray], list[SpoolOption]]:
    """A native run's questions (#4504): each filled untagged tray the overlay finds no
    spool for, with the shelf's records it may hold (:func:`close_spools`), and the shelf
    rows those questions name. A tray with no close record is not asked about: it is a
    choice of its own already, and there is nothing to link it to."""
    if overlay is None:
        return [], []
    shelf = shelf_options(overlay.shelf)
    held = {
        (tray.ams_id, tray.tray_id)
        for tray in overlay.trays
        if tray.physical_spool_id is not None or tray.records
    }
    questions: list[UnknownTray] = []
    offered: set[int] = set()
    for seen in seen_trays(status):
        if seen.tray_uuid or seen.tag_uid or (seen.ams_id, seen.tray_id) in held:
            continue
        material = seen.material or ""
        candidates = close_spools(shelf, material, seen.colour)
        if not candidates:
            continue
        offered.update(spool.spool_id for spool in candidates)
        questions.append(
            UnknownTray(
                ams_id=seen.ams_id,
                tray_id=seen.tray_id,
                label=_native_label(seen.ams_id, seen.tray_id, status),
                material=material,
                colour=seen.colour,
                colour_word=colour_word(seen.colour),
                fingerprint=seen.fingerprint,
                spool_id=tray_spool_id(seen.ams_id, seen.tray_id),
                candidates=[spool.spool_id for spool in candidates],
            )
        )
    return questions, [spool for spool in shelf if spool.spool_id in offered]


def _first(records: Mapping[str, SpoolSnapshot], field: str) -> Any:
    return next(
        (value for record in records.values() if (value := getattr(record, field)) is not None),
        None,
    )
