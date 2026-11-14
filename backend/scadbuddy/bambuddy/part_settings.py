"""Slicer settings per filament slot for one print (#5571).

A print can set Bambu Studio process settings on the parts of one colour: more top
layers on raised letters, say. They go into the laid-out 3MF as each part's
``<metadata key= value=/>`` in ``Metadata/model_settings.config``
(:func:`~scadbuddy.render.bambu3mf.set_part_settings`), which the slicer reads over
the process preset for that part alone. The process preset and the whole-print
overrides stay as they are.

Any Bambu Studio key is taken (#2998, the owner's decision of 2026-11-14): no allowlist,
no type or range check. Each value is written as a Bambu config stores it
(``process_overrides.scalar_text``), and the slicer judges it. The six keys the dialog
first offered (#5627) stay named fields, so a request that sets them keys as before
(``runs.run_key``): ``top_shell_layers``, ``bottom_shell_layers``, ``wall_loops``,
``sparse_infill_density`` (a number is a percent), ``sparse_infill_pattern`` and
``ironing_type`` (Bambu Studio v02.08.02.61, ``src/libslic3r/PrintConfig.cpp``).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.process_overrides import (
    ProcessInput,
    ProcessScalar,
    joined_text,
    scalar_text,
)
from scadbuddy.render.bambu3mf import PartOverrides
from scadbuddy.render.colours import colour_hex

#: The part settings the process's own value is read for, to open the dialog's first
#: controls on (#5627, :func:`process_part_settings`), each with whether it is a number.
NUMBER_KEYS = ("top_shell_layers", "bottom_shell_layers", "wall_loops", "sparse_infill_density")


def without_unset(dumped: dict[str, Any]) -> dict[str, Any]:
    """``dumped`` without its unset (``None``) values."""
    return {key: value for key, value in dumped.items() if value is not None}


def parts_set_only(
    dumped: dict[str, Any], *, maps: Collection[str] = (), single: Collection[str] = ()
) -> dict[str, Any]:
    """A read model's dump (#5627) with only the settings set in each field of ``maps``
    (a dumped :data:`PartSettingsMap`) and ``single`` (a dumped :class:`PartSettings`):
    an unset one is the process's, so what a read gives back (``get_print``,
    ``model_choices``, a check) is a value a request takes again, the agent's strict
    ``part_settings`` among them. Only read models strip them: a request's own dump stays
    as it was, since ``runs.run_key`` digests it, and a changed key would start a second
    print for a retry that spans the deploy. Called from a lambda ``model_serializer``,
    which has no return annotation: with one, pydantic gives the answer a schema of its
    own (``-Output``), and the API's request and answer types would part."""
    for field in maps:
        value = dumped.get(field)
        if isinstance(value, dict):
            dumped[field] = {
                slot: without_unset(inner) if isinstance(inner, dict) else inner
                for slot, inner in value.items()
            }
    for field in single:
        value = dumped.get(field)
        if isinstance(value, dict):
            dumped[field] = without_unset(value)
    return dumped


class PartSettings(BaseModel):
    """The process settings for the parts of one slot; each omitted one is the
    process's. Any Bambu Studio key (#2998): the named fields are the dialog's first six,
    and every other key is kept as sent."""

    model_config = ConfigDict(extra="allow")
    #: Every other key: a value, or a list for a per-extruder option.
    __pydantic_extra__: dict[str, ProcessInput] = Field(init=False)

    top_shell_layers: ProcessScalar | None = None
    bottom_shell_layers: ProcessScalar | None = None
    wall_loops: ProcessScalar | None = None
    #: A number is a percent (written ``15%``); a string goes as written.
    sparse_infill_density: ProcessScalar | None = None
    sparse_infill_pattern: ProcessScalar | None = None
    ironing_type: ProcessScalar | None = None

    def metadata(self) -> tuple[tuple[str, str], ...]:
        """Each setting sent, as the 3MF stores it, sorted by key."""
        values: list[tuple[str, str]] = []
        for key, value in sorted(self.model_dump(exclude_none=True).items()):
            if (
                key == "sparse_infill_density"
                and isinstance(value, int | float)
                and not (isinstance(value, bool))
            ):
                text = f"{scalar_text(value)}%"
            else:
                text = joined_text(value)
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


def _decimal(text: str) -> Decimal | None:
    """``text`` as a finite number; ``None`` when it is not one."""
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _whole_percent(text: str) -> str:
    """``text`` rounded to a whole number, half up; as it is when it is not a number, or
    too large to round (``1e30``: ``quantize`` refuses it), which is then kept as text."""
    number = _decimal(text)
    if number is None:
        return text
    try:
        return str(number.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return text


def inexact_part_settings(values: Mapping[str, Any]) -> list[str]:
    """The part settings whose process value :func:`process_part_settings` rounded (#5627):
    a fractional density (``15.5%``) opens the control on 16, but 16 is still a change
    from what the process prints, so the dialog sends it."""
    value = values.get("sparse_infill_density")
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return []
    number = _decimal(value.strip().removesuffix("%").strip())
    whole = number is None or number == number.to_integral_value()
    return [] if whole else ["sparse_infill_density"]


def process_part_settings(values: Mapping[str, Any]) -> PartSettings:
    """The process's own value of the dialog's first part settings (#5627), from a process
    preset's flattened values (``PresetValues.values``: a string, or a list of strings for
    a vector option, ``slicing/presets.py`` ``apply_overrides``). A whole number of
    :data:`NUMBER_KEYS` is a number (a fractional density, ``15.5%``, rounded to the
    nearest whole percent, half up), any other value its first string. A value the preset
    leaves out is left out. Every other key's process value is
    ``POST /print/process-values``'s."""
    read: dict[str, Any] = {}
    for key in PartSettings.model_fields:
        value = values.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if not isinstance(value, str):
            continue
        text = value.strip()
        if key == "sparse_infill_density":
            text = _whole_percent(text.removesuffix("%").strip())
        if key in NUMBER_KEYS and text.isascii() and text.isdigit():
            read[key] = int(text)
        else:
            read[key] = text
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
