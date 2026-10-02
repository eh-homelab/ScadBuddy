"""Ranking the H2C's nozzle rack for a plate's rack groups (#836).

Spec ``docs/superpowers/specs/2026-10-01-rack-nozzle-selection-design.md`` §3-§5. Pure:
no I/O and no logging. A serial rides on :class:`Pick` only (spec §7), never on a
:class:`RackCandidate`, so a view built from the candidates cannot carry one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from datetime import datetime

from scadbuddy.bambuddy.extruders import RACK_SIDE
from scadbuddy.bambuddy.filaments import (
    FilamentWarning,
    SpoolOption,
    WarningKind,
    normalise_colour,
)
from scadbuddy.bambuddy.models import FilamentRequirement, NozzleRackSlot, RackAlgorithm

__all__ = ["RackAlgorithm"]

DEFAULT_ALGORITHM: RackAlgorithm = "least_used"
#: The flow every slice carries while Bambuddy has no High Flow presets (#484): the
#: preview and the manual pick's 422 judge it, since Bambuddy re-checks a pick against
#: the sliced group at dispatch.
SLICED_VOLUME_TYPE = "Standard"
#: ``nozzle_type`` code -> material, from the hotends' own labels (spec §8 unknown 1).
#: Ships EMPTY: every code then counts as not hardened, so an abrasive group always
#: carries ``rack-unsafe-material`` and never gets a silent brass pick.
NOZZLE_MATERIALS: dict[str, str] = {}
HARDENED_MATERIALS: frozenset[str] = frozenset({"hardened steel", "tungsten carbide"})
ABRASIVE_TOKENS: frozenset[str] = frozenset({"CF", "GF"})
#: Rack ids 16-21 are positions 1-6 (spec §2).
FIRST_RACK_ID = 16
POSITIONS = range(1, 7)
_SPLIT = re.compile(r"[-\s]+")

REASON_MANUAL = "chosen by hand"
REASON_ONLY = "the only eligible position"
REASON_COLOR = "already loaded with this color"
REASON_POSITION = "lowest free position"
_ALGORITHM_REASONS: dict[RackAlgorithm, str] = {
    "least_used": "least used",
    "oldest_first": "oldest hotend",
    "newest_first": "newest hotend",
    "bambuddy": REASON_POSITION,
}


@dataclass(frozen=True)
class Usage:
    """One serial's use (spec §4). A serial with no rows is all zeros, never missing."""

    prints: int = 0
    print_seconds: int = 0
    grams: float = 0.0
    first_seen_at: datetime | None = None


@dataclass(frozen=True)
class RackGroup:
    """One ``on_rack`` filament group of a sliced plate, or the preview's rack side."""

    group_id: int
    nozzle_diameter: str
    volume_type: str
    color: str | None = None
    materials: tuple[str, ...] = ()
    abrasive: bool = False
    #: A filament had no inventory spool, so Glow could not be checked (spec §3).
    glow_unchecked: bool = False
    #: How a warning names it; empty means "group <id>".
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or f"group {self.group_id}"


@dataclass(frozen=True)
class RackCandidate:
    """An eligible position, ranked. Carries no serial (spec §5)."""

    position: int
    nozzle_diameter: str
    high_flow: bool
    nozzle_type: str
    color: str | None
    material: str | None
    prints: int
    print_seconds: int
    #: (material, color, algorithm, algorithm, position): lower is better.
    key: tuple[float, ...]


@dataclass(frozen=True)
class Pick:
    """A position chosen for a group. Internal: the serial never leaves the backend."""

    group_id: int
    position: int
    serial: str = field(repr=False)
    reason: str
    unsafe_material: bool
    manual: bool
    candidates: tuple[RackCandidate, ...]


def diameter(text: str | None) -> float | None:
    """``round(float(x), 2)``; ``"0.2"`` and ``"0.20"`` are one size (spec §3)."""
    try:
        return round(float(text or ""), 2)
    except ValueError:
        return None


def _wants_high_flow(volume_type: str) -> bool:
    return volume_type.strip().lower().startswith("high flow")


def eligible(slot: NozzleRackSlot, nozzle_diameter: str, volume_type: str) -> bool:
    """Bambuddy's ``_rack_slot_is_eligible``: same diameter, and the same flow when both
    the slot's code and the group's flow name are present (spec §3)."""
    want, have = diameter(nozzle_diameter), diameter(slot.nozzle_diameter)
    if want is None or have is None or want != have:
        return False
    if len(slot.nozzle_type) < 2 or not volume_type.strip():
        return True
    return slot.high_flow == _wants_high_flow(volume_type)


def rack_positions(rack: Sequence[NozzleRackSlot]) -> dict[int, NozzleRackSlot]:
    """Position -> hotend. Ids 16-21 are positions 1-6. The firmware omits the id of the
    hotend on the carriage; when exactly one position is missing, the rack-side carriage
    hotend (id ``RACK_SIDE``) is that position, as Bambuddy's ``_rack_by_position``."""
    found = {
        entry.id - FIRST_RACK_ID + 1: entry
        for entry in rack
        if entry.id - FIRST_RACK_ID + 1 in POSITIONS
    }
    missing = [position for position in POSITIONS if position not in found]
    on_carriage = next((entry for entry in rack if entry.id == RACK_SIDE), None)
    if len(missing) == 1 and on_carriage is not None:
        found[missing[0]] = on_carriage
    return dict(sorted(found.items()))


def rack_serials(rack: Sequence[NozzleRackSlot]) -> list[str]:
    """The serials of the hotends :func:`rack_positions` finds, for usage lookups."""
    return [entry.serial_number for entry in rack_positions(rack).values()]


def nozzle_material(code: str) -> str | None:
    return NOZZLE_MATERIALS.get(code)


def hardened(code: str) -> bool:
    """A code not in the table counts as not hardened (spec §3)."""
    return nozzle_material(code) in HARDENED_MATERIALS


def abrasive_type(filament_type: str | None) -> bool:
    """A ``CF`` or ``GF`` token in the type, split on ``-`` and spaces (spec §3)."""
    return any(token.upper() in ABRASIVE_TOKENS for token in _SPLIT.split(filament_type or ""))


def glow(*texts: str | None) -> bool:
    """``glow`` in a spool's material or subtype, case-insensitively (spec §3)."""
    return any("glow" in (text or "").lower() for text in texts)


def rack_color(raw: str) -> str | None:
    """A rack color, or ``None`` for a zero alpha: the rack reports ``00000000`` for a
    hotend with no filament loaded, which is no color rather than black."""
    text = raw.strip().lstrip("#")
    if len(text) == 8 and text[6:].upper() == "00":
        return None
    return normalise_colour(raw)


def rack_groups(
    filaments: Sequence[FilamentRequirement], spools_by_slot: Mapping[int, SpoolOption]
) -> list[RackGroup]:
    """One :class:`RackGroup` per ``on_rack`` group of the plate's used filaments. A group
    is abrasive when any of its filaments is; its color is the group's own (its first
    filament's, spec §8 unknown 2)."""
    grouped: dict[int, list[FilamentRequirement]] = {}
    for filament in filaments:
        if (
            filament.used_in_plate
            and filament.group_id is not None
            and filament.group is not None
            and filament.group.on_rack
        ):
            grouped.setdefault(filament.group_id, []).append(filament)
    groups: list[RackGroup] = []
    for group_id, members in sorted(grouped.items()):
        shape = members[0].group
        if shape is None:  # narrowed above; keeps mypy honest without an assert
            continue
        spools = [spools_by_slot.get(member.slot_id) for member in members]
        groups.append(
            RackGroup(
                group_id=group_id,
                nozzle_diameter=shape.nozzle_diameter,
                volume_type=shape.volume_type,
                color=shape.filament_color or None,
                materials=tuple(dict.fromkeys(m.type for m in members if m.type)),
                abrasive=any(abrasive_type(member.type) for member in members)
                or any(
                    spool is not None and glow(spool.material, spool.subtype) for spool in spools
                ),
                glow_unchecked=any(spool is None for spool in spools),
            )
        )
    return groups


def _available(
    group: RackGroup, positions: Mapping[int, NozzleRackSlot], taken: Set[int]
) -> dict[int, NozzleRackSlot]:
    return {
        position: entry
        for position, entry in positions.items()
        if position not in taken and eligible(entry, group.nozzle_diameter, group.volume_type)
    }


def _algorithm_key(algorithm: RackAlgorithm, use: Usage) -> tuple[float, float]:
    if algorithm == "least_used":
        return float(use.print_seconds), float(use.prints)
    seen = use.first_seen_at
    if algorithm == "oldest_first":
        return (1.0, 0.0) if seen is None else (0.0, seen.timestamp())
    if algorithm == "newest_first":
        return (0.0, 0.0) if seen is None else (1.0, -seen.timestamp())
    return 0.0, 0.0


def _rank(
    group: RackGroup,
    positions: Mapping[int, NozzleRackSlot],
    algorithm: RackAlgorithm,
    usage: Mapping[str, Usage],
    taken: Set[int],
) -> tuple[RackCandidate, ...]:
    want = normalise_colour(group.color)
    found: list[RackCandidate] = []
    for position, entry in _available(group, positions, taken).items():
        use = usage.get(entry.serial_number, Usage()) if entry.serial_number else Usage()
        have = rack_color(entry.filament_colour)
        found.append(
            RackCandidate(
                position=position,
                nozzle_diameter=entry.nozzle_diameter,
                high_flow=entry.high_flow,
                nozzle_type=entry.nozzle_type,
                color=have,
                material=nozzle_material(entry.nozzle_type),
                prints=use.prints,
                print_seconds=use.print_seconds,
                key=(
                    0.0 if hardened(entry.nozzle_type) == group.abrasive else 1.0,
                    0.0 if want is not None and want == have else 1.0,
                    *_algorithm_key(algorithm, use),
                    float(position),
                ),
            )
        )
    return tuple(sorted(found, key=lambda candidate: candidate.key))


def candidates_for(
    group: RackGroup,
    rack: Sequence[NozzleRackSlot],
    algorithm: RackAlgorithm,
    usage: Mapping[str, Usage],
    taken: Set[int] = frozenset(),
) -> tuple[RackCandidate, ...]:
    """Every eligible free position for ``group``, best first."""
    return _rank(group, rack_positions(rack), algorithm, usage, taken)


def _reason(group: RackGroup, candidates: Sequence[RackCandidate], algorithm: RackAlgorithm) -> str:
    """The first rule that set the pick apart from the runner-up."""
    if len(candidates) == 1:
        return REASON_ONLY
    best, runner = candidates[0].key, candidates[1].key
    index = next(i for i, (a, b) in enumerate(zip(best, runner, strict=True)) if a != b)
    if index == 0:
        if group.abrasive:
            return f"hardened nozzle for {_materials(group)}"
        return "keeps the hardened nozzles for abrasive filament"
    if index == 1:
        return REASON_COLOR
    if index in (2, 3):
        return _ALGORITHM_REASONS[algorithm]
    return REASON_POSITION


def rank_rack(
    groups: list[RackGroup],
    rack: list[NozzleRackSlot],
    algorithm: RackAlgorithm,
    usage: Mapping[str, Usage],
    manual: Mapping[int, int],
) -> dict[int, Pick]:
    """Picks by group id (spec §3, §5). Manual positions are placed first, each only where
    it fits its sliced group (#1016); then the remaining groups are allocated one at a
    time, the one with the fewest free eligible positions next (ties by group id),
    re-counted before each allocation. A group with no free eligible position has no
    pick. "Let Bambuddy pick" ranks nothing; a manual pick is still kept."""
    positions = rack_positions(rack)
    by_id = {group.group_id: group for group in groups}
    picks: dict[int, Pick] = {}
    taken: set[int] = set()
    for group_id, position in sorted(manual.items()):
        group = by_id.get(group_id)
        chosen = positions.get(position)
        if group is None or chosen is None or position in taken:
            continue
        if not eligible(chosen, group.nozzle_diameter, group.volume_type):
            continue
        candidates = _rank(group, positions, algorithm, usage, frozenset(taken))
        taken.add(position)
        picks[group_id] = Pick(
            group_id=group_id,
            position=position,
            serial=chosen.serial_number,
            reason=REASON_MANUAL,
            unsafe_material=group.abrasive and not hardened(chosen.nozzle_type),
            manual=True,
            candidates=candidates,
        )
    if algorithm == "bambuddy":
        return picks
    remaining = [group for group in groups if group.group_id not in picks]
    while remaining:
        group = min(
            remaining,
            key=lambda g: (len(_available(g, positions, taken)), g.group_id),
        )
        remaining.remove(group)
        candidates = _rank(group, positions, algorithm, usage, frozenset(taken))
        if not candidates:
            continue
        best = positions[candidates[0].position]
        taken.add(candidates[0].position)
        picks[group.group_id] = Pick(
            group_id=group.group_id,
            position=candidates[0].position,
            serial=best.serial_number,
            reason=_reason(group, candidates, algorithm),
            unsafe_material=group.abrasive and not hardened(best.nozzle_type),
            manual=False,
            candidates=candidates,
        )
    return picks


def _warning(kind: WarningKind, message: str) -> FilamentWarning:
    """Every rack warning is plate-wide: ``slot_id`` stays ``None`` (spec §6)."""
    return FilamentWarning(kind=kind, message=message)


def _materials(group: RackGroup) -> str:
    return ", ".join(group.materials) or "this filament"


def _size(group: RackGroup) -> str:
    size = diameter(group.nozzle_diameter)
    return f"{size:g}" if size is not None else group.nozzle_diameter


def manual_for(
    groups: Sequence[RackGroup], position: int | None, algorithm: RackAlgorithm
) -> tuple[dict[int, int], list[FilamentWarning]]:
    """The run's ``manual`` map from the dialog's one rack-side position (spec §5)."""
    if position is None:
        return {}, []
    if not groups:
        return {}, [
            _warning(
                "rack-left-to-bambuddy",
                "Rack pick left to Bambuddy: manual rack pick unused: this plate does not "
                "print from the rack.",
            )
        ]
    ids = sorted(group.group_id for group in groups)
    first, rest = ids[0], ids[1:]
    if not rest:
        return {first: position}, []
    then = "Bambuddy picks for" if algorithm == "bambuddy" else "ScadBuddy ranked"
    noun = "group" if len(rest) == 1 else "groups"
    others = ", ".join(str(group_id) for group_id in rest)
    return {first: position}, [
        _warning(
            "rack-manual-partial",
            f"The slice put the rack side in {len(ids)} nozzle groups: group {first} got "
            f"position {position}, and {then} {noun} {others}.",
        )
    ]


def rack_warnings(
    groups: Sequence[RackGroup],
    rack: Sequence[NozzleRackSlot],
    algorithm: RackAlgorithm,
    picks: Mapping[int, Pick],
    manual: Mapping[int, int],
) -> list[FilamentWarning]:
    """What the run carries about each group's pick (spec §5, §6)."""
    positions = rack_positions(rack)
    found: list[FilamentWarning] = []
    for group in sorted(groups, key=lambda g: g.group_id):
        pick = picks.get(group.group_id)
        wanted = manual.get(group.group_id)
        if wanted is not None and (pick is None or not pick.manual):
            flow = group.volume_type.strip() or "any flow"
            outcome = (
                f"ScadBuddy chose position {pick.position} instead."
                if pick is not None
                else "it was not used."
            )
            found.append(
                _warning(
                    "rack-manual-partial",
                    f"Rack position {wanted} does not fit {group.name} ({_size(group)} mm "
                    f"{flow}), so {outcome}",
                )
            )
        if pick is not None:
            if pick.unsafe_material:
                material = nozzle_material(positions[pick.position].nozzle_type)
                held = f"is {material}" if material else "is not known to be hardened"
                found.append(
                    _warning(
                        "rack-unsafe-material",
                        f"Position {pick.position}, chosen by hand, {held}, and "
                        f"{_materials(group)} is abrasive."
                        if pick.manual
                        else f"No hardened {_size(group)} nozzle in the rack for "
                        f"{_materials(group)}; position {pick.position} {held}.",
                    )
                )
            continue
        if algorithm == "bambuddy":
            options = _available(group, positions, frozenset())
            if group.abrasive and any(not hardened(e.nozzle_type) for e in options.values()):
                found.append(
                    _warning(
                        "rack-unsafe-material",
                        "Bambuddy picks the nozzle and may use a non-hardened one for "
                        f"{_materials(group)}.",
                    )
                )
            continue
        if diameter(group.nozzle_diameter) is None:
            message = f"Rack pick left to Bambuddy: {group.name}: nozzle size unreadable."
        else:
            message = f"Rack pick left to Bambuddy: no eligible position for {group.name}."
        found.append(_warning("rack-left-to-bambuddy", message))
    return found
