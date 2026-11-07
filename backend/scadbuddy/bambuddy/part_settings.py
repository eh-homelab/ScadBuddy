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

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.render.bambu3mf import PartOverrides

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

    def metadata(self) -> tuple[tuple[str, str], ...]:
        """Each setting sent, as the 3MF stores it, sorted by key."""
        values: list[tuple[str, str]] = []
        for key, value in sorted(self.model_dump(exclude_none=True).items()):
            text = f"{value}%" if key == "sparse_infill_density" else str(value)
            values.append((key, text))
        return tuple(values)


#: A filament slot, from 1, as ``FilamentPlan.slots[].slot_id`` numbers it.
SlotId = Annotated[int, Field(ge=1, le=64)]
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


def unknown_slots(value: PartSettingsMap | None, slots: int) -> list[int]:
    """The slots ``value`` names that a file of ``slots`` filaments lacks."""
    return [slot for slot in (normalised(value) or {}) if slot > slots]
