"""Spool-first printing (spec 2026-09-27 §4): choices in, every slicer preset out.

A pure function. Callers read Bambuddy (catalogue, spools, per-spool presets) and pass
it in, so nothing here makes a network call and every rule is tested from recordings.

Presets are matched by **name and compatible printer**, never by id: the cloud tier
spells an id ``GP243`` and the standard tier spells the same preset by its name, and
either tier may be the only one present (Bambu Cloud logged out).

Controller rulings R8/R9 (task-3-addendum.md, 2026-09-27) override the original spec
brief here: a live check found Bambuddy rejects every ``source: "local"`` printer
preset with 400 "The selected printer is not compatible with the process preset in
the 3mf." So ScadBuddy never invents its own printer preset for High Flow or mixed
nozzle sizes — it always names Bambu's own preset for ``nozzles[0].size``, adds a
``hf-unsupported`` warning when High Flow was asked for, and refuses mixed sizes
outright rather than offering an override.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.catalogue import PresetChoice, _Catalogue
from scadbuddy.bambuddy.filaments import FilamentOptions, FilamentPlan, FilamentWarning, _label
from scadbuddy.bambuddy.models import (
    FlowType,
    NozzleChoice,
    NozzleSize,
    PresetRef,
    SpoolFilamentPreset,
    Tier,
)

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

#: Source order when the same preset is in several tiers: the cloud id is what every
#: existing pipeline and the filament intake use.
_SOURCE_ORDER = {"cloud": 0, "local": 1, "standard": 2, "orca_cloud": 3}


class PrintChoices(BaseModel):
    nozzles: list[NozzleChoice] = Field(min_length=1, max_length=2)
    tier: Tier | None = "standard"
    process_name: str | None = None
    bed_type: str = Field(default=DEFAULT_BED, max_length=64)
    filament_overrides: dict[int, PresetRef] = Field(default_factory=dict)


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

    Addendum R8: Bambuddy 400s on any ``source: "local"`` printer preset, so there is
    no ScadBuddy-authored name for High Flow or mixed sizes — every flow combination
    resolves to the same Bambu preset name, and ``resolve`` is what adds the
    ``hf-unsupported`` warning or the ``mixed-sizes`` error instead.
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


def _override_problem(
    ref: PresetRef, rows: list[PresetChoice], printer: str, size: str
) -> str | None:
    """Why an Advanced override cannot slice, or ``None`` when it can. An empty
    ``compatible_printers`` declares no restriction (see :class:`PresetChoice`)."""
    matches = [row for row in rows if row.ref == ref]
    if not matches:
        return "isn't one Bambuddy has any more"
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


def _resolve_choices(choices: PrintChoices, catalogue: _Catalogue) -> _ChoiceResult:
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
                message="The two nozzles are different sizes. Bambuddy can't slice "
                "mixed nozzle sizes yet.",
            )
        )

    name = printer_preset_name(choices.nozzles)
    printer_ref = _named(catalogue.printer, name)
    if printer_ref is None:
        errors.append(
            FilamentWarning(
                kind="no-preset",
                message=f"Bambuddy has no printer preset {name!r}.",
            )
        )

    if any(nozzle.flow == "high_flow" for nozzle in choices.nozzles):
        warnings.append(
            FilamentWarning(
                kind="hf-unsupported",
                message="Bambuddy slices this as Standard flow; High Flow presets "
                "aren't supported by Bambuddy yet.",
            )
        )

    printer = _bambu_printer(size)
    process_name = choices.process_name or TIERS[size][choices.tier or "standard"]
    process_ref = _named(catalogue.process, process_name, printer)
    if process_ref is None:
        errors.append(
            FilamentWarning(
                kind="no-process",
                message=f"{process_name!r} is not a process Bambuddy has for a {size} mm nozzle.",
            )
        )
    return _ChoiceResult(
        printer_preset_name=name,
        printer_preset=printer_ref,
        process_preset=process_ref,
        warnings=warnings,
        errors=errors,
    )


def choice_errors(choices: PrintChoices, catalogue: _Catalogue) -> list[FilamentWarning]:
    """The errors :func:`resolve` would report from the choices alone, before any plate
    is read — so a caller can refuse before uploading anything. Slot errors (no spool,
    no filament preset, an unusable Advanced override) need the plate's slots, which
    only a library file answers, and so are left to :func:`resolve`."""
    return _resolve_choices(choices, catalogue).errors


def resolve(
    options: FilamentOptions,
    plan: FilamentPlan,
    choices: PrintChoices,
    catalogue: _Catalogue,
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
        option = by_id.get(plan.spool_for(slot.slot_id) or -1)
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
        if ref is not None:
            # Checked like every other preset (PR #335 review 2): an override left from
            # an earlier nozzle size, or since deleted in Bambuddy, would otherwise only
            # fail as an opaque error from Bambuddy's own slice.
            problem = _override_problem(ref, catalogue.filament, printer, size)
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
        else:
            own = [
                row.slicer_filament
                for row in spool_presets.get(option.spool_id, [])
                if row.printer_model == PRINTER_MODEL and row.nozzle_diameter == size
            ]
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
                                f"{_label(option)} has no {size} mm preset of its own, so "
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
                        f"{_label(option)} has no slicer preset for a {size} mm nozzle. "
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
