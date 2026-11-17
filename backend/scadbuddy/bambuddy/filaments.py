"""Choosing a spool per plate slot (#87), from ScadBuddy's own spools (#6262 Task 7).

This is a **picker**, not a matcher. It shows the SB spools with how much is left on
each, marks the ones already loaded and puts them first, and hands the choice's type and
colour to Bambuddy. ScadBuddy decides no placement on Bambuddy's path: which AMS tray a
chosen spool is drawn from is Bambuddy's answer, computed
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
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal, Protocol
from uuid import UUID

from fastapi import status
from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.concurrent import settled
from scadbuddy.bambuddy.models import (
    FilamentRequirements,
    NozzleInfo,
    Printer,
    PrinterStatus,
    SlotChoice,
    SlotMaterial,
    SpoolAssignment,
)
from scadbuddy.core.problems import DATABASE_ERRORS, ApiError
from scadbuddy.crud.client import CrudUnavailableError
from scadbuddy.filament.colour import same_opacity
from scadbuddy.filament.models import FilamentInventorySpool, HeldTrays
from scadbuddy.print_library.reads import LibraryReads, reads_for, served_reads

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
    # #4272 — High Flow on one side and Standard on the other: refused.
    "mixed-flows",
    "no-process",
    "not-installed",
    "plate-differs",
    "plate-not-listed",
    # No longer given since the slice states its flow (#484), but kept so a run recorded
    # with one still reads back.
    "hf-unsupported",
    "hf-mounted",
    "rack-unsafe-material",
    # The slug is historical: over the LAN its message says "the printer" (#4586), and
    # it is kept for clients that key on it (#4600 review).
    "rack-left-to-bambuddy",
    "rack-manual-partial",
    # A by-object print whose objects the toolhead or the gantry would hit (#956).
    "sequence-clearance",
    # #717: a filament profile left on that could not apply; the print goes ahead.
    "profile-not-applied",
    # #1899: a support setting left out because the preset's shape for it is unknown:
    # its values could not be read, or it has no value (or an empty one) for that key.
    "supports-not-applied",
    # #3628: a prime tower setting the process preset has no value for, sent anyway.
    "prime-tower-unconfirmed",
    # #2998: a process override the process preset has no value for, or a list over
    # one value: sent as set, and the slicer judges it.
    "process-override-unchecked",
    # #2998: a setting that acts on the machine that slices (`post_process`, the print
    # host's), dropped from the print (`process_overrides.HOST_SIDE_KEYS`).
    "host-setting-dropped",
    # #3628: a prime tower chosen for an already-sliced file, which queues as it is;
    # #3962: a tower asked for that Bambuddy's slicer dropped from the slice.
    "prime-tower-not-applied",
    # A sliced file queued as it is (#2671), never refused: what to set right first.
    "sliced-other-model",
    "sliced-nozzle-missing",
    "sliced-plate-missing",
    # A sliced file whose settings could not be read, so nothing was checked (#3436).
    "sliced-unchecked",
    # Sliced for another model of the same G-code family, which prints it (#2956).
    "sliced-same-family",
    # ScadBuddy's own earlier slice of the same choices queued, nothing sliced (#2955).
    "slice-reused",
    # #2722 — the chosen spools mix material families.
    "mixed-materials",
    # #4272 — a Track Switch inlet feeds both extruders, by a side chosen by hand.
    "inlet-shared",
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
    #: Normalised to ``#RRGGBB``. An SB spool's ``rgba`` spells it ``#RRGGBBAA``,
    #: ``available-filaments`` spells the same colour with the ``#`` too, and a plate's
    #: requirement spells it ``#RRGGBB`` — so everything is normalised on the way in.
    colour: str | None = None
    slicer_filament: str | None = None
    slicer_filament_name: str | None = None
    #: The SB spool's grams left (its ledger), or Bambuddy's own reconciled figure for a
    #: spool loaded in the chosen printer, which wins where both exist.
    remaining_g: float | None = None
    storage_location: str | None = None
    loaded: LoadedAt | None = None
    #: The physical extruder this spool feeds on the chosen printer — 0 right, 1 left —
    #: and the letter for it (#469). ``None`` when the spool is not loaded there or the
    #: printer does not say. A label only: the run checks no spool against a nozzle (#768).
    extruder: int | None = None
    side: Literal["L", "R"] | None = None
    #: The spool's label weight in grams, for "about half full" (#2164); None unknown.
    label_weight_g: int | None = None
    #: Not a spool: what is in a filled tray Bambuddy has no spool for (#2164,
    #: ``trays.tray_option``), under a negative id, with no weight tracked. On a native
    #: run every choice is one (``trays.loaded_trays``, #3818).
    tray_only: bool | None = None
    #: The tray, as a person reads it ("AMS-A slot 1", "HT-A", "External"), on a native
    #: run's tray choices only.
    label: str | None = None
    #: The tray's colour as a word ("blue", `trays.colour_word`), for "PLA blue in AMS-A
    #: slot 1"; on a native run's tray choices only, as ``UnknownTray.colour_word``.
    colour_word: str | None = None
    #: What the AMS says is left, in percent: an RFID spool's ``remain``; ``None`` for an
    #: untagged one, which reports -1.
    remain_percent: int | None = None
    #: ScadBuddy's own spool (#6262, `filament/inventory.py`): on a native run's tray
    #: choice the SB spool the tray holds, on a shelf row or a Bambuddy-path row (Task 7,
    #: whose ``spool_id`` is this number) the spool itself; ``None`` when none is known.
    #: ``sb_number`` is its ``#N``, what its presets are read by (F1).
    sb_spool_id: UUID | None = None
    sb_number: int | None = None
    #: The colour with its alpha, ``#RRGGBBAA``: the held SB spool's own, else the tray's
    #: reported ``tray_color``. ``colour`` stays ``#RRGGBB``, which the render splits by;
    #: this is what tells a translucent spool from an opaque one (`same_opacity`).
    rgba: str | None = None
    #: The SB spool has no type yet (#6261): its values are its own until it gets one.
    #: ``None`` where no SB spool is known; optional, as ``tray_only`` is.
    needs_type: bool | None = None


class UnknownTray(BaseModel):
    """A filled tray with no spool assigned, and the spools it may hold."""

    ams_id: int
    tray_id: int
    #: "AMS-D slot 4".
    label: str
    material: str
    colour: str | None = None
    #: "black", for "black PLA".
    colour_word: str | None = None
    #: What the tray is now, so a "no" is remembered only until it changes.
    fingerprint: str
    #: The :func:`tray_spool_id` it is offered under.
    spool_id: int
    #: Spools of the same material with a close colour that are not loaded anywhere,
    #: most filament first and empty ones last.
    candidates: list[int] = Field(default_factory=list)


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
    than merely preferring one. It is on by default (#6218): with it off, the scheduler
    can print a part in the wrong colour when the right one is not loaded; with it on,
    Bambuddy holds the queue item until a tray of the right colour is loaded. A caller
    may still send ``False``.

    ``ids`` says what a positive ``spool_id`` names (#6262 F14): ``"sb"`` an SB spool's
    number, what every picker offers since Task 7 and every client sends; ``"bb"`` (the
    default, so a plan from before it reads as it was meant: an in-flight run's input, an
    older client) a Bambuddy spool id, mapped to its linked SB spool's number where it is
    read (:func:`sb_plan`). A negative id is a tray chosen for itself either way.
    """

    slots: list[SlotChoice] = Field(default_factory=list)
    force_colour_match: bool = True
    ids: Literal["bb", "sb"] = Field(
        default="bb",
        description=(
            "What a positive `spool_id` names: `sb` an SB spool's number (what "
            "`GET /print/filaments` and the choices offer), `bb` a Bambuddy spool id "
            "(the default, for a plan from before this field). Send `sb` with the "
            "numbers you read; an omitted `ids` is read as Bambuddy ids."
        ),
    )

    def spool_for(self, slot_id: int) -> int | None:
        return next((slot.spool_id for slot in self.slots if slot.slot_id == slot_id), None)


class FilamentOptions(BaseModel):
    """Everything the filament step of the dialog needs, in one answer.

    One route rather than three, because the join — inventory against assignments
    against the per-slot remaining weights — is the part that is easy to get wrong, and
    doing it in the browser would mean shipping every spool's history to do it.
    """

    #: The library file the slots were read from; ``None`` for an output read from its
    #: own 3MF, with no copy in Bambuddy yet (#2720).
    library_file_id: int | None
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
    #: The chosen printer's filled trays with no spool assigned (#2164): the dialog
    #: asks which spool each holds. On the Bambuddy path each is also offered in
    #: ``spools`` as itself, and its ``candidates`` are inventory spools there; on a
    #: native run (#4504) every tray is in ``spools`` already, and the candidates are
    #: rows of ``shelf``.
    trays: list[UnknownTray] = Field(default_factory=list)
    #: A native run's SB spools no tray holds (#4504, #6262, `trays.shelf_options`): what
    #: its ``trays`` questions' ``candidates`` name. Never a print choice, so never in
    #: ``spools``; empty on the Bambuddy path. Each row's ``spool_id`` is the SB spool's
    #: number (``sb_number``); a client links a row by its ``sb_spool_id``
    #: (``PUT /printers/{id}/trays/{ams}/{tray}/spool``).
    shelf: list[SpoolOption] = Field(default_factory=list)
    #: Where ``spools`` came from: Bambuddy's inventory, or (a native run, #3818) the
    #: printer's loaded trays with the SB spool each holds (#6262).
    source: Literal["inventory", "trays"] = "inventory"


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
    The output is always ``#RRGGBB``, the only form ``slicing/models.py`` ``COLOUR``
    lets reach the native slicer.
    """
    if not raw:
        return None
    text = raw.strip().lstrip("#")
    # Hex digits only: int(..., 16) also takes "-12345" and "12_456", which the native
    # slicer's colour pattern then refuses mid-slice (#3962).
    if re.fullmatch(r"[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", text) is None:
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


def colours_match(
    slot_colour: str | None, spool_colour: str | None, *, spool_rgba: str | None = None
) -> bool:
    """Whether a spool's colour is close enough to a slot's for a remembered choice of
    it to be kept: within ``COLOUR_MATCH_DISTANCE``, the cut-off the auto-match uses,
    or with no colour on either side to tell them apart (#933). Never when one is
    translucent and the other opaque (`same_opacity` on the slot's colour and the
    spool's ``spool_rgba``, plan #6262 Decision 12): a slot's colour is opaque unless it
    carries an alpha byte."""
    if not same_opacity(slot_colour, spool_rgba):
        return False
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
    return FilamentPlan(slots=[chosen[slot_id] for slot_id in sorted(chosen)], ids="sb")


def build_options(
    *,
    library_file_id: int | None,
    rows: Sequence[FilamentInventorySpool],
    requirements: list[SlotNeed],
    printer: Printer | None = None,
    slot_materials: list[SlotMaterial] | None = None,
    nozzle: str = "0.4",
) -> FilamentOptions:
    """Join everything already fetched into what the dialog renders: each unarchived SB
    spool of ``rows`` (`FilamentInventory.rows`, #6262 Task 7) under its number, filled as
    a native tray's spool is (`trays._from_spool`: its effective values, ``rgba`` and
    ``needs_type``), at the tray it is in, with the preset it names for the H2C and the
    ``nozzle`` size (`presets.preset_for`, review R2), as the resolver reads it.

    Kept free of the client so the join — the part with the traps in it — is testable
    against recorded bodies without any HTTP at all.
    """
    # Here, not at the top: `trays` builds its choices from this module's models, and
    # `filament.presets` reads the resolver's `PRINTER_MODEL`.
    from scadbuddy.bambuddy.resolver import PRINTER_MODEL
    from scadbuddy.bambuddy.trays import _from_spool
    from scadbuddy.filament.presets import preset_for

    by_slot = {(material.ams_id, material.tray_id): material for material in (slot_materials or [])}
    options: list[SpoolOption] = []
    for row in rows:
        spool, loaded = row.spool, row.loaded
        if spool.archived_at is not None:
            continue
        # ``inventory-remain`` was read for ``printer`` while a spool's tray may be any
        # printer's, so a spool sitting in printer B's AMS 0 slot 1 must take none of
        # printer A's AMS 0 slot 1 state — it is a different spool.
        here = loaded is not None and (printer is None or loaded.bambuddy_printer_id == printer.id)
        slot_material = by_slot.get((loaded.ams_id, loaded.tray_id)) if here and loaded else None
        preset = preset_for(row.presets, PRINTER_MODEL, nozzle)
        option = SpoolOption(
            spool_id=spool.number,
            material=spool.names.material or "",
            subtype=spool.names.subtype,
            colour=normalise_colour(spool.effective.rgba),
            slicer_filament=preset.slicer_filament if preset else None,
            slicer_filament_name=preset.slicer_filament_name if preset else None,
            storage_location=spool.names.location,
            loaded=(
                LoadedAt(
                    # 0 for a printer Bambuddy does not have, reached over the LAN alone,
                    # as its report's status reads (`printers.status.status_from_report`).
                    printer_id=loaded.bambuddy_printer_id or 0,
                    printer_name=loaded.printer_name,
                    ams_id=loaded.ams_id,
                    tray_id=loaded.tray_id,
                )
                if loaded is not None
                else None
            ),
        ).model_copy(update=_from_spool(spool))
        # Bambuddy's reconciliation of the tray beats the ledger for a spool loaded here.
        if slot_material is not None and slot_material.remaining_g is not None:
            option.remaining_g = slot_material.remaining_g
        options.append(option)

    options.sort(key=_display_order(printer.id if printer else None))
    return _options(
        library_file_id=library_file_id,
        options=options,
        requirements=requirements,
        printer_id=printer.id if printer else None,
        printer_name=printer.name if printer else None,
    )


def name_presets(
    options: FilamentOptions, rows: Sequence[FilamentInventorySpool], nozzle: str
) -> FilamentOptions:
    """``options`` with each SB spool's preset named for the ``nozzle`` size, as
    `build_options` names it: for the filament step, once the printer's mounted nozzle
    is read (#6262 Task 7 re-review M-c)."""
    # Here, not at the top: `filament.presets` reads the resolver's `PRINTER_MODEL`.
    from scadbuddy.bambuddy.resolver import PRINTER_MODEL
    from scadbuddy.filament.presets import preset_for

    presets = {row.spool.number: row.presets for row in rows}
    for option in options.spools:
        if option.sb_number is None or option.sb_number not in presets:
            continue
        preset = preset_for(presets[option.sb_number], PRINTER_MODEL, nozzle)
        option.slicer_filament = preset.slicer_filament if preset else None
        option.slicer_filament_name = preset.slicer_filament_name if preset else None
    return options


def _options(
    *,
    library_file_id: int | None,
    options: list[SpoolOption],
    requirements: list[SlotNeed],
    printer_id: int | None,
    printer_name: str | None,
    source: Literal["inventory", "trays"] = "inventory",
    trays: list[UnknownTray] | None = None,
    shelf: list[SpoolOption] | None = None,
) -> FilamentOptions:
    """The dialog's answer over ``options``, already in display order: each slot's
    colour matches, the opening selection and its warnings. ``trays`` and ``shelf`` are
    a native run's questions and the rows they name (`trays.tray_questions`)."""
    suggested = suggest(requirements, options, printer_id=printer_id)
    built = FilamentOptions(
        library_file_id=library_file_id,
        printer_id=printer_id,
        printer_name=printer_name,
        source=source,
        trays=trays or [],
        shelf=shelf or [],
        slots=[
            slot.model_copy(
                update={
                    "colour_matches": [
                        option.spool_id
                        for option in options
                        if colours_match(slot.colour, option.colour, spool_rgba=option.rgba)
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


_SPLIT = re.compile(r"[-\s_]+")


def family(material: str | None) -> str:
    """What can share a hotend (#2166): the material's first word (PLA, PETG, ABS,
    TPU ...), so ``PLA Basic``, ``PLA-Matte`` and ``PLA-CF`` are one family."""
    words = [word for word in _SPLIT.split((material or "").upper()) if word]
    return words[0] if words else ""


def suggest(
    slots: list[SlotNeed], spools: list[SpoolOption], *, printer_id: int | None = None
) -> list[SlotChoice]:
    """Pre-select a spool per slot, by material then colour.

    This only fills the picker in so the common case needs no clicks; it decides
    nothing. A spool already taken by an earlier slot is not offered again, because a
    two-colour plate opening with the same spool in both slots reads as a bug, except
    as below for a slot that declares no material.

    A slot that declares no material (every generated template's) stays in the plate's
    material family (#2461): the declared ones, else the family that matches the most
    slots with a loaded spool, then the most slots at all, then the best match (#2850);
    :func:`_anchor_family` has the full rule, including that it counts slots, not spools.
    One PETG loaded beside the PLA does not make a mostly-PLA plate a PETG one because
    it is the closest colour for a single slot. Opening a PLA print with an unloaded
    TPU in one slot looks deliberate, for the reason :func:`_match_score` gives for PETG
    in a PLA slot. Such a slot with no free spool of that family shares a loaded one
    already chosen whose colour fits, and is otherwise left for the user.
    """
    scored = [
        (score, index, option)
        for index, slot in enumerate(slots)
        for option in spools
        if (score := _match_score(slot, option, printer_id=printer_id)) is not None
    ]
    declared = {family(slot.material) for slot in slots if slot.material}
    families = declared or _anchor_family(scored, printer_id)
    taken: set[int] = set()
    chosen: list[SlotChoice] = []
    for slot in slots:
        best: tuple[float, int] | None = None
        shared: tuple[float, int] | None = None
        for option in spools:
            score = _match_score(slot, option, printer_id=printer_id)
            if score is None:
                continue
            if not slot.material and family(option.material) not in families:
                continue
            if option.spool_id not in taken:
                if best is None or score < best[0]:
                    best = (score, option.spool_id)
            elif (
                not slot.material
                and _loaded_here(option, printer_id)
                and (shared is None or score < shared[0])
            ):
                shared = (score, option.spool_id)
        if best is not None:
            taken.add(best[1])
            chosen.append(SlotChoice(slot_id=slot.slot_id, spool_id=best[1]))
        elif shared is not None:
            chosen.append(SlotChoice(slot_id=slot.slot_id, spool_id=shared[1]))
    return chosen


def _anchor_family(
    scored: list[tuple[float, int, SpoolOption]], printer_id: int | None
) -> set[str]:
    """The family an all-undeclared plate is suggested in (#2850): the one whose
    spools match the most slots with a loaded spool, then the most slots at all, then
    the best single match.

    It counts slots, not spools: one loaded spool whose colour fits three slots beats
    two loaded spools that fit two, since an undeclared slot can share a loaded spool.
    A spool with no material is the family ``""``; if that wins, only material-less
    spools are suggested for the undeclared slots."""
    loaded: dict[str, set[int]] = {}
    matched: dict[str, set[int]] = {}
    best: dict[str, tuple[float, int]] = {}
    for score, index, option in scored:
        kind = family(option.material)
        matched.setdefault(kind, set()).add(index)
        loaded.setdefault(kind, set())
        if _loaded_here(option, printer_id):
            loaded[kind].add(index)
        best[kind] = min(best.get(kind, (score, index)), (score, index))
    if not matched:
        return set()
    return {
        min(
            matched,
            key=lambda kind: (-len(loaded[kind]), -len(matched[kind]), best[kind]),
        )
    }


def _loaded_here(option: SpoolOption, printer_id: int | None) -> bool:
    return (
        option.loaded is not None
        and printer_id is not None
        and option.loaded.printer_id == printer_id
    )


def _match_score(slot: SlotNeed, option: SpoolOption, *, printer_id: int | None) -> float | None:
    """Lower is better; ``None`` means this spool is not offered for this slot.

    A slot that declares a material only ever pre-selects that material — opening with
    a PETG in a PLA slot would be a worse failure than opening with nothing, because it
    looks deliberate.
    """
    if slot.material and option.material.upper() != slot.material.upper():
        return None
    if not same_opacity(slot.colour, option.rgba):
        # A translucent spool for an opaque slot, or the reverse, is never a colour
        # match (spec §5.1, plan #6262 Decision 12), however close the RGB.
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
    loaded_here = _loaded_here(option, printer_id)
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
    # What every slot on one spool needs together (#2851): a spool shared by two slots,
    # which `suggest` can pick as well as a hand pick, has to cover both.
    sharing: dict[int, list[SlotNeed]] = {}
    for slot in options.slots:
        spool_id = plan.spool_for(slot.slot_id)
        if spool_id is not None and slot.used_grams is not None:
            sharing.setdefault(spool_id, []).append(slot)

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
        shared = tuple(other for other in sharing.get(option.spool_id, []) if other is not slot)
        warnings.extend(_slot_warnings(options, slot, option, copies=copies, shared=shared))

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
    options: FilamentOptions,
    slot: SlotNeed,
    option: SpoolOption,
    *,
    copies: int,
    shared: tuple[SlotNeed, ...] = (),
) -> list[FilamentWarning]:
    """``shared``: the other slots with known grams printing from ``option``."""
    found: list[FilamentWarning] = []
    label = spool_label(option)

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
    elif needed is not None and option.remaining_g is not None and shared:
        together = needed + sum((other.used_grams or 0.0) * copies for other in shared)
        if option.remaining_g < together:
            ids = sorted([slot.slot_id, *(other.slot_id for other in shared)])
            names = ", ".join(str(i) for i in ids[:-1]) + f" and {ids[-1]}"
            found.append(
                FilamentWarning(
                    kind="low-filament",
                    slot_id=slot.slot_id,
                    message=(
                        f"{label} has about {option.remaining_g:.0f} g left and slots "
                        f"{names} need {together:.0f} g of it between them."
                    ),
                )
            )

    return found


def spool_label(option: SpoolOption) -> str:
    """``Bambu Lab PLA Basic Black``: brand, material, subtype and colour name."""
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
    library_file_id: int | None,
    printer_id: int | None = None,
    plate_id: int | None = None,
    fallback_colours: list[str] | None = None,
    own_colours: list[str] | None = None,
    reads: SbRows | None = None,
) -> FilamentOptions:
    """Read Bambuddy once for everything the filament step needs, and ``reads`` for the
    SB spools it offers (#6262 Task 7).

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
        reads=reads,
    )
    return options


#: The SB spools and where each is loaded, read once (#2166 reuses one read; #6262 Task 7).
Inventory = list[FilamentInventorySpool]


class SbRows(Protocol):
    """What the Bambuddy path reads of SB spools (`filament/inventory.py` `SbSpoolReads`,
    the API's Postgres or the print worker's CRUD routes)."""

    async def rows(
        self, bb_assignments: Sequence[SpoolAssignment]
    ) -> list[FilamentInventorySpool]: ...

    async def bb_to_sb(self, ids: Sequence[int]) -> dict[int, int]: ...


async def read_inventory(client: BambuddyClient, reads: SbRows | None) -> Inventory:
    """The Bambuddy path's picker rows (#6262 Task 7): ScadBuddy's own spools, each in the
    tray a printer's report holds it in, else the one Bambuddy assigns its linked spool to
    (``/inventory/assignments``, read here). Without ``reads`` there are no SB spools to
    offer, so there are no rows. A store that cannot be read (the API's database, or the
    CRUD service from the print worker) is a 503, as a failed Bambuddy read is (#6262
    Task 7 review M6)."""
    assignments = await client.spool_assignments()
    if reads is None:
        return []
    return await _sb_read(reads.rows(assignments))


async def _sb_read[T](read: Awaitable[T]) -> T:
    """``read`` of ScadBuddy's spools; a store that cannot be read is a 503."""
    unread = ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "ScadBuddy's spools could not be read; try again shortly.",
    )
    try:
        return await read
    except DATABASE_ERRORS as error:
        raise unread from error
    except CrudUnavailableError as error:
        raise unread from error


async def sb_plan(plan: FilamentPlan, reads: SbRows | None) -> FilamentPlan:
    """``plan`` naming SB spool numbers (#6262 F14): as it is when its ``ids`` are
    ``"sb"``; else each positive id, a Bambuddy spool's, mapped to its linked SB spool's
    number (`FilamentInventory.bb_to_sb`), and a slot whose spool has no link dropped,
    never read as SB spool #N. A tray chosen for itself (a negative id) is kept. A
    mapping that cannot be read is a 503, as in `read_inventory` (re-review M-e)."""
    if plan.ids == "sb":
        return plan
    named = [slot.spool_id for slot in plan.slots if slot.spool_id > 0]
    mapped = await _sb_read(reads.bb_to_sb(named)) if reads is not None and named else {}
    return plan.model_copy(
        update={
            "ids": "sb",
            "slots": [
                slot
                if slot.spool_id <= 0
                else SlotChoice(slot_id=slot.slot_id, spool_id=mapped[slot.spool_id])
                for slot in plan.slots
                if slot.spool_id <= 0 or slot.spool_id in mapped
            ],
        }
    )


#: The most plates one request may name (#3652); a 3MF lays out far fewer.
MAX_PLATES = 256


def asked_plates(
    plate_id: int, all_plates: bool, plate_ids: Sequence[int] | None
) -> list[int] | None:
    """The plates a request asks for, each once and in plate order; ``None`` for every
    plate (#3652). ``all_plates`` wins over ``plate_ids``, which wins over ``plate_id``."""
    if all_plates:
        return None
    if plate_ids:
        return sorted(set(plate_ids))
    return [plate_id]


def one_plate_folded(
    plate_id: int, all_plates: bool, plate_ids: Sequence[int] | None
) -> tuple[int, list[int] | None]:
    """``(plate_id, plate_ids)`` with ``plate_ids`` each once in plate order, for a run
    and an analysis alike (#3762 review). One chosen plate is ``plate_id``'s print
    (``plate_ids=[2]`` is ``plate_id=2``), and several make ``plate_id`` the first of
    them, so the mesh checks read a plate that prints. ``all_plates`` keeps ``plate_id``."""
    if plate_ids is None:
        return plate_id, None
    chosen = sorted(set(plate_ids))
    if all_plates:
        return plate_id, chosen
    return (chosen[0], None) if len(chosen) == 1 else (chosen[0], chosen)


class _Plated(Protocol):
    plate_id: int
    all_plates: bool
    plate_ids: list[int] | None
    __pydantic_fields_set__: set[str]


def fold_plates(request: _Plated) -> None:
    """Apply :func:`one_plate_folded` to a request model in place, keeping the fields it
    was sent as its set fields (#3768 review): ``exclude_unset`` dumps (a batch's
    stored request and its ``batch_request_id``) read the same before and after the
    fold. Only a one-plate list, which leaves ``plate_ids`` empty, marks ``plate_id``
    set, so a dump re-read names the plate that prints."""
    # Called from an `after` validator: safe only while neither model sets
    # `validate_assignment`, which would run the validator again on each assignment.
    folded = one_plate_folded(request.plate_id, request.all_plates, request.plate_ids)
    if folded == (request.plate_id, request.plate_ids):
        return
    sent = set(request.__pydantic_fields_set__)
    request.plate_id, request.plate_ids = folded
    if folded[1] is None and "plate_ids" in sent:
        sent.add("plate_id")
    request.__pydantic_fields_set__ = sent


def plates_named(plates: Sequence[int]) -> str:
    """``plate 4``, or ``plates 4 and 5``, or ``plates 4, 5 and 6``."""
    if len(plates) == 1:
        return f"plate {plates[0]}"
    return f"plates {', '.join(map(str, plates[:-1]))} and {plates[-1]}"


async def gather_plate_options(
    client: BambuddyClient | None,
    *,
    library_file_id: int | None,
    printer_id: int | None = None,
    plate_ids: Sequence[int | None],
    fallback_colours: list[str] | None = None,
    own_colours: list[str] | None = None,
    inventory: Inventory | None = None,
    known: Callable[[int | None], FilamentRequirements] | None = None,
    native: LibraryReads | None = None,
    trays_from: PrinterStatus | None = None,
    held: HeldTrays | None = None,
    reads: SbRows | None = None,
    nozzle: str = "0.4",
) -> list[FilamentOptions]:
    """:func:`gather_options` for several plates of one file, in ``plate_ids`` order.
    Each spool names its preset for the ``nozzle`` size (`build_options`).

    The spools and printer are the same for every plate, so they are read once (#480);
    only each plate's slots are read per plate, concurrently. The reads keep the
    single-plate order, the spools (`read_inventory`: Bambuddy's assignments, then the SB
    spools from ``reads``), the plates, then the printer and its inventory-remain, so the
    same failure surfaces either way: a plate's error beats the printer's, and among
    plates the first failing one in ``plate_ids`` order wins. ``inventory`` is the
    spools when the caller has read them already.
    ``known`` answers each plate's slots in place of Bambuddy's ``filament-requirements``
    when they were read from the file itself (#2550). A native file's (#3827) are read
    from ``native``: the empty requirement, so its slots read as unknown.

    ``trays_from`` (a native run, #3818 tier A, plan 2026-10-18 D1) replaces the inventory,
    the printer and its inventory-remain with the printer's loaded trays from its LAN
    report, with ``held`` (#6262: the inventory's `TrayView`, or the background slice's
    `SbSpools`, no shelf) naming the SB spool in each and the shelf it has; no client is
    needed then, and none is read. Without ``held`` the trays are shown alone."""
    if trays_from is not None:
        return await _tray_plate_options(
            client,
            library_file_id=library_file_id,
            printer_id=printer_id,
            plate_ids=plate_ids,
            fallback_colours=fallback_colours,
            own_colours=own_colours,
            known=known,
            native=native,
            status=trays_from,
            held=held,
        )
    if client is None:
        raise TypeError("the Bambuddy path's options need a client")
    rows = inventory if inventory is not None else await read_inventory(client, reads)

    answers = await asyncio.gather(
        *(
            _requirements(
                client, library_file_id, plate, fallback_colours, own_colours, known, native
            )
            for plate in plate_ids
        ),
        *(
            (client.printer(printer_id), client.inventory_remain(printer_id))
            if printer_id is not None
            else ()
        ),
        return_exceptions=True,
    )
    per_plate: list[list[SlotNeed]] = [settled(answer) for answer in answers[: len(plate_ids)]]

    printer = None
    slot_materials: list[SlotMaterial] = []
    if printer_id is not None:
        # Read beside the plates (#2354), and still failing after them.
        printer = settled(answers[len(plate_ids)])
        slot_materials = settled(answers[len(plate_ids) + 1]).slot_materials

    return [
        build_options(
            library_file_id=library_file_id,
            rows=rows,
            requirements=requirements,
            printer=printer,
            slot_materials=slot_materials,
            nozzle=nozzle,
        )
        for requirements in per_plate
    ]


async def _tray_plate_options(
    client: BambuddyClient | None,
    *,
    library_file_id: int | None,
    printer_id: int | None,
    plate_ids: Sequence[int | None],
    fallback_colours: list[str] | None,
    own_colours: list[str] | None,
    known: Callable[[int | None], FilamentRequirements] | None,
    native: LibraryReads | None,
    status: PrinterStatus,
    held: HeldTrays | None,
) -> list[FilamentOptions]:
    # Here, not at the top: `trays` builds its choices from this module's models.
    from scadbuddy.bambuddy.trays import loaded_trays, tray_questions

    answers = await asyncio.gather(
        *(
            _requirements(
                client, library_file_id, plate, fallback_colours, own_colours, known, native
            )
            for plate in plate_ids
        ),
        return_exceptions=True,
    )
    per_plate: list[list[SlotNeed]] = [settled(answer) for answer in answers]
    # The report's own id: the Bambuddy printer's when the row links one, else 0
    # (`printers.status.status_from_report`); every tray is loaded "here".
    here = printer_id if printer_id is not None else status.id
    options = loaded_trays(status, here, status.name, held)
    # Shelf ids are SB spool numbers, the same on every plate of one answer.
    questions, shelf = tray_questions(status, held)
    return [
        _options(
            library_file_id=library_file_id,
            options=[option.model_copy() for option in options],
            requirements=requirements,
            printer_id=here,
            printer_name=status.name,
            source="trays",
            trays=[question.model_copy(deep=True) for question in questions],
            shelf=[spool.model_copy(deep=True) for spool in shelf],
        )
        for requirements in per_plate
    ]


async def _requirements(
    client: BambuddyClient | None,
    library_file_id: int | None,
    plate_id: int | None,
    fallback_colours: list[str] | None,
    own_colours: list[str] | None = None,
    known: Callable[[int | None], FilamentRequirements] | None = None,
    native: LibraryReads | None = None,
) -> list[SlotNeed]:
    """The plate's slots, from Bambuddy, falling back to the output's own colours.

    Bambuddy reads them out of the 3MF's ``Metadata/slice_info.config``, which a plate
    ScadBuddy generated does carry. If it ever does not, the output's colour list is
    the same information in the same order, so the picker still opens.
    """
    if known is not None:
        answer = known(plate_id)
    else:
        if library_file_id is None:
            # Only a source that answered ReadFile(None) without reading its own slots
            # reaches here; say so rather than fail on an assert.
            raise ApiError(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"plate {plate_id}'s slots were neither read from the file nor named by a"
                " library file",
            )
        reads = (
            served_reads(library_file_id, None, native)
            if client is None
            else reads_for(library_file_id, client, native)
        )
        answer = await reads.filament_requirements(library_file_id, plate_id=plate_id)
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
