"""Which nozzle each filament prints from, planned by ScadBuddy (#2166).

The person chooses only High Flow or Standard; ScadBuddy decides the rest. Before this
the slicer's "Auto For Flush" grouping chose the sides from whatever the 3MF's
``extruder_nozzle_stats`` offered, and a library file made for one extruder put every
colour on the left (queue item 268, #2181).

The plan, for the sides that have a nozzle of the chosen size
(:func:`~scadbuddy.bambuddy.extruders._offered_sides`):

* A filament chosen for a side by hand (Advanced) prints there.
* Without the Filament Track Switch an AMS is wired to one extruder, so a loaded spool
  prints on the side it feeds. With the switch any AMS reaches either nozzle, and the
  side a spool rests on only breaks ties.
* The rest are grouped by what can share a hotend: one material family (PLA, PETG, ABS,
  TPU …), which is also one temperature window. Two or more families go to the side
  with fewer filaments so far, a family at a time, so no hotend runs two families. One
  family is spread a filament at a time, so each colour has a nozzle of its own where
  there are enough: fewer purges and tool changes.

Abrasive filament (a ``CF``/``GF`` type) needs a hardened nozzle, but no mounted
nozzle's material is known (``rack.rank.NOZZLE_MATERIALS`` ships empty), so the plan
cannot prefer one; the rack pick still warns of an unhardened one (``rack-unsafe-material``).

What the 3MF carries (:meth:`NozzlePlan.settings`), checked against Bambu Studio's own
CLI source (``src/BambuStudio.cpp``, the 02.08 tree the deployed
``bambu-studio-api`` sidecar builds; read 2026-10-06 for #484 and again for #2166):

* ``filament_map_mode`` is read from the plate's or the project's config when the
  command line passes none (``part_plate->get_real_filament_map_mode(m_print_config)``,
  around line 6655), and ``filament_map`` the same way (``get_real_filament_maps``,
  around line 6745). Under ``"Manual"`` the map is used as given: each filament is
  checked against what its extruder can print (``CLI_FILAMENT_CAN_NOT_MAP`` and
  ``CLI_FILAMENTS_NOT_SUPPORTED_BY_EXTRUDER``, around 6842-6870) and no grouping
  runs. ``filament_map`` is 1-based in the slicer's extruder order: 1 is the left on
  the H2C (``physical_extruder_map`` ["1", "0"], spec 2026-09-27 §4.3).
* ``filament_volume_map`` (each filament's flow, ``NozzleVolumeType``: 0 Standard,
  1 High Flow, ``PrintConfig.cpp`` ``s_keys_map_NozzleVolumeType``) is built from the
  map only for a single plate (``plate_to_slice != 0``, around line 6783); otherwise
  the CLI pads it with the left extruder's flow (around line 6960). ScadBuddy slices a
  one-plate library file as plate 0 (#2180), so the plan writes it too.
* ``extruder_nozzle_stats`` offer exactly the sides the plan uses. On 2026-09-28 (#745)
  the deployed slicer was measured not to follow a Manual map while the stats offered
  both sides; if it still does not, its Auto For Flush grouping is held to the same
  sides by the stats, and with both offered it spreads the colours as the plan does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.extruders import (
    LEFT,
    RIGHT,
    SLICER_ORDER,
    VOLUME_TYPE,
    Side,
    _flows,
    _offered_sides,
    extruder_of,
    nozzle_stats_for,
    side_of,
    track_switch,
)
from scadbuddy.bambuddy.filaments import FilamentPlan, Inventory, normalise_colour, read_inventory
from scadbuddy.bambuddy.models import (
    FlowType,
    NozzleChoice,
    PrinterStatus,
    Spool,
    SpoolAssignment,
)
from scadbuddy.bambuddy.trays import family, tray_label, tray_material_colour, tray_of_spool_id

#: ``NozzleVolumeType`` as Bambu Studio numbers it (``PrintConfig.cpp``).
VOLUME_INDEX: dict[FlowType, str] = {"standard": "0", "high_flow": "1"}
MANUAL = "Manual"
_SIDE_EXTRUDER: dict[Side, int] = {"L": LEFT, "R": RIGHT}


@dataclass(frozen=True)
class PlanInput:
    """One filament of the print: its slot, what it is, and where its spool rests."""

    slot_id: int
    material: str | None = None
    colour: str | None = None
    name: str | None = None
    #: The physical extruder the spool's AMS feeds or rests on (``extruders.extruder_of``).
    rests: int | None = None


class PlannedSlot(BaseModel):
    slot_id: int
    side: Side
    name: str
    colour: str | None = None
    material: str | None = None
    #: Chosen for this side by hand (Advanced), not planned.
    by_hand: bool = False


class NozzlePlan(BaseModel):
    """The filament→nozzle map, as the dialog shows it and the 3MF states it."""

    slots: list[PlannedSlot] = Field(default_factory=list)
    size: str
    left_flow: FlowType
    right_flow: FlowType
    track_switch: bool = False
    #: "Mistletoe Green → left · Inland Black → right · 0.4 High Flow".
    summary: str

    def side_of_slot(self, slot_id: int) -> Side | None:
        return next((slot.side for slot in self.slots if slot.slot_id == slot_id), None)

    @property
    def sides(self) -> frozenset[int]:
        return frozenset(_SIDE_EXTRUDER[slot.side] for slot in self.slots)

    def nozzle_stats(self, nozzles: Sequence[NozzleChoice]) -> list[str]:
        """``extruder_nozzle_stats`` offering the sides the plan uses."""
        return nozzle_stats_for(self.sides, nozzles)

    def filament_map(self, filaments: int) -> list[str]:
        """``filament_map`` for a file of ``filaments`` filaments, in the slicer's
        numbering (1 the left). A filament the plan has no slot for (one no plate
        uses) goes with the first planned one."""
        fallback = self.slots[0].side if self.slots else "L"
        return [
            str(SLICER_ORDER.index(_SIDE_EXTRUDER[self.side_of_slot(index) or fallback]) + 1)
            for index in range(1, filaments + 1)
        ]

    def volume_map(self, filaments: int) -> list[str]:
        """``filament_volume_map``: each filament's flow, its side's."""
        flows = {LEFT: self.left_flow, RIGHT: self.right_flow}
        order = self.filament_map(filaments)
        return [VOLUME_INDEX[flows[SLICER_ORDER[int(entry) - 1]]] for entry in order]


def _flow_words(left: FlowType, right: FlowType, size: str, sides: frozenset[int]) -> str:
    if left == right or len(sides) == 1:
        flow = left if sides == frozenset({LEFT}) else right
        return f"{size} {VOLUME_TYPE[flow]}"
    return f"{size} · left {VOLUME_TYPE[left]}, right {VOLUME_TYPE[right]}"


def plan_nozzles(
    status: PrinterStatus | None,
    nozzles: Sequence[NozzleChoice],
    filaments: Sequence[PlanInput],
    by_hand: Mapping[int, Side] | None = None,
) -> NozzlePlan | None:
    """The plan (module docstring), or ``None`` when there is nothing to plan: no
    filament, an unreadable status, a printer with one extruder, or no side with a
    nozzle of the size. The file is then left to the slicer, as before #2166."""
    allowed = _offered_sides(status, nozzles)
    if allowed is None or not filaments or not nozzles:
        return None
    switch = track_switch(status)
    hand = by_hand or {}
    placed: dict[int, int] = {}
    chosen: set[int] = set()
    for filament in filaments:
        side = hand.get(filament.slot_id)
        if side is not None and _SIDE_EXTRUDER[side] in allowed:
            placed[filament.slot_id] = _SIDE_EXTRUDER[side]
            chosen.add(filament.slot_id)
        elif len(allowed) == 1:
            placed[filament.slot_id] = next(iter(allowed))
        elif not switch and filament.rests is not None and filament.rests in allowed:
            placed[filament.slot_id] = filament.rests
    load = {side: sum(1 for value in placed.values() if value == side) for side in allowed}

    def pick(rests: list[int | None]) -> int:
        """The side with fewest filaments, then where most of these rest, then left."""
        return min(
            sorted(allowed, key=lambda side: side != LEFT),
            key=lambda side: (load[side], -rests.count(side)),
        )

    free = [filament for filament in filaments if filament.slot_id not in placed]
    groups: dict[str, list[PlanInput]] = {}
    for filament in free:
        groups.setdefault(family(filament.material), []).append(filament)
    if len(groups) > 1:
        for members in sorted(groups.values(), key=lambda group: -len(group)):
            extruder = pick([member.rests for member in members])
            for member in members:
                placed[member.slot_id] = extruder
            load[extruder] += len(members)
    else:
        for filament in free:
            extruder = pick([filament.rests])
            placed[filament.slot_id] = extruder
            load[extruder] += 1

    flows = _flows(nozzles)
    size = nozzles[0].size
    slots = [
        PlannedSlot(
            slot_id=filament.slot_id,
            side=side_of(placed[filament.slot_id]) or "L",
            name=filament.name or f"Slot {filament.slot_id}",
            colour=filament.colour,
            material=filament.material,
            by_hand=filament.slot_id in chosen,
        )
        for filament in sorted(filaments, key=lambda each: each.slot_id)
    ]
    used = frozenset(placed.values())
    words = " · ".join(f"{slot.name} → {'left' if slot.side == 'L' else 'right'}" for slot in slots)
    return NozzlePlan(
        slots=slots,
        size=size,
        left_flow=flows[LEFT],
        right_flow=flows[RIGHT],
        track_switch=switch,
        summary=f"{words} · {_flow_words(flows[LEFT], flows[RIGHT], size, used)}",
    )


_BAMBU_BRANDS = frozenset({"bambu", "bambu lab", "bambulab"})


def spool_name(spool: Spool) -> str:
    """A spool as a person names it: "Mistletoe Green" for Bambu's own, "Inland Black"
    for another brand's, else its material."""
    colour = (spool.color_name or "").strip()
    brand = (spool.brand or "").strip()
    if colour and brand and brand.lower() not in _BAMBU_BRANDS:
        return f"{brand} {colour}"
    return colour or " ".join(part for part in (brand, spool.material) if part)


def plan_inputs(
    plan: FilamentPlan,
    spools: Sequence[Spool],
    assignments: Sequence[SpoolAssignment],
    status: PrinterStatus | None,
    printer_id: int,
) -> list[PlanInput]:
    """One :class:`PlanInput` per slot of ``plan``, from the inventory and where each
    spool is loaded on ``printer_id``."""
    by_id = {spool.id: spool for spool in spools}
    loaded = {
        assignment.spool_id: assignment
        for assignment in assignments
        if assignment.printer_id == printer_id
    }
    found: list[PlanInput] = []
    for slot in sorted(plan.slots, key=lambda each: each.slot_id):
        where_tray = tray_of_spool_id(slot.spool_id)
        if where_tray is not None:
            # The tray itself, chosen for a spool Bambuddy does not know (#2164).
            found_tray = tray_material_colour(slot.spool_id, status)
            found.append(
                PlanInput(
                    slot_id=slot.slot_id,
                    material=found_tray[0] if found_tray else None,
                    colour=found_tray[1] if found_tray else None,
                    name=f"what's in {tray_label(*where_tray)}",
                    rests=extruder_of(*where_tray, status),
                )
            )
            continue
        spool = by_id.get(slot.spool_id)
        where = loaded.get(slot.spool_id)
        found.append(
            PlanInput(
                slot_id=slot.slot_id,
                material=spool.material if spool else None,
                colour=normalise_colour(spool.rgba) if spool else None,
                name=spool_name(spool) if spool else None,
                rests=extruder_of(where.ams_id, where.tray_id, status) if where else None,
            )
        )
    return found


async def plan_for(
    client: BambuddyClient,
    status: PrinterStatus | None,
    printer_id: int,
    plan: FilamentPlan,
    nozzles: Sequence[NozzleChoice],
    by_hand: Mapping[int, Side] | None = None,
    inventory: Inventory | None = None,
) -> NozzlePlan | None:
    """:func:`plan_nozzles` for a print request: the inventory and its assignments,
    read here unless ``inventory`` has them, and nothing read when there is nothing to
    plan."""
    if _offered_sides(status, nozzles) is None or not plan.slots:
        return None
    spools, assignments = inventory if inventory is not None else await read_inventory(client)
    inputs = plan_inputs(plan, spools, assignments, status, printer_id)
    return plan_nozzles(status, nozzles, inputs, by_hand)
