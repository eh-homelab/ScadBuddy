"""Spool-first printing (spec 2026-09-27 §4): choices in, every slicer preset out.

A pure function. Callers read Bambuddy (catalogue, spools, per-spool presets) and pass
it in, so nothing here makes a network call and every rule is tested from recordings.

Presets are matched by **name and compatible printer**, never by id: the cloud tier
spells an id ``GP243`` and the standard tier spells the same preset by its name, and
either tier may be the only one present (Bambu Cloud logged out).

The spec's original plan of a ScadBuddy-authored printer preset was dropped (spec
2026-09-27 §4.1, after §5's live tests): Bambuddy rejects every ``source: "local"``
printer preset with 400 "The selected printer is not compatible with the process
preset in the 3mf." So ScadBuddy never invents its own printer preset for High Flow or mixed
nozzle sizes — it always names Bambu's own preset for ``nozzles[0].size`` and refuses
mixed sizes outright rather than offering an override. High Flow needs no preset: the
print run states each side's flow in the 3MF, as Bambu Studio does (#484,
:func:`scadbuddy.bambuddy.extruders.slicer_volume_types`).
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.catalogue import Catalogue, PresetChoice
from scadbuddy.bambuddy.filaments import (
    FilamentOptions,
    FilamentPlan,
    FilamentWarning,
    SpoolOption,
    spool_label,
)
from scadbuddy.bambuddy.models import (
    FlowType,
    NozzleChoice,
    NozzleSize,
    PresetRef,
    SpoolFilamentPreset,
    Tier,
)

logger = logging.getLogger(__name__)

#: The nozzle types moved to ``models`` so the settings store can remember them; they
#: are re-exported here, where every caller already imports them from.
__all__ = [
    "DEFAULT_BED",
    "PRINTER_MODEL",
    "TIERS",
    "FlowType",
    "NozzleChoice",
    "NozzleSize",
    "PrintChoices",
    "Resolved",
    "Tier",
    "choice_errors",
    "printer_preset_name",
    "resolve",
]

PRINTER_MODEL = "H2C"
DEFAULT_BED = "Textured PEI Plate"

#: Spec §4.2. Bambu's own default for a size is Standard on 0.2/0.4 and Draft on
#: 0.6/0.8, where it is already the thickest profile Bambu ships.
TIERS: dict[str, dict[str, str]] = {
    "0.2": {
        "fine": "0.08mm High Quality @BBL H2C 0.2 nozzle",
        "standard": "0.10mm Standard @BBL H2C 0.2 nozzle",
        "draft": "0.12mm Balanced Quality @BBL H2C 0.2 nozzle",
    },
    "0.4": {
        "fine": "0.12mm High Quality @BBL H2C",
        "standard": "0.20mm Standard @BBL H2C",
        "draft": "0.24mm Standard @BBL H2C",
    },
    "0.6": {
        "fine": "0.18mm Balanced Quality @BBL H2C 0.6 nozzle",
        "standard": "0.24mm Balanced Quality @BBL H2C 0.6 nozzle",
        "draft": "0.30mm Standard @BBL H2C 0.6 nozzle",
    },
    "0.8": {
        "fine": "0.24mm Balanced Quality @BBL H2C 0.8 nozzle",
        "standard": "0.32mm Balanced Quality @BBL H2C 0.8 nozzle",
        "draft": "0.40mm Standard @BBL H2C 0.8 nozzle",
    },
}

#: Source order when the same preset is in several tiers: the cloud id is what
#: Bambuddy's own pipelines and the filament intake use.
_SOURCE_ORDER = {"cloud": 0, "local": 1, "standard": 2, "orca_cloud": 3}


class PrintChoices(BaseModel):
    #: The left side first and the right second, as the dialog's nozzle step lists them;
    #: one entry is both sides'. The flow each side is sliced for is read in this order
    #: (``extruders._flows``, #484), so it must not change.
    nozzles: list[NozzleChoice] = Field(min_length=1, max_length=2)
    tier: Tier | None = "standard"
    process_name: str | None = None
    bed_type: str = Field(default=DEFAULT_BED, max_length=64)
    filament_overrides: dict[int, PresetRef] = Field(default_factory=dict)
    #: Advanced (#2166): a slot printed on the side chosen by hand, "L" or "R", in place
    #: of the side ScadBuddy plans for it (``nozzle_plan``). A slot not named is planned.
    sides: dict[int, Literal["L", "R"]] = Field(default_factory=dict)


class Resolved(BaseModel):
    printer_preset_name: str
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    filament_presets: list[PresetRef] = Field(default_factory=list)
    filament_colours: list[str] = Field(default_factory=list)
    bed_type: str = DEFAULT_BED
    warnings: list[FilamentWarning] = Field(default_factory=list)
    errors: list[FilamentWarning] = Field(default_factory=list)


def _bambu_printer(size: str) -> str:
    return f"Bambu Lab {PRINTER_MODEL} {size} nozzle"


def printer_preset_name(nozzles: list[NozzleChoice]) -> str:
    """Bambu's own printer preset for ``nozzles[0]``'s size.

    Spec §4.1: Bambuddy 400s on any ``source: "local"`` printer preset, so there is
    no ScadBuddy-authored name for High Flow or mixed sizes — every flow combination
    resolves to the same Bambu preset name (the flow goes in the 3MF, #484), and
    ``resolve`` is what adds the ``mixed-sizes`` error.
    """
    return _bambu_printer(nozzles[0].size)


def _best(rows: list[PresetChoice]) -> PresetRef | None:
    if not rows:
        return None
    return min(rows, key=lambda row: _SOURCE_ORDER.get(row.ref.source, 9)).ref


def _named(rows: list[PresetChoice], name: str, printer: str | None = None) -> PresetRef | None:
    return _best(
        [
            row
            for row in rows
            if row.name == name and (printer is None or printer in row.compatible_printers)
        ]
    )


def _fits(rows: list[PresetChoice], prefix: str, printer: str) -> PresetRef | None:
    """The preset whose name is ``prefix`` or ``prefix + " <size> nozzle"`` and which
    declares ``printer`` — Bambu names its 0.4 (or 0.6/0.8) profile without a suffix."""
    return _best(
        [
            row
            for row in rows
            if (row.name == prefix or row.name.startswith(f"{prefix} "))
            and printer in row.compatible_printers
        ]
    )


def _by_filament_id(rows: list[PresetChoice], filament_id: str, printer: str) -> PresetRef | None:
    """The preset whose ``filament_id`` is a tray's ``tray_info_idx`` and which declares
    ``printer``: one per printer and nozzle in the BBL tree (``slicing/catalogue.py``).
    Only the image's catalogue carries ``filament_id``; Bambuddy's rows never match."""
    return _best(
        [
            row
            for row in rows
            if row.filament_id == filament_id and printer in row.compatible_printers
        ]
    )


def _record_preset(catalogue: Catalogue, option: SpoolOption, printer: str) -> PresetRef | None:
    """An untagged tray's preset from the spool providers' records of its spool (#3818
    D1), in their priority order: Bambuddy's ``slicer_filament`` is a ``filament_id``
    (``GFA00``) or a cloud ``setting_id`` (``GFSL99_20``), as
    ``tests/bambuddy/recordings/inventory-spools.json`` shows; anything else (a local
    preset's id) is no native preset, and the slot falls back as any tray does."""
    for record in option.records.values():
        if not record.preset:
            continue
        ref = _by_filament_id(catalogue.filament, record.preset, printer)
        if ref is None:
            cloud = catalogue.native_ref(PresetRef(source="cloud", id=record.preset), "filament")
            fits = cloud is not None and any(
                row.name == cloud.id and printer in row.compatible_printers
                for row in catalogue.filament
            )
            ref = cloud if fits else None
        if ref is not None:
            return ref
    return None


#: What a ref the image's catalogue does not hold is, by its source (#3818 D5): a
#: standard one is Bambu Studio's, from a version this image does not ship.
_NOT_NATIVE: dict[str, str] = {
    "standard": "not one of Bambu Studio's own presets in this version",
    "cloud": "a Bambu Cloud user preset",
    "local": "a Bambuddy local preset",
    "orca_cloud": "an OrcaSlicer cloud preset",
}


def _override_problem(
    ref: PresetRef, rows: list[PresetChoice], printer: str, size: str, owner: str = "Bambuddy"
) -> str | None:
    """Why an Advanced override cannot slice, or ``None`` when it can. An empty
    ``compatible_printers`` declares no restriction (see :class:`PresetChoice`)."""
    matches = [row for row in rows if row.ref == ref]
    if not matches:
        return f"isn't one {owner} has any more"
    if not any(
        not row.compatible_printers or printer in row.compatible_printers for row in matches
    ):
        return f"doesn't fit a {size} mm nozzle"
    return None


class _ChoiceResult(BaseModel):
    printer_preset_name: str
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    warnings: list[FilamentWarning] = Field(default_factory=list)
    errors: list[FilamentWarning] = Field(default_factory=list)


def _resolve_choices(choices: PrintChoices, catalogue: Catalogue) -> _ChoiceResult:
    """Everything the choices decide on their own, without any plate's slots: the
    nozzle sizes, the printer preset and the process preset."""
    warnings: list[FilamentWarning] = []
    errors: list[FilamentWarning] = []
    sizes = {nozzle.size for nozzle in choices.nozzles}
    size = choices.nozzles[0].size
    if len(sizes) > 1:
        errors.append(
            FilamentWarning(
                kind="mixed-sizes",
                message="The two nozzles are different sizes. "
                f"{catalogue.slicer} can't slice mixed nozzle sizes yet.",
            )
        )
    if len({nozzle.flow for nozzle in choices.nozzles}) > 1:
        # Print #355 stalled on the H2C with the left High Flow and the right Standard,
        # which the owner rules undefined on this printer (#4272).
        errors.append(
            FilamentWarning(
                kind="mixed-flows",
                message="One nozzle is High Flow and the other Standard. A print can't "
                "mix High Flow and Standard: choose one flow for both.",
            )
        )

    name = printer_preset_name(choices.nozzles)
    printer_ref = _named(catalogue.printer, name)
    if printer_ref is None:
        errors.append(
            FilamentWarning(
                kind="no-preset",
                message=f"{catalogue.owner} has no printer preset {name!r}.",
            )
        )

    printer = _bambu_printer(size)
    process_name = choices.process_name or TIERS[size][choices.tier or "standard"]
    process_ref = _named(catalogue.process, process_name, printer)
    if process_ref is None:
        errors.append(
            FilamentWarning(
                kind="no-process",
                message=f"{process_name!r} is not a process {catalogue.owner} has for a "
                f"{size} mm nozzle.{catalogue.native_note}",
            )
        )
    return _ChoiceResult(
        printer_preset_name=name,
        printer_preset=printer_ref,
        process_preset=process_ref,
        warnings=warnings,
        errors=errors,
    )


def process_preset_for(choices: PrintChoices, catalogue: Catalogue) -> PresetRef | None:
    """The process preset the choices resolve to, or ``None`` (which :func:`resolve`
    reports as an error)."""
    return _resolve_choices(choices, catalogue).process_preset


def choice_errors(choices: PrintChoices, catalogue: Catalogue) -> list[FilamentWarning]:
    """The errors :func:`resolve` would report from the choices alone, before any plate
    is read — so a caller can refuse before uploading anything. Slot errors (no spool,
    no filament preset, an unusable Advanced override) need the plate's slots, which
    only a library file answers, and so are left to :func:`resolve`."""
    return _resolve_choices(choices, catalogue).errors


def resolve(
    options: FilamentOptions,
    plan: FilamentPlan,
    choices: PrintChoices,
    catalogue: Catalogue,
    spool_presets: dict[int, list[SpoolFilamentPreset]],
) -> Resolved:
    chosen = _resolve_choices(choices, catalogue)
    warnings = list(chosen.warnings)
    errors = list(chosen.errors)
    size = choices.nozzles[0].size
    printer = _bambu_printer(size)

    by_id = {option.spool_id: option for option in options.spools}
    #: Bambuddy's slice expects a per-AMS-slot array indexed by ``slot_id - 1``, not a
    #: compact list over the slots the plate happens to use (a plate using only slot 2
    #: needs a length-2 array, slot 2's preset at index 1) — mirroring the old
    #: ``slice_filament_presets``.
    width = max((slot.slot_id for slot in options.slots), default=0)
    presets: list[PresetRef | None] = [None] * width
    colours: list[str | None] = [None] * width
    for slot in options.slots:
        idx = slot.slot_id - 1
        spool_id = plan.spool_for(slot.slot_id)
        # Not ``or -1``: a negative id is a tray chosen for itself (#2164).
        option = by_id.get(spool_id) if spool_id is not None else None
        colours[idx] = (option.colour if option else slot.colour) or "#FFFFFF"
        if option is None:
            # Not a preset problem, so Advanced cannot fix it: the slot needs a spool.
            errors.append(
                FilamentWarning(
                    kind="no-choice",
                    slot_id=slot.slot_id,
                    message=f"Slot {slot.slot_id} has no spool chosen.",
                )
            )
            continue
        ref = choices.filament_overrides.get(slot.slot_id)
        if ref is not None and catalogue.is_native:
            # A native run slices with the image's presets only (#3818 D5): a cloud ref
            # is the preset its setting_id names there, and Bambuddy's own are refused.
            native = catalogue.native_ref(ref, "filament")
            if native is None:
                errors.append(
                    FilamentWarning(
                        kind="no-preset",
                        slot_id=slot.slot_id,
                        message=f"The preset chosen for slot {slot.slot_id} is "
                        f"{_NOT_NATIVE[ref.source]}.{catalogue.native_note}",
                    )
                )
                continue
            ref = native
        if ref is not None:
            # Checked like every other preset (PR #335 review 2): an override left from
            # an earlier nozzle size, or since deleted in Bambuddy, would otherwise only
            # fail as an opaque error from Bambuddy's own slice.
            problem = _override_problem(ref, catalogue.filament, printer, size, catalogue.owner)
            if problem is not None:
                errors.append(
                    FilamentWarning(
                        kind="no-preset",
                        slot_id=slot.slot_id,
                        message=f"The preset chosen for slot {slot.slot_id} {problem}. "
                        "Pick another under Advanced.",
                    )
                )
                continue
        elif option.spool_id < 0 and option.slicer_filament:
            # A tray chosen for itself: its tray_info_idx is the BBL preset's filament_id.
            ref = _by_filament_id(catalogue.filament, option.slicer_filament, printer)
        if ref is None and option.spool_id < 0 and catalogue.is_native:
            ref = _record_preset(catalogue, option, printer)
        if ref is None:
            own = [
                row.slicer_filament
                for row in spool_presets.get(option.spool_id, [])
                if row.printer_model == PRINTER_MODEL and row.nozzle_diameter == size
            ]
            if catalogue.is_native:
                # A spool's preset is a Bambu Cloud setting_id (or a Bambuddy local id,
                # which the image lacks); native refs are preset names (#4349).
                mapped = {
                    preset_id: catalogue.native_ref(
                        PresetRef(source="cloud", id=preset_id), "filament"
                    )
                    for preset_id in own
                }
                if None in mapped.values():
                    # Said, so a slot that falls back to a name is not a mystery.
                    logger.debug(
                        "a spool's own preset is not in the native catalogue",
                        extra={"spool_id": option.spool_id},
                    )
                own = [native.id for native in mapped.values() if native is not None]
            ref = next(
                (
                    choice.ref
                    for preset_id in own
                    for choice in catalogue.filament
                    if choice.ref.id == preset_id
                ),
                None,
            )
            if ref is None and option.slicer_filament_name:
                ref = _fits(catalogue.filament, f"{option.slicer_filament_name} @BBL H2C", printer)
            if ref is None:
                ref = _fits(catalogue.filament, f"Generic {option.material} @BBL H2C", printer)
                if ref is not None:
                    warnings.append(
                        FilamentWarning(
                            kind="no-preset",
                            slot_id=slot.slot_id,
                            message=(
                                f"{spool_label(option)} has no {size} mm preset of its own, so "
                                f"Bambu's Generic {option.material} is used."
                            ),
                        )
                    )
        if ref is None:
            errors.append(
                FilamentWarning(
                    kind="no-preset",
                    slot_id=slot.slot_id,
                    message=(
                        f"{spool_label(option)} has no slicer preset for a {size} mm nozzle. "
                        "Pick one under Advanced."
                    ),
                )
            )
            continue
        presets[idx] = ref

    #: Padding (unused) slots get a printer-compatible preset already resolved for this
    #: plate — the first used slot's, by slot order — mirroring the old code's baseline
    #: fill rather than inventing a preset id Bambuddy cannot look up. When nothing
    #: resolved at all, every used slot already carries a "no-preset" error and the
    #: caller rejects the request before this list is ever sent to Bambuddy.
    pad_ref = next((ref for ref in presets if ref is not None), None)
    filament_presets = (
        [ref if ref is not None else pad_ref for ref in presets] if pad_ref is not None else []
    )
    filament_colours = [colour if colour is not None else "#FFFFFF" for colour in colours]

    return Resolved(
        printer_preset_name=chosen.printer_preset_name,
        printer_preset=chosen.printer_preset,
        process_preset=chosen.process_preset,
        filament_presets=filament_presets,
        filament_colours=filament_colours,
        bed_type=choices.bed_type,
        warnings=warnings,
        errors=errors,
    )
