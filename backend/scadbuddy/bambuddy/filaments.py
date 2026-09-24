"""Choosing a spool per plate slot, from Bambuddy's own inventory (#87).

This is a **picker**, not a matcher. It shows Bambuddy's spools with how much is left
on each, marks the ones already loaded and puts them first, and hands the choice
straight back to Bambuddy. ScadBuddy keeps no filament state of its own and decides no
placement: which AMS tray a chosen spool is drawn from is Bambuddy's answer, computed
by its scheduler's ``_compute_ams_mapping_for_printer`` whenever a queue item carries
no ``ams_mapping`` — which is exactly what ScadBuddy sends.

That is deliberate and was measured rather than assumed. Bambuddy's own mapping step
handles the Filament Track Switch (a switcher routes any AMS slot to either extruder,
so the per-nozzle filter must not apply — its issue #2186), the AMS-HT's own id space
and the external feeds. Recomputing any of it here would be a second, worse copy that
drifts the first time Bambuddy learns a new machine.

What ScadBuddy sends instead is the pair Bambuddy's scheduler actually matches on:

* ``filament_overrides`` — ``{slot_id, type, color, used_grams, force_color_match?}``,
  the shape its own 3MF parser produces and its scheduler validates.
* ``required_filament_types`` — the materials the chosen spools are made of.

Three facts about the data are worth keeping written down, because each one silently
changes what a reading means:

* **``used_grams: 0`` means unknown.** ScadBuddy uploads an unsliced plate, and
  ``filament-requirements`` answers 0 for every slot of one. Read as a real weight, the
  "not enough filament" warning never fires; read as "needs nothing", it claims every
  spool is sufficient. Both are wrong, so it is ``None``.
* **``inventory-remain`` is the authority on remaining weight for a loaded spool** —
  it is Bambuddy's reconciliation of the AMS against the inventory row, and an AMS
  tray's own ``remain: -1`` is what an untagged spool reports.
* **The live state is one printer's; the assignments are every printer's.**
  ``/inventory/assignments`` is deliberately unfiltered, so a spool loaded in another
  machine is still offered — but none of the chosen printer's per-slot state may be
  joined onto it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import (
    PresetRef,
    Printer,
    SlotMaterial,
    Spool,
    SpoolAssignment,
)

logger = logging.getLogger(__name__)

#: How far apart two colours may be, as a plain RGB distance, and still be pre-selected.
#: The whole cube's diagonal is ~441, so this is "recognisably the same colour" — a
#: navy and a royal blue match, a blue and a pink do not. It only seeds the picker's
#: opening selection; every slot stays editable and nothing is decided by it.
COLOUR_MATCH_DISTANCE = 48.0

WarningKind = Literal["not-loaded", "low-filament", "no-choice", "no-preset"]


class LoadedAt(BaseModel):
    """Where a spool physically is, when Bambuddy says it is loaded somewhere.

    Purely a label. It is not an address: nothing here turns it into a tray number.
    """

    printer_id: int
    printer_name: str | None = None
    ams_id: int
    tray_id: int


class SpoolOption(BaseModel):
    """One row of the picker: a spool, and where it is."""

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
    storage_location: str | None = None
    loaded: LoadedAt | None = None


class SlotNeed(BaseModel):
    """One filament slot of the plate, as Bambuddy reads the 3MF. ``slot_id`` is 1-based."""

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

    One route rather than three, because the join — inventory against assignments
    against the per-slot remaining weights — is the part that is easy to get wrong, and
    doing it in the browser would mean shipping every spool's history to do it.
    """

    library_file_id: int
    printer_id: int | None = None
    printer_name: str | None = None
    slots: list[SlotNeed] = Field(default_factory=list)
    spools: list[SpoolOption] = Field(default_factory=list)
    #: The picker's opening selection, so the common case needs no clicks.
    suggested: list[SlotChoice] = Field(default_factory=list)
    #: Warnings for :attr:`suggested`, so the dialog can show them before any click.
    warnings: list[FilamentWarning] = Field(default_factory=list)


class QueueFilaments(BaseModel):
    """The two ``PrintQueueItemCreate`` filament fields a plan turns into.

    There is no ``ams_mapping`` here on purpose — see the module docstring. Neither
    field exists on ``PipelineRunCreateRequest``, which is why choosing spools forces
    the slice + queue route: that is Bambuddy's API shape, not a decision taken here.
    """

    #: ``{slot_id, type, color, used_grams, force_color_match?}`` — the shape Bambuddy's
    #: scheduler validates, and the one its own 3MF parser produces.
    filament_overrides: list[dict[str, object]] = Field(default_factory=list)
    required_filament_types: list[str] = Field(default_factory=list)


def normalise_colour(raw: str | None) -> str | None:
    """``RRGGBBAA``, ``#RRGGBBAA``, ``#RRGGBB`` → ``#RRGGBB``; anything else → ``None``.

    The alpha byte is dropped rather than kept: nothing compares it, and leaving it on
    would make the same colour unequal to itself across two of Bambuddy's own routes.
    """
    if not raw:
        return None
    text = raw.strip().lstrip("#")
    if len(text) not in (6, 8):
        return None
    try:
        int(text[:6], 16)
    except ValueError:
        return None
    return f"#{text[:6].upper()}"


def colour_distance(left: str | None, right: str | None) -> float | None:
    """Plain RGB distance, or ``None`` when either side has no colour.

    ``None`` rather than ``0.0``: "no colour on either side" is not a perfect match,
    and returning zero would make every unpainted slot claim every spool.
    """
    first, second = normalise_colour(left), normalise_colour(right)
    if first is None or second is None:
        return None
    pairs = [(int(first[i : i + 2], 16), int(second[i : i + 2], 16)) for i in (1, 3, 5)]
    return float(float(sum((a - b) ** 2 for a, b in pairs)) ** 0.5)


def build_options(
    *,
    library_file_id: int,
    spools: list[Spool],
    assignments: list[SpoolAssignment],
    requirements: list[SlotNeed],
    printer: Printer | None = None,
    slot_materials: list[SlotMaterial] | None = None,
) -> FilamentOptions:
    """Join everything already fetched into what the dialog renders.

    Kept free of the client so the join — the part with the traps in it — is testable
    against recorded bodies without any HTTP at all.
    """
    by_slot = {(material.ams_id, material.tray_id): material for material in (slot_materials or [])}
    # Last assignment wins: Bambuddy keeps history rows, and a spool moved between
    # trays would otherwise render in the tray it left.
    assignment_by_spool = {assignment.spool_id: assignment for assignment in assignments}

    options: list[SpoolOption] = []
    for spool in spools:
        if spool.archived_at is not None:
            continue
        assignment = assignment_by_spool.get(spool.id)
        # ``inventory-remain`` was read for ``printer`` while ``/inventory/assignments``
        # covers every printer, so a spool sitting in printer B's AMS 0 slot 1 must take
        # none of printer A's AMS 0 slot 1 state — it is a different spool.
        here = assignment is not None and (printer is None or assignment.printer_id == printer.id)
        slot_material = (
            by_slot.get((assignment.ams_id, assignment.tray_id)) if here and assignment else None
        )
        remaining = spool.remaining_g
        if slot_material is not None and slot_material.remaining_g is not None:
            remaining = slot_material.remaining_g
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
                storage_location=spool.storage_location,
                loaded=(
                    LoadedAt(
                        printer_id=assignment.printer_id,
                        printer_name=assignment.printer_name,
                        ams_id=assignment.ams_id,
                        tray_id=assignment.tray_id,
                    )
                    if assignment
                    else None
                ),
            )
        )

    options.sort(key=_display_order(printer.id if printer else None))
    suggested = suggest(requirements, options, printer_id=printer.id if printer else None)
    built = FilamentOptions(
        library_file_id=library_file_id,
        printer_id=printer.id if printer else None,
        printer_name=printer.name if printer else None,
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
    """Pre-select a spool per slot, by material then colour.

    This only fills the picker in so the common case needs no clicks; it decides
    nothing. A spool already taken by an earlier slot is not offered again, because a
    two-colour plate opening with the same spool in both slots reads as a bug.
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
    """Lower is better; ``None`` means this spool is not offered for this slot.

    A slot that declares a material only ever pre-selects that material — opening with
    a PETG in a PLA slot would be a worse failure than opening with nothing, because it
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
    """The two things that fall straight out of the data, and nothing else.

    There is no compatibility rules engine here on purpose: whether two filaments can
    share a plate, whether a spool can reach the extruder that slices it and whether a
    printer can run the job at all are Bambuddy's questions, answered by its own
    eligibility report, which the dialog shows verbatim beside these.
    """
    by_id = {option.spool_id: option for option in options.spools}
    warnings: list[FilamentWarning] = []

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
        warnings.extend(_slot_warnings(options, slot, option, copies=copies))

    return warnings


def _slot_warnings(
    options: FilamentOptions, slot: SlotNeed, option: SpoolOption, *, copies: int
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

    return found


def _label(option: SpoolOption) -> str:
    parts = [part for part in (option.brand, option.material, option.subtype) if part]
    name = " ".join(parts)
    return f"{name} {option.color_name}".strip() if option.color_name else name or "this spool"


def queue_filaments(options: FilamentOptions, plan: FilamentPlan) -> QueueFilaments:
    """The plan as ``PrintQueueItemCreate``'s filament fields.

    No ``ams_mapping``: Bambuddy computes it from these, against the printer it is
    actually dispatching to and the switcher that printer actually has.
    """
    by_id = {option.spool_id: option for option in options.spools}
    overrides: list[dict[str, object]] = []
    types: list[str] = []

    for slot in options.slots:
        spool_id = plan.spool_for(slot.slot_id)
        option = by_id.get(spool_id) if spool_id is not None else None
        if option is None:
            continue
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

    return QueueFilaments(filament_overrides=overrides, required_filament_types=types)


def slice_filament_presets(
    options: FilamentOptions,
    plan: FilamentPlan,
    *,
    pipeline_presets: list[PresetRef],
    resolve: dict[str, PresetRef],
) -> tuple[list[PresetRef], list[str], list[FilamentWarning]]:
    """The filament presets and colours to slice this plan with.

    A spool names its own slicer preset (``slicer_filament``, e.g. ``"GFG00"``), so
    that is the one used — no preset is chosen for it here. The pipeline's own presets
    are the baseline and stay in place wherever the spool names none or names one
    Bambuddy's catalogue cannot look up, because sending an id it cannot resolve fails
    the slice naming a preset nobody chose. ``resolve`` is that catalogue, by preset id.
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
        if option is None or option.slicer_filament is None:
            continue
        ref = resolve.get(option.slicer_filament)
        if ref is None:
            warnings.append(
                FilamentWarning(
                    kind="no-preset",
                    slot_id=slot.slot_id,
                    message=(
                        f"Bambuddy has no slicer preset called {option.slicer_filament!r} for "
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
    fallback_colours: list[str] | None = None,
) -> FilamentOptions:
    """Read Bambuddy once for everything the filament step needs."""
    spools = await client.spools()
    assignments = await client.spool_assignments()
    requirements = await _requirements(client, library_file_id, plate_id, fallback_colours)

    printer = None
    slot_materials: list[SlotMaterial] = []
    if printer_id is not None:
        printer = await client.printer(printer_id)
        slot_materials = (await client.inventory_remain(printer_id)).slot_materials

    return build_options(
        library_file_id=library_file_id,
        spools=spools,
        assignments=assignments,
        requirements=requirements,
        printer=printer,
        slot_materials=slot_materials,
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
            # saying so is what keeps the "enough filament left" warning honest.
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
