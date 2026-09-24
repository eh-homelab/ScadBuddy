"""Choosing a spool per plate slot, from Bambuddy's own inventory (#87).

The picker offers the **whole spool inventory**, not only what is loaded — a spool on
the shelf is a legitimate choice that produces a "load this into …" instruction rather
than a silent failure. Everything it shows is a read of Bambuddy's objects; ScadBuddy
keeps no filament state of its own.

Four things about Bambuddy's data shape this module, each measured rather than assumed:

* **``inventory-remain`` already does the hard join.** ``slot_materials[]`` carries
  ``global_tray_id`` (the number ``ams_mapping`` is made of), ``remaining_g``
  (Bambuddy's reconciliation of the AMS against the inventory) and ``extruder`` (which
  extruder the slot feeds). So the flat tray id is *read*, not recomputed, and the
  filament-switcher question is answered without decoding ``ams_switch_inlet``'s A/B
  into extruder numbers — that mapping is the printer's, not ours to infer.
* **A spool row's ``nozzle_temp_min/max`` are null** on the live instance. The window
  comes from the AMS tray the spool is loaded in, then the spool row, then nowhere —
  and "nowhere" is reported as unknown rather than treated as compatible.
* **``used_grams: 0`` means unknown.** ScadBuddy uploads an unsliced plate, and
  ``filament-requirements`` answers 0 for every slot of one. A "not enough filament"
  rule that read it as a real weight would never fire; one that read 0 as "needs
  nothing" would claim every spool is sufficient. Both are wrong, so it is ``None``.
* **``remain: -1`` on an AMS tray means unknown too** — that is what an untagged spool
  reports, and it is why remaining weight is taken from the inventory rather than from
  the tray.

The compatibility rules are derived from that data and from nothing else. There is no
material table here on purpose: "PLA must not share a plate with PETG" *is* the
non-overlapping nozzle-temperature window (190-230 against 230-260), so the rule keeps
working as filaments are added, which a hand-kept list does not.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import (
    AmsTray,
    NozzleRackSlot,
    PresetRef,
    Printer,
    PrinterStatus,
    SlotMaterial,
    Spool,
    SpoolAssignment,
    SpoolFilamentPreset,
)

logger = logging.getLogger(__name__)

#: How far apart two colours may be, as a plain RGB distance, and still auto-match.
#: The whole cube's diagonal is ~441, so this is "recognisably the same colour" — a
#: navy and a royal blue match, a blue and a pink do not. It only seeds the selection;
#: every slot stays editable.
COLOUR_MATCH_DISTANCE = 48.0

#: ``254`` is the deputy external feed and ``255`` the main one, per Bambuddy's own
#: ``print_scheduler``. They are not AMS units and have no ``ams_id * 4`` arithmetic.
EXTERNAL_TRAY_ID_MIN = 254

#: A process preset names its nozzle in its own name ("… H2C 0.4 nozzle"); there is no
#: diameter field anywhere in Bambuddy's preset model to read instead.
_NOZZLE_IN_NAME = re.compile(r"(\d+\.\d+)\s*nozzle", re.IGNORECASE)

#: Where a spool's nozzle-temperature window was read from. "unknown" is a real
#: answer here, not a placeholder: the temperature rule declines to judge on it.
TemperatureSource = Literal["tray", "spool", "unknown"]

WarningKind = Literal[
    "temperature",
    "not-loaded",
    "low-filament",
    "nozzle-mismatch",
    "unreachable",
    "no-choice",
    "unknown-temperature",
]


class LoadedAt(BaseModel):
    """Where a spool physically is, when Bambuddy says it is loaded somewhere."""

    printer_id: int
    printer_name: str | None = None
    ams_id: int
    tray_id: int
    #: The number ``ams_mapping`` carries. Read from ``inventory-remain`` where the
    #: printer reported one, else ``ams_id`` at 128+ and ``ams_id * 4 + tray_id`` below.
    global_tray_id: int
    #: Which extruder this slot feeds, as Bambuddy reports it. ``None`` when the printer
    #: did not say — on a single-extruder machine there is nothing to say.
    extruder: int | None = None
    #: The inlet this AMS is switched to ("A"/"B"), from ``ams_switch_inlet``.
    inlet: str | None = None
    is_ams_ht: bool = False
    is_external: bool = False


class SpoolOption(BaseModel):
    """One row of the picker: a spool, plus where it is and what it can print."""

    spool_id: int
    material: str
    subtype: str | None = None
    brand: str | None = None
    color_name: str | None = None
    #: Normalised to ``#RRGGBB``. The inventory spells it ``RRGGBBAA`` without a ``#``,
    #: ``available-filaments`` spells the same colour with one, and a plate's
    #: requirement spells it ``#RRGGBB`` — so everything is normalised on the way in.
    colour: str | None = None
    slicer_filament: str | None = None
    slicer_filament_name: str | None = None
    #: ``label_weight - weight_used``, or Bambuddy's own reconciled figure for a loaded
    #: spool, which wins where both exist.
    remaining_g: float | None = None
    nozzle_temp_min: int | None = None
    nozzle_temp_max: int | None = None
    #: Where the temperature window came from, so the UI can say "unknown" honestly.
    temperature_from: TemperatureSource = "unknown"
    storage_location: str | None = None
    loaded: LoadedAt | None = None
    #: ``nozzle_diameter -> slicer_filament`` for the chosen printer's model. Empty when
    #: the spool has no preset for that model, which is itself worth warning about.
    nozzle_presets: dict[str, str] = Field(default_factory=dict)


class SlotNeed(BaseModel):
    """One filament slot of the plate, as Bambuddy reads the 3MF.

    ``slot_id`` is **1-based**: ``ams_mapping`` is indexed by ``slot_id - 1``.
    """

    slot_id: int
    material: str | None = None
    colour: str | None = None
    #: Grams this slot needs for one copy, or ``None`` when the 3MF carries no slice
    #: info — which is every ScadBuddy upload before it has been sliced.
    used_grams: float | None = None


class FilamentWarning(BaseModel):
    """Advisory, never blocking. Bambuddy's own refusals are the eligibility report."""

    kind: WarningKind
    slot_id: int | None = None
    message: str


class SlotChoice(BaseModel):
    slot_id: int
    spool_id: int


class FilamentPlan(BaseModel):
    """What the dialog submits: one spool per slot, and how hard to insist on it.

    ``force_colour_match`` becomes ``force_color_match`` on every override, which is
    the flag Bambuddy's scheduler reads to require an exact type+colour match rather
    than merely preferring one. It is off by default because it only bites on a
    model-targeted queue item, where insisting can leave the job unschedulable.
    """

    slots: list[SlotChoice] = Field(default_factory=list)
    force_colour_match: bool = False

    def spool_for(self, slot_id: int) -> int | None:
        return next((slot.spool_id for slot in self.slots if slot.slot_id == slot_id), None)


class FilamentOptions(BaseModel):
    """Everything the filament step of the dialog needs, in one answer.

    One route rather than six, because the join — inventory against assignments against
    the live AMS against the per-slot remaining weights — is the part that is easy to
    get wrong, and doing it in the browser would mean shipping every spool's history to
    do it.
    """

    library_file_id: int
    printer_id: int | None = None
    printer_name: str | None = None
    printer_model: str | None = None
    #: One per extruder, as strings — ``PrinterStatus`` spells diameters that way.
    nozzle_diameters: list[str] = Field(default_factory=list)
    nozzle_rack: list[NozzleRackSlot] = Field(default_factory=list)
    ams_switch_inlet: dict[str, str] = Field(default_factory=dict)
    #: Parsed out of the pipeline's process preset *name*; there is no diameter field.
    process_nozzle_diameter: str | None = None
    slots: list[SlotNeed] = Field(default_factory=list)
    spools: list[SpoolOption] = Field(default_factory=list)
    #: ScadBuddy's opening move, so the common case needs no clicks.
    suggested: list[SlotChoice] = Field(default_factory=list)
    #: Warnings for :attr:`suggested`, so the dialog can show them before any click.
    warnings: list[FilamentWarning] = Field(default_factory=list)


class QueueFilaments(BaseModel):
    """The three ``PrintQueueItemCreate`` fields a plan turns into.

    They are the reason a plan cannot ride on a pipeline run: none of the three exists
    on ``PipelineRunCreateRequest``, so choosing spools forces the slice + queue route.
    """

    #: Indexed by ``slot_id - 1``; ``-1`` is Bambuddy's own "unresolved" sentinel.
    ams_mapping: list[int] = Field(default_factory=list)
    #: ``{slot_id, type, color, used_grams, force_color_match?}`` — the shape Bambuddy's
    #: scheduler validates, and the one its own 3MF parser produces.
    filament_overrides: list[dict[str, object]] = Field(default_factory=list)
    required_filament_types: list[str] = Field(default_factory=list)


def normalise_colour(raw: str | None) -> str | None:
    """``RRGGBBAA``, ``#RRGGBBAA``, ``#RRGGBB`` → ``#RRGGBB``; anything else → ``None``.

    Alpha is dropped rather than compared: a fully transparent ``00000000`` is how an
    empty tray and a clear filament both spell themselves, and treating that as a
    distinct colour would match clear spools to empty slots.
    """
    if not raw:
        return None
    value = raw.strip().lstrip("#").upper()
    if len(value) not in (6, 8) or any(char not in "0123456789ABCDEF" for char in value):
        return None
    return f"#{value[:6]}"


def colour_distance(left: str | None, right: str | None) -> float | None:
    """Plain RGB distance. ``None`` when either colour is unknown — not ``0``."""
    if left is None or right is None:
        return None
    a = tuple(int(left[index : index + 2], 16) for index in (1, 3, 5))
    b = tuple(int(right[index : index + 2], 16) for index in (1, 3, 5))
    return float(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5)


def global_tray_id(ams_id: int, tray_id: int) -> int:
    """Bambu's flat tray addressing, mirroring Bambuddy's ``_global_tray_id``.

    A four-slot AMS is ``ams_id * 4 + tray_id``; an AMS-HT reports an ``ams_id`` of 128
    or more and *is* its own id. Used only where ``inventory-remain`` did not already
    report one — it is the authority when it did.
    """
    return ams_id if ams_id >= 128 else ams_id * 4 + tray_id


def _tray_index(status: PrinterStatus | None) -> dict[tuple[int, int], AmsTray]:
    """``(ams_id, tray_id) -> tray``, including the external ``vt_tray`` feeds."""
    if status is None:
        return {}
    index: dict[tuple[int, int], AmsTray] = {}
    for unit in status.ams:
        for tray in unit.tray:
            index[(unit.id, tray.id)] = tray
    for tray in status.vt_tray:
        index[(tray.id, tray.id)] = tray
    return index


def _ams_ht(status: PrinterStatus | None) -> set[int]:
    return {unit.id for unit in status.ams if unit.is_ams_ht} if status else set()


def _temperature(
    spool: Spool, tray: AmsTray | None
) -> tuple[int | None, int | None, TemperatureSource]:
    """The nozzle window for a spool, and where it came from.

    The tray wins: it is the printer's own reading of the filament actually loaded,
    while the inventory row's pair is null on every spool of the live instance. Nothing
    is invented when both are absent — an unknown window is reported as unknown, and
    the temperature rule then declines to judge rather than passing the pair.
    """
    if tray is not None and tray.nozzle_temp_min and tray.nozzle_temp_max:
        return tray.nozzle_temp_min, tray.nozzle_temp_max, "tray"
    if spool.nozzle_temp_min is not None and spool.nozzle_temp_max is not None:
        return spool.nozzle_temp_min, spool.nozzle_temp_max, "spool"
    return None, None, "unknown"


def process_nozzle_diameter(process_preset_name: str | None) -> str | None:
    """``"Bambu PETG … H2C 0.4 nozzle"`` → ``"0.4"``.

    Bambuddy models no nozzle diameter anywhere — not on the process preset, not on the
    pipeline — so the preset's name is the only place it is written down. A name that
    does not carry one simply yields ``None`` and the nozzle rule stays quiet, rather
    than inventing a default diameter to compare against.
    """
    if not process_preset_name:
        return None
    match = _NOZZLE_IN_NAME.search(process_preset_name)
    return match.group(1) if match else None


def _loaded_at(
    assignment: SpoolAssignment,
    *,
    printer_id: int | None,
    slot_material: SlotMaterial | None,
    status: PrinterStatus | None,
    ams_ht: set[int],
) -> LoadedAt:
    tray = global_tray_id(assignment.ams_id, assignment.tray_id)
    if slot_material is not None:
        tray = slot_material.global_tray_id
    inlet = None
    if status is not None and assignment.printer_id == printer_id:
        inlet = status.ams_switch_inlet.get(str(assignment.ams_id))
    return LoadedAt(
        printer_id=assignment.printer_id,
        printer_name=assignment.printer_name,
        ams_id=assignment.ams_id,
        tray_id=assignment.tray_id,
        global_tray_id=tray,
        extruder=slot_material.extruder if slot_material else None,
        inlet=inlet,
        is_ams_ht=assignment.ams_id in ams_ht,
        is_external=assignment.ams_id >= EXTERNAL_TRAY_ID_MIN,
    )


def build_options(
    *,
    library_file_id: int,
    spools: list[Spool],
    assignments: list[SpoolAssignment],
    requirements: list[SlotNeed],
    printer: Printer | None = None,
    status: PrinterStatus | None = None,
    slot_materials: list[SlotMaterial] | None = None,
    presets_by_spool: dict[int, list[SpoolFilamentPreset]] | None = None,
    process_preset_name: str | None = None,
) -> FilamentOptions:
    """Join everything already fetched into what the dialog renders.

    Kept free of the client so the join — the part with the traps in it — is testable
    against recorded bodies without any HTTP at all.
    """
    ams_ht = _ams_ht(status)
    trays = _tray_index(status)
    by_slot = {(material.ams_id, material.tray_id): material for material in (slot_materials or [])}
    # Last assignment wins: Bambuddy keeps history rows, and a spool moved between
    # trays would otherwise render in the tray it left.
    assignment_by_spool = {assignment.spool_id: assignment for assignment in assignments}
    model = printer.model if printer else None

    options: list[SpoolOption] = []
    for spool in spools:
        if spool.archived_at is not None:
            continue
        assignment = assignment_by_spool.get(spool.id)
        # **The live state is one printer's, the assignments are every printer's.**
        # ``inventory-remain`` and ``status`` were read for ``printer``, while
        # ``/inventory/assignments`` is deliberately unfiltered so a spool loaded in
        # another machine is still offered. Joining them on ``(ams_id, tray_id)`` alone
        # would hand a spool sitting in printer B's AMS 0 slot 1 the flat tray id,
        # remaining weight and extruder of printer A's AMS 0 slot 1 — a different spool
        # — and `ams_mapping` would then address A's tray while the UI named B's spool.
        here = assignment is not None and (printer is None or assignment.printer_id == printer.id)
        slot_material = (
            by_slot.get((assignment.ams_id, assignment.tray_id)) if here and assignment else None
        )
        tray = trays.get((assignment.ams_id, assignment.tray_id)) if here and assignment else None
        low, high, source = _temperature(spool, tray)
        remaining = spool.remaining_g
        if slot_material is not None and slot_material.remaining_g is not None:
            remaining = slot_material.remaining_g
        presets = (presets_by_spool or {}).get(spool.id, [])
        options.append(
            SpoolOption(
                spool_id=spool.id,
                material=spool.material,
                subtype=spool.subtype,
                brand=spool.brand,
                color_name=spool.color_name,
                colour=normalise_colour(spool.rgba),
                slicer_filament=spool.slicer_filament,
                slicer_filament_name=spool.slicer_filament_name,
                remaining_g=remaining,
                nozzle_temp_min=low,
                nozzle_temp_max=high,
                temperature_from=source,
                storage_location=spool.storage_location,
                loaded=(
                    _loaded_at(
                        assignment,
                        printer_id=printer.id if printer else None,
                        slot_material=slot_material,
                        status=status,
                        ams_ht=ams_ht,
                    )
                    if assignment
                    else None
                ),
                nozzle_presets={
                    preset.nozzle_diameter: preset.slicer_filament
                    for preset in presets
                    if preset.slicer_filament and (model is None or preset.printer_model == model)
                },
            )
        )

    options.sort(key=_display_order(printer.id if printer else None))
    suggested = suggest(requirements, options, printer_id=printer.id if printer else None)
    built = FilamentOptions(
        library_file_id=library_file_id,
        printer_id=printer.id if printer else None,
        printer_name=printer.name if printer else None,
        printer_model=model,
        nozzle_diameters=[nozzle.nozzle_diameter for nozzle in (status.nozzles if status else [])],
        nozzle_rack=list(status.nozzle_rack) if status else [],
        ams_switch_inlet=dict(status.ams_switch_inlet) if status else {},
        process_nozzle_diameter=process_nozzle_diameter(process_preset_name),
        slots=requirements,
        spools=options,
    )
    built.suggested = suggested
    built.warnings = check(built, FilamentPlan(slots=suggested), copies=1)
    return built


def _display_order(printer_id: int | None) -> Callable[[SpoolOption], tuple[int, float, str]]:
    """Loaded in *this* printer first, then loaded anywhere, then the shelf.

    Within each band, most filament left first — the usual reason to prefer one of two
    identical spools. The key is a tuple rather than several sorts so the order is one
    statement and stable.
    """

    def key(option: SpoolOption) -> tuple[int, float, str]:
        if option.loaded is None:
            band = 2
        elif printer_id is not None and option.loaded.printer_id == printer_id:
            band = 0
        else:
            band = 1
        return (band, -(option.remaining_g or 0.0), option.color_name or "")

    return key


def suggest(
    slots: list[SlotNeed], spools: list[SpoolOption], *, printer_id: int | None = None
) -> list[SlotChoice]:
    """Pre-select a spool per slot: exact preset, then colour, then material.

    Greedy in slot order, and a spool already taken by an earlier slot is not offered
    again — the same rule Bambuddy's own matcher applies to trays, and without it a
    two-colour plate happily maps both slots onto one tray.
    """
    taken: set[int] = set()
    chosen: list[SlotChoice] = []
    for slot in slots:
        best: tuple[float, int] | None = None
        for option in spools:
            if option.spool_id in taken:
                continue
            score = _match_score(slot, option, printer_id=printer_id)
            if score is None:
                continue
            if best is None or score < best[0]:
                best = (score, option.spool_id)
        if best is not None:
            taken.add(best[1])
            chosen.append(SlotChoice(slot_id=slot.slot_id, spool_id=best[1]))
    return chosen


def _match_score(slot: SlotNeed, option: SpoolOption, *, printer_id: int | None) -> float | None:
    """Lower is better; ``None`` means this spool cannot serve this slot.

    A slot that declares a material only ever matches that material — auto-selecting a
    PETG for a PLA slot would be a worse failure than selecting nothing, because it
    looks deliberate.
    """
    if slot.material and option.material.upper() != slot.material.upper():
        return None
    distance = colour_distance(slot.colour, option.colour)
    if distance is None:
        # No colour to go on either side: material alone is a weak but real match, and
        # it is ranked behind every colour match rather than competing with them.
        base = 1_000.0
    elif distance > COLOUR_MATCH_DISTANCE:
        return None
    else:
        base = distance
    loaded_here = (
        option.loaded is not None
        and printer_id is not None
        and option.loaded.printer_id == printer_id
    )
    # A loaded spool wins every tie and beats a marginally closer shelf spool: the
    # point of the picker is that the common case needs no clicks *and* no reloading.
    return base + (0.0 if loaded_here else COLOUR_MATCH_DISTANCE + 1.0)


def check(
    options: FilamentOptions, plan: FilamentPlan, *, copies: int = 1
) -> list[FilamentWarning]:
    """Every compatibility rule, derived from the data rather than from a list."""
    by_id = {option.spool_id: option for option in options.spools}
    inlets = inlet_extruders(options)
    warnings: list[FilamentWarning] = []
    chosen: list[tuple[SlotNeed, SpoolOption]] = []

    for slot in options.slots:
        spool_id = plan.spool_for(slot.slot_id)
        option = by_id.get(spool_id) if spool_id is not None else None
        if option is None:
            warnings.append(
                FilamentWarning(
                    kind="no-choice",
                    slot_id=slot.slot_id,
                    message=f"Slot {slot.slot_id} has no filament chosen.",
                )
            )
            continue
        chosen.append((slot, option))
        warnings.extend(_slot_warnings(options, slot, option, copies=copies, inlets=inlets))

    warnings.extend(_temperature_warnings(chosen))
    return warnings


def _slot_warnings(
    options: FilamentOptions,
    slot: SlotNeed,
    option: SpoolOption,
    *,
    copies: int,
    inlets: dict[str, int],
) -> list[FilamentWarning]:
    found: list[FilamentWarning] = []
    label = _label(option)

    if option.loaded is None:
        found.append(
            FilamentWarning(
                kind="not-loaded",
                slot_id=slot.slot_id,
                message=(
                    f"Load {label} into the printer before this prints"
                    + (
                        f" — it is stored in {option.storage_location}."
                        if option.storage_location
                        else ", then start the print."
                    )
                ),
            )
        )
    elif options.printer_id is not None and option.loaded.printer_id != options.printer_id:
        found.append(
            FilamentWarning(
                kind="not-loaded",
                slot_id=slot.slot_id,
                message=(
                    f"{label} is loaded in {option.loaded.printer_name or 'another printer'}, "
                    f"not in {options.printer_name or 'the chosen printer'} — move it into an "
                    "AMS slot there first."
                ),
            )
        )

    needed = None if slot.used_grams is None else slot.used_grams * copies
    if needed is not None and option.remaining_g is not None and option.remaining_g < needed:
        found.append(
            FilamentWarning(
                kind="low-filament",
                slot_id=slot.slot_id,
                message=(
                    f"{label} has about {option.remaining_g:.0f} g left and this needs "
                    f"{needed:.0f} g."
                ),
            )
        )

    wanted = options.process_nozzle_diameter
    if wanted and option.nozzle_presets and wanted not in option.nozzle_presets:
        have = ", ".join(sorted(option.nozzle_presets)) or "none"
        found.append(
            FilamentWarning(
                kind="nozzle-mismatch",
                slot_id=slot.slot_id,
                message=(
                    f"The pipeline slices for a {wanted} mm nozzle, and {label} has a filament "
                    f"preset for {have} on this printer."
                ),
            )
        )
    if wanted and options.nozzle_diameters and wanted not in options.nozzle_diameters:
        fitted = ", ".join(options.nozzle_diameters)
        found.append(
            FilamentWarning(
                kind="nozzle-mismatch",
                slot_id=slot.slot_id,
                message=(
                    f"The pipeline slices for a {wanted} mm nozzle and the printer has "
                    f"{fitted} mm fitted."
                ),
            )
        )

    found.extend(_reachability(options, slot, option, inlets))
    return found


def inlet_extruders(options: FilamentOptions) -> dict[str, int]:
    """``inlet letter -> extruder``, **derived from the printer's own report**.

    Nothing here knows that inlet "A" is the right extruder, because nothing should:
    the correspondence is the machine's. Every loaded slot reports both the inlet its
    AMS is switched to and the extruder it feeds, so the map is read off those pairs.
    An inlet that reports two different extruders is dropped rather than guessed at —
    that means the reading is inconsistent, and a rule built on it would be worse than
    no rule.
    """
    seen: dict[str, set[int]] = {}
    for option in options.spools:
        loaded = option.loaded
        if loaded is None or loaded.inlet is None or loaded.extruder is None:
            continue
        seen.setdefault(loaded.inlet, set()).add(loaded.extruder)
    return {inlet: next(iter(found)) for inlet, found in seen.items() if len(found) == 1}


def _reachability(
    options: FilamentOptions, slot: SlotNeed, option: SpoolOption, inlets: dict[str, int]
) -> list[FilamentWarning]:
    """Will the extruder this spool is routed to print at the sliced nozzle diameter?

    This is the filament-switcher check the issue asks for, stated in the one form that
    is decidable from the data: a spool is fed to a particular extruder, that extruder
    has a particular nozzle fitted, and the pipeline slices for a particular diameter.
    Where the three disagree the print will not come out, and switching the AMS to the
    other inlet is the fix — so the warning names the AMS, the inlet and both diameters.

    On a single-extruder printer there is no routing to get wrong and nothing is said.
    """
    loaded = option.loaded
    wanted = options.process_nozzle_diameter
    if loaded is None or not wanted or len(options.nozzle_diameters) < 2:
        return []
    extruder = loaded.extruder
    if extruder is None and loaded.inlet is not None:
        extruder = inlets.get(loaded.inlet)
    if extruder is None or extruder >= len(options.nozzle_diameters):
        return []
    fitted = options.nozzle_diameters[extruder]
    if fitted == wanted:
        return []
    where = f"AMS {loaded.ams_id}"
    if loaded.inlet:
        where += f" (inlet {loaded.inlet})"
    return [
        FilamentWarning(
            kind="unreachable",
            slot_id=slot.slot_id,
            message=(
                f"{_label(option)} is in {where}, which feeds extruder {extruder} — that "
                f"extruder has a {fitted} mm nozzle and the pipeline slices for {wanted} mm. "
                "Switch that AMS to the other inlet, or choose a spool that already feeds the "
                "right extruder."
            ),
        )
    ]


def _temperature_warnings(
    chosen: list[tuple[SlotNeed, SpoolOption]],
) -> list[FilamentWarning]:
    """Do the chosen filaments share a printable nozzle temperature?

    Grouped by the extruder each one feeds, because two extruders heat independently —
    a PLA on one and a PETG on the other is exactly what a two-extruder machine is for,
    and warning about it would make the rule noise. Spools whose window is unknown are
    reported as unknown once, not folded into the intersection as if they fitted.
    """
    if len(chosen) < 2:
        return []
    groups: dict[int | None, list[tuple[SlotNeed, SpoolOption]]] = {}
    for slot, option in chosen:
        extruder = option.loaded.extruder if option.loaded else None
        groups.setdefault(extruder, []).append((slot, option))

    warnings: list[FilamentWarning] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        known = [(slot, option) for slot, option in members if option.temperature_from != "unknown"]
        unknown = [option for _, option in members if option.temperature_from == "unknown"]
        if unknown:
            names = ", ".join(_label(option) for option in unknown)
            warnings.append(
                FilamentWarning(
                    kind="unknown-temperature",
                    message=(
                        f"Bambuddy has no nozzle temperature for {names}, so ScadBuddy cannot "
                        "check it against the others on this plate."
                    ),
                )
            )
        if len(known) < 2:
            continue
        low = max(option.nozzle_temp_min or 0 for _, option in known)
        high = min(option.nozzle_temp_max or 0 for _, option in known)
        if low > high:
            names = ", ".join(
                f"{_label(option)} ({option.nozzle_temp_min}-{option.nozzle_temp_max} °C)"
                for _, option in known
            )
            warnings.append(
                FilamentWarning(
                    kind="temperature",
                    message=(
                        f"These do not share a nozzle temperature and should not print on one "
                        f"plate: {names}."
                    ),
                )
            )
    return warnings


def _label(option: SpoolOption) -> str:
    parts = [part for part in (option.brand, option.material, option.subtype) if part]
    name = " ".join(parts)
    return f"{name} {option.color_name}".strip() if option.color_name else name or "this spool"


def queue_filaments(options: FilamentOptions, plan: FilamentPlan) -> QueueFilaments:
    """The plan as ``PrintQueueItemCreate``'s three filament fields.

    ``ams_mapping`` is positional — index ``slot_id - 1``, value the flat tray id — and
    a slot whose spool is not loaded gets Bambuddy's own ``-1`` sentinel rather than
    being dropped, because dropping it would shift every later slot onto the wrong tray.
    """
    by_id = {option.spool_id: option for option in options.spools}
    width = max((slot.slot_id for slot in options.slots), default=0)
    mapping = [-1] * width
    overrides: list[dict[str, object]] = []
    types: list[str] = []

    for slot in options.slots:
        spool_id = plan.spool_for(slot.slot_id)
        option = by_id.get(spool_id) if spool_id is not None else None
        if option is None:
            continue
        # A tray id only addresses a tray of *this* printer. A spool loaded in another
        # machine is a legitimate choice — it produces a "move it here" warning — but
        # mapping the slot onto its tray number would address whatever this printer
        # happens to have in the same physical position, which is a different filament.
        # ``-1`` says "unresolved", which is what it genuinely is until someone loads it.
        if option.loaded is not None and (
            options.printer_id is None or option.loaded.printer_id == options.printer_id
        ):
            mapping[slot.slot_id - 1] = option.loaded.global_tray_id
        override: dict[str, object] = {
            "slot_id": slot.slot_id,
            "type": option.material,
            "color": option.colour or slot.colour,
        }
        if slot.used_grams is not None:
            override["used_grams"] = slot.used_grams
        if plan.force_colour_match:
            override["force_color_match"] = True
        overrides.append(override)
        if option.material not in types:
            types.append(option.material)

    return QueueFilaments(
        ams_mapping=mapping,
        filament_overrides=overrides,
        required_filament_types=types,
    )


def slice_filament_presets(
    options: FilamentOptions,
    plan: FilamentPlan,
    *,
    pipeline_presets: list[PresetRef],
    resolve: dict[str, PresetRef],
) -> tuple[list[PresetRef], list[str], list[FilamentWarning]]:
    """The filament presets and colours to slice this plan with.

    The pipeline's own presets are the baseline and stay in place wherever the chosen
    spool cannot be resolved to a real :class:`PresetRef` — ``slicer_filament`` is a
    bare string ("GFG00", or a local preset's row id "2"), and inventing a
    ``PresetRef`` source for one that is not in the catalogue would send Bambuddy an id
    it cannot look up. ``resolve`` is that catalogue, keyed by preset id.

    The nozzle-specific preset wins when the process preset names a diameter: the same
    spool slices as a different profile through a 0.2 than through a 0.4.
    """
    by_id = {option.spool_id: option for option in options.spools}
    presets = list(pipeline_presets)
    colours: list[str] = []
    warnings: list[FilamentWarning] = []
    width = max((slot.slot_id for slot in options.slots), default=0)
    if not presets:
        # A pipeline always carries at least one filament preset (Bambuddy's own
        # ``minItems: 1``), so this is unreachable through the picker — but padding an
        # empty list would mean inventing a preset id, and an id Bambuddy cannot look
        # up fails the slice with a message about a preset nobody chose.
        return (
            [],
            [],
            [
                FilamentWarning(
                    kind="no-choice",
                    message=(
                        "This pipeline carries no filament preset, so the plate cannot be sliced."
                    ),
                )
            ],
        )
    while len(presets) < width:
        presets.append(presets[-1])

    for slot in options.slots:
        option = by_id.get(plan.spool_for(slot.slot_id) or -1)
        colours.append((option.colour if option else slot.colour) or "#FFFFFF")
        if option is None:
            continue
        wanted = options.process_nozzle_diameter
        candidate = option.nozzle_presets.get(wanted) if wanted else None
        candidate = candidate or option.slicer_filament
        if candidate is None:
            continue
        ref = resolve.get(candidate)
        if ref is None:
            warnings.append(
                FilamentWarning(
                    kind="nozzle-mismatch",
                    slot_id=slot.slot_id,
                    message=(
                        f"Bambuddy has no slicer preset called {candidate!r} for "
                        f"{_label(option)}, so the pipeline's own filament preset is used for "
                        "this slot."
                    ),
                )
            )
            continue
        presets[slot.slot_id - 1] = ref

    return presets, colours, warnings


async def gather_options(
    client: BambuddyClient,
    *,
    library_file_id: int,
    printer_id: int | None = None,
    plate_id: int | None = None,
    process_preset_name: str | None = None,
    fallback_colours: list[str] | None = None,
) -> FilamentOptions:
    """Read Bambuddy once for everything the filament step needs.

    The per-spool preset reads are the only per-row calls, and they are made only for
    the spools that could plausibly serve a slot — asking for all of them would be one
    request per spool in the whole inventory for a two-colour keychain.
    """
    spools = await client.spools()
    assignments = await client.spool_assignments()
    requirements = await _requirements(client, library_file_id, plate_id, fallback_colours)

    printer = None
    status = None
    slot_materials: list[SlotMaterial] = []
    if printer_id is not None:
        printer = await client.printer(printer_id)
        status = await client.printer_status(printer_id)
        slot_materials = (await client.inventory_remain(printer_id)).slot_materials

    wanted = sorted(
        spool.id
        for spool in spools
        if any(
            not need.material or need.material.upper() == spool.material.upper()
            for need in requirements
        )
    )
    # Concurrently, and for the same reason ``check_pipelines`` gathers its eligibility
    # checks: the picker cannot open until the last answer arrives, so a sequential loop
    # would cost the sum of every spool's latency rather than the slowest one's. An
    # unsliced plate declares no material, so "could serve a slot" is the whole
    # inventory — a dozen round trips one after another is the dialog's opening delay.
    fetched = await asyncio.gather(
        *(client.spool_filament_presets(spool_id) for spool_id in wanted)
    )
    presets_by_spool = dict(zip(wanted, fetched, strict=True))

    return build_options(
        library_file_id=library_file_id,
        spools=spools,
        assignments=assignments,
        requirements=requirements,
        printer=printer,
        status=status,
        slot_materials=slot_materials,
        presets_by_spool=presets_by_spool,
        process_preset_name=process_preset_name,
    )


async def _requirements(
    client: BambuddyClient,
    library_file_id: int,
    plate_id: int | None,
    fallback_colours: list[str] | None,
) -> list[SlotNeed]:
    """The plate's slots, from Bambuddy, falling back to the output's own colours.

    Bambuddy reads them out of the 3MF's ``Metadata/slice_info.config``, which a plate
    ScadBuddy generated does carry. If it ever does not, the output's colour list is
    the same information in the same order, so the picker still opens.
    """
    answer = await client.filament_requirements(library_file_id, plate_id=plate_id)
    slots = [
        SlotNeed(
            slot_id=filament.slot_id,
            material=filament.type or None,
            colour=normalise_colour(filament.color),
            # 0 g is what an unsliced plate reports for every slot; it is unknown, and
            # saying so is what keeps the "enough filament left" rule honest.
            used_grams=filament.used_grams or None,
        )
        for filament in answer.filaments
        if filament.used_in_plate
    ]
    if slots:
        return slots
    logger.info(
        "Bambuddy read no filament slots from the 3MF; using the output's own colours",
        extra={"library_file_id": library_file_id},
    )
    return [
        SlotNeed(slot_id=index + 1, colour=normalise_colour(colour))
        for index, colour in enumerate(fallback_colours or [])
    ]
