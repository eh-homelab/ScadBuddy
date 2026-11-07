"""Slicer settings per filament slot for one print (#5571).

A print can set a few Bambu Studio process settings on the parts of one colour: more
top layers on raised letters, say. They go into the laid-out 3MF as each part's
``<metadata key= value=/>`` in ``Metadata/model_settings.config``
(:func:`~scadbuddy.render.bambu3mf.set_part_settings`), which the slicer reads over
the process preset for that part alone. The process preset and the whole-print
overrides stay as they are.

The keys and their limits are Bambu Studio v02.08.02.61's
(``src/libslic3r/PrintConfig.cpp``): ``top_shell_layers`` (line 5954, coInt, min 0),
``bottom_shell_layers`` (line 1363, coInt, min 0), ``wall_loops`` (line 4596, coInt,
0-1000), ``sparse_infill_density`` (line 3085, coPercent, 0-100),
``sparse_infill_pattern`` (line 3102, its ``enum_values``) and ``ironing_type`` (line
4069, its ``enum_values``). The slicer sets no maximum for the shell layers; ScadBuddy
takes the ``wall_loops`` bound, 1000, for them too.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_serializer

from scadbuddy.render.bambu3mf import PartOverrides
from scadbuddy.render.colours import colour_hex

#: The most layers or loops a part setting takes: ``wall_loops``' own maximum.
MAX_LAYERS = 1000

#: ``sparse_infill_pattern``'s ``enum_values`` (PrintConfig.cpp lines 3107-3128).
SparseInfillPattern = Literal[
    "concentric",
    "zig-zag",
    "grid",
    "line",
    "cubic",
    "triangles",
    "tri-hexagon",
    "gyroid",
    "honeycomb",
    "adaptivecubic",
    "alignedrectilinear",
    "3dhoneycomb",
    "hilbertcurve",
    "archimedeanchords",
    "octagramspiral",
    "supportcubic",
    "lightning",
    "crosshatch",
    "zigzag",
    "crosszag",
    "lockedzag",
    "2dlattice",
]
#: ``ironing_type``'s ``enum_values`` (PrintConfig.cpp lines 4075-4078).
IroningType = Literal["no ironing", "top", "topmost", "solid"]


def without_unset(dumped: dict[str, Any]) -> dict[str, Any]:
    """``dumped`` without its unset (``None``) values."""
    return {key: value for key, value in dumped.items() if value is not None}


class PartSettings(BaseModel):
    """The process settings for the parts of one slot; each omitted one is the
    process's."""

    model_config = ConfigDict(extra="forbid")

    top_shell_layers: int | None = Field(default=None, ge=0, le=MAX_LAYERS)
    bottom_shell_layers: int | None = Field(default=None, ge=0, le=MAX_LAYERS)
    wall_loops: int | None = Field(default=None, ge=0, le=MAX_LAYERS)
    #: Percent, 0-100.
    sparse_infill_density: int | None = Field(default=None, ge=0, le=100)
    sparse_infill_pattern: SparseInfillPattern | None = None
    ironing_type: IroningType | None = None

    # Only the settings set (#5627): an unset one is the process's, and is left out, so
    # what a read gives back (`get_print`, `model_choices`) is a value a request takes
    # again, the agent's strict `part_settings` among them. A lambda, so the serializer
    # has no return annotation: with one, pydantic gives the answer a schema of its own
    # (`PartSettings-Output`), and the API's request and answer types would part.
    _set_only = model_serializer(mode="wrap")(lambda self, handler: without_unset(handler(self)))

    def metadata(self) -> tuple[tuple[str, str], ...]:
        """Each setting sent, as the 3MF stores it, sorted by key."""
        values: list[tuple[str, str]] = []
        for key, value in sorted(self.model_dump(exclude_none=True).items()):
            text = f"{value}%" if key == "sparse_infill_density" else str(value)
            values.append((key, text))
        return tuple(values)


#: The highest filament slot a request names.
MAX_SLOT = 64
#: A filament slot, from 1, as ``FilamentPlan.slots[].slot_id`` numbers it.
SlotId = Annotated[int, Field(ge=1, le=MAX_SLOT)]
#: What a request's ``part_settings`` holds: the settings for each slot's parts.
PartSettingsMap = dict[SlotId, PartSettings]


def normalised(value: PartSettingsMap | None) -> PartSettingsMap | None:
    """``value`` without slots that set nothing, in slot order; ``None`` when nothing
    is left, so a request that sets nothing keys as one that sent no field."""
    if value is None:
        return None
    kept = {slot: value[slot] for slot in sorted(value) if value[slot].metadata()}
    return kept or None


def overrides_of(value: PartSettingsMap | None) -> PartOverrides | None:
    """``value`` as the 3MF writer takes it; ``None`` for nothing to write."""
    kept = normalised(value)
    if kept is None:
        return None
    return tuple((slot, settings.metadata()) for slot, settings in kept.items())


def unknown_slots(value: PartSettingsMap | None, slots: Collection[int]) -> list[int]:
    """The slots ``value`` names that no printed part of the file is made in: ``slots``
    are those its parts are (:func:`~scadbuddy.render.bambu3mf.printed_part_slots`)."""
    return [slot for slot in (normalised(value) or {}) if slot not in slots]


def dumped(value: PartSettingsMap | None) -> dict[str, dict[str, Any]] | None:
    """``value`` as a quote stores it (#5571): slots as strings, each setting sent, in slot
    order; ``None`` for nothing set. Two requests set the same when these are equal."""
    kept = normalised(value)
    if kept is None:
        return None
    return {str(slot): settings.model_dump(exclude_none=True) for slot, settings in kept.items()}


def process_part_settings(values: Mapping[str, Any]) -> PartSettings:
    """The process's own value of each part setting (#5627), from a process preset's
    flattened values (``PresetValues.values``: a string, or a list of strings for a
    vector option, ``slicing/presets.py`` ``apply_overrides``). The print dialog opens
    each control on it. A value the preset leaves out, or one out of :class:`PartSettings`'
    bounds, is left out: the dialog then shows the control empty."""
    read: dict[str, Any] = {}
    for key in PartSettings.model_fields:
        value = values.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if not isinstance(value, str):
            continue
        text = value.strip()
        if key == "sparse_infill_density":
            text = text.removesuffix("%").strip()
        try:
            read[key] = PartSettings.model_validate({key: text}).model_dump()[key]
        except ValidationError:
            continue
    return PartSettings.model_validate(read)


#: How a template's ``part_print_settings`` (#5626) names a part: by the OpenSCAD
#: identifier of its ``// color`` parameter, never by slot, since the slots follow the
#: colours a render uses and move when two colours become one or a colour comes or goes.
COLOUR_PARAMETER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
ColourParameter = Annotated[str, Field(pattern=COLOUR_PARAMETER_PATTERN, max_length=200)]


def by_colour(
    declared: Mapping[str, PartSettings],
    params: Mapping[str, object],
    colours: Sequence[str],
) -> PartSettingsMap | None:
    """A template's ``part_print_settings`` (#5626), named by colour parameter, as the
    slots of one output: each parameter's value in ``params`` (the output's) is matched
    to the slot of that colour in ``colours`` (the output's, in slot order). A parameter
    the output has no value for, or whose colour no slot is, sets nothing. Two
    parameters of one colour share its slot; for a key both set, the one declared first
    wins. A colour past slot :data:`MAX_SLOT` sets nothing (#5649). ``None`` when
    nothing is left."""
    slots = {colour_hex(colour): index for index, colour in enumerate(colours[:MAX_SLOT], start=1)}
    found: dict[int, dict[str, Any]] = {}
    for name, settings in declared.items():
        hex_value = colour_hex(params.get(name))
        slot = slots.get(hex_value) if hex_value is not None else None
        if slot is None:
            continue
        found[slot] = {**settings.model_dump(exclude_none=True), **found.get(slot, {})}
    return normalised({slot: PartSettings(**values) for slot, values in found.items()})


def laid_under(
    template: PartSettingsMap | None, request: PartSettingsMap | None
) -> PartSettingsMap | None:
    """The template's settings per slot (#5626) with the request's over them: the
    request wins per key, and a key only the template sets stays."""
    merged: dict[int, PartSettings] = {}
    for slot in sorted({*(template or {}), *(request or {})}):
        under = (template or {}).get(slot)
        over = (request or {}).get(slot)
        merged[slot] = PartSettings(
            **{
                **(under.model_dump(exclude_none=True) if under is not None else {}),
                **(over.model_dump(exclude_none=True) if over is not None else {}),
            }
        )
    return normalised(merged)
