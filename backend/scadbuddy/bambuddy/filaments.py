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

import asyncio
import logging
import math
from collections.abc import Callable, Sequence
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import (
    NozzleInfo,
    Printer,
    SlotChoice,
    SlotMaterial,
    Spool,
    SpoolAssignment,
)

logger = logging.getLogger(__name__)

#: How far apart two colours may be, as a CIEDE2000 colour difference, and still be
#: pre-selected. About 2 is "just noticeable" and 100 is black against white, so this
#: is "recognisably the same colour": `#FF0000` matches an opaque red filament
#: (10-12), a navy matches a royal blue (12), and a royal blue does not match a
#: greyish Misty Blue (20) or a blue a pink (37). Kept this tight because a loaded
#: spool inside it beats an exact match on the shelf. It only seeds the picker's
#: opening selection; every slot stays editable and nothing is decided by it.
COLOUR_MATCH_DISTANCE = 15.0

WarningKind = Literal[
    "not-loaded",
    "low-filament",
    "no-choice",
    "no-preset",
    "no-fan-out",
    "mixed-sizes",
    "no-process",
    "not-installed",
    "plate-differs",
    "hf-unsupported",
    "hf-mounted",
    "rack-unsafe-material",
    "rack-left-to-bambuddy",
    "rack-manual-partial",
]


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
    #: The physical extruder this spool feeds on the chosen printer — 0 right, 1 left —
    #: and the letter for it (#469). ``None`` when the spool is not loaded there or the
    #: printer does not say. A label only: the run checks no spool against a nozzle (#768).
    extruder: int | None = None
    side: Literal["L", "R"] | None = None


class SlotNeed(BaseModel):
    """One filament slot of the plate, as Bambuddy reads the 3MF. ``slot_id`` is 1-based."""

    slot_id: int
    material: str | None = None
    colour: str | None = None
    #: Grams this slot needs for one copy, or ``None`` when the 3MF carries no slice
    #: info — which is every ScadBuddy upload before it has been sliced.
    used_grams: float | None = None
    #: The spools whose colour still fits this slot's (`colours_match`): only these
    #: may keep a remembered choice for it (#933). Filled in by `build_options`.
    colour_matches: list[int] = Field(default_factory=list)


class FilamentWarning(BaseModel):
    """Advisory, never blocking. Bambuddy's own refusals are the eligibility report."""

    kind: WarningKind
    slot_id: int | None = None
    message: str


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
    #: The chosen printer's mounted nozzles, one per extruder (#78). Empty without a
    #: printer: a class target nobody has narrowed yet has no hardware to read.
    nozzles: list[NozzleInfo] = Field(default_factory=list)
    #: The chosen printer has the Filament Track Switch (#469): any AMS reaches either
    #: nozzle, so a spool's ``side`` is only where its inlet rests, not a constraint.
    track_switch: bool = False


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
    """The CIEDE2000 difference of two sRGB colours, or ``None`` when either side
    has no colour (#943: plain RGB distance put every real red over the cut-off).

    ``None`` rather than ``0.0``: "no colour on either side" is not a perfect match,
    and returning zero would make every unpainted slot claim every spool.
    """
    first, second = normalise_colour(left), normalise_colour(right)
    if first is None or second is None:
        return None
    return ciede2000(_lab(first), _lab(second))


Lab = tuple[float, float, float]


def _lab(colour: str) -> Lab:
    """A ``#RRGGBB`` sRGB colour in CIE L*a*b*, against the D65 white."""
    srgb = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    red, green, blue = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb)
    x = (0.4124564 * red + 0.3575761 * green + 0.1804375 * blue) / 0.95047
    y = 0.2126729 * red + 0.7151522 * green + 0.0721750 * blue
    z = (0.0193339 * red + 0.1191920 * green + 0.9503041 * blue) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def ciede2000(first: Lab, second: Lab) -> float:
    """The CIEDE2000 colour difference (Sharma, Wu and Dalal, 2005), kL = kC = kH = 1."""
    l1, a1, b1 = first
    l2, a2, b2 = second
    c_mean = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(c_mean**7 / (c_mean**7 + 25**7)))
    a1p, a2p = a1 * (1 + g), a2 * (1 + g)
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0

    delta_l = l2 - l1
    delta_c = c2p - c1p
    if c1p * c2p == 0:
        delta_h = 0.0
    elif abs(h2p - h1p) <= 180:
        delta_h = h2p - h1p
    elif h2p - h1p > 180:
        delta_h = h2p - h1p - 360
    else:
        delta_h = h2p - h1p + 360
    delta_big_h = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(delta_h / 2))

    l_mean = (l1 + l2) / 2
    cp_mean = (c1p + c2p) / 2
    if c1p * c2p == 0:
        h_mean = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        h_mean = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        h_mean = (h1p + h2p + 360) / 2
    else:
        h_mean = (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(h_mean - 30))
        + 0.24 * math.cos(math.radians(2 * h_mean))
        + 0.32 * math.cos(math.radians(3 * h_mean + 6))
        - 0.20 * math.cos(math.radians(4 * h_mean - 63))
    )
    delta_theta = 30 * math.exp(-(((h_mean - 275) / 25) ** 2))
    r_c = 2 * math.sqrt(cp_mean**7 / (cp_mean**7 + 25**7))
    s_l = 1 + 0.015 * (l_mean - 50) ** 2 / math.sqrt(20 + (l_mean - 50) ** 2)
    s_c = 1 + 0.045 * cp_mean
    s_h = 1 + 0.015 * cp_mean * t
    r_t = -math.sin(math.radians(2 * delta_theta)) * r_c
    lightness, chroma, hue = delta_l / s_l, delta_c / s_c, delta_big_h / s_h
    return math.sqrt(lightness**2 + chroma**2 + hue**2 + r_t * chroma * hue)


def colours_match(slot_colour: str | None, spool_colour: str | None) -> bool:
    """Whether a spool's colour is close enough to a slot's for a remembered choice of
    it to be kept: within ``COLOUR_MATCH_DISTANCE``, the cut-off the auto-match uses,
    or with no colour on either side to tell them apart (#933)."""
    distance = colour_distance(slot_colour, spool_colour)
    return distance is None or distance <= COLOUR_MATCH_DISTANCE


def seed_plan(options: FilamentOptions, remembered: Sequence[SlotChoice]) -> FilamentPlan:
    """The picker's opening plan: a slot's remembered spool while it is still in the
    inventory and its colour still fits the slot (#933), the auto-match otherwise.

    The dialog seeds itself the same way (``seedPlan`` in ``frontend/src/lib/filaments.ts``),
    reading each slot's ``colour_matches``.
    """
    chosen = {choice.slot_id: choice for choice in options.suggested}
    for slot in options.slots:
        kept = next(
            (
                choice
                for choice in remembered
                if choice.slot_id == slot.slot_id and choice.spool_id in slot.colour_matches
            ),
            None,
        )
        if kept is not None:
            chosen[slot.slot_id] = kept
    return FilamentPlan(slots=[chosen[slot_id] for slot_id in sorted(chosen)])


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
        slots=[
            slot.model_copy(
                update={
                    "colour_matches": [
                        option.spool_id
                        for option in options
                        if colours_match(slot.colour, option.colour)
                    ]
                }
            )
            for slot in requirements
        ],
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
    if distance is None and normalise_colour(slot.colour) is not None:
        # A spool with no colour says nothing about a slot that has one: opening with
        # it reads as a match (#943). The slot is left for the user instead.
        return None
    if distance is None:
        # No colour to go on in the slot: material alone is a weak but real match.
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


def across_plates(plates: list[FilamentOptions]) -> FilamentOptions:
    """The first plate's options with each slot's grams summed over every plate (#198).

    What an all-plates print needs from a spool is the total, not any one plate's
    share: every plate can fit what is left and the run as a whole still not. A slot
    only some plates use counts only on those, and a plate with no slice info adds
    nothing, which is the same "unknown" a single plate's ``None`` is.
    """
    slots: dict[int, SlotNeed] = {}
    for options in plates:
        for slot in options.slots:
            seen = slots.get(slot.slot_id)
            if seen is None:
                slots[slot.slot_id] = slot
            elif slot.used_grams is not None:
                total = (seen.used_grams or 0.0) + slot.used_grams
                slots[slot.slot_id] = seen.model_copy(update={"used_grams": total})
    return plates[0].model_copy(update={"slots": sorted(slots.values(), key=_slot_order)})


def _slot_order(slot: SlotNeed) -> int:
    return slot.slot_id


def every_plate(plates: list[FilamentOptions]) -> FilamentOptions:
    """The filament step for an all-plates print: one row per slot any plate uses.

    :func:`across_plates` with the opening selection and its warnings recomputed over
    the union, so a slot only a later plate uses is offered and pre-selected too (spec
    §2 step 1) rather than reaching the run with no spool.
    """
    merged = across_plates(plates)
    merged.suggested = suggest(merged.slots, merged.spools, printer_id=merged.printer_id)
    merged.warnings = check(merged, FilamentPlan(slots=merged.suggested), copies=1)
    return merged


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


async def gather_options(
    client: BambuddyClient,
    *,
    library_file_id: int,
    printer_id: int | None = None,
    plate_id: int | None = None,
    fallback_colours: list[str] | None = None,
    own_colours: list[str] | None = None,
) -> FilamentOptions:
    """Read Bambuddy once for everything the filament step needs.

    ``own_colours`` replace the colors the file reports, slot by slot: the file was
    recolored for a run's spools (#457), and the step shows the model's.
    """
    (options,) = await gather_plate_options(
        client,
        library_file_id=library_file_id,
        printer_id=printer_id,
        plate_ids=[plate_id],
        fallback_colours=fallback_colours,
        own_colours=own_colours,
    )
    return options


async def gather_plate_options(
    client: BambuddyClient,
    *,
    library_file_id: int,
    printer_id: int | None = None,
    plate_ids: Sequence[int | None],
    fallback_colours: list[str] | None = None,
    own_colours: list[str] | None = None,
) -> list[FilamentOptions]:
    """:func:`gather_options` for several plates of one file, in ``plate_ids`` order.

    The spools, assignments and printer are the same for every plate, so they are read
    once (#480); only each plate's slots are read per plate, concurrently. The reads
    keep the single-plate order, spools, assignments, the plates, then the printer and
    its inventory-remain, so the same failure surfaces either way: a plate's error beats
    the printer's, and among plates the first failing one in ``plate_ids`` order wins."""
    spools = await client.spools()
    assignments = await client.spool_assignments()

    answers = await asyncio.gather(
        *(
            _requirements(client, library_file_id, plate, fallback_colours, own_colours)
            for plate in plate_ids
        ),
        return_exceptions=True,
    )
    per_plate: list[list[SlotNeed]] = []
    for answer in answers:
        if isinstance(answer, BaseException):
            raise answer
        per_plate.append(answer)

    printer = None
    slot_materials: list[SlotMaterial] = []
    if printer_id is not None:
        printer = await client.printer(printer_id)
        slot_materials = (await client.inventory_remain(printer_id)).slot_materials

    return [
        build_options(
            library_file_id=library_file_id,
            spools=spools,
            assignments=assignments,
            requirements=requirements,
            printer=printer,
            slot_materials=slot_materials,
        )
        for requirements in per_plate
    ]


async def _requirements(
    client: BambuddyClient,
    library_file_id: int,
    plate_id: int | None,
    fallback_colours: list[str] | None,
    own_colours: list[str] | None = None,
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
            colour=normalise_colour(
                own_colours[filament.slot_id - 1]
                if own_colours and 0 < filament.slot_id <= len(own_colours)
                else filament.color
            ),
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
