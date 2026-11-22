"""The shop's slice profile (#6153, #6574): what a storefront variant is sliced with that
a visitor cannot choose. It holds the registry printer, which routes the slice and prices
machine time, and one filament type per material (`filament/types.py`, #6262). The slot's
colour is the visitor's (the render's 3MF has it), and its price comes from the
price-book filament the visitor picked.

The profile names no spool, tray or preset (#6574). A slot is sliced with its type's
preset for the H2C and the chosen nozzle (`filament_spool_type_presets`, the presets a
spool of that type takes too), with no spool, in the file's own colour
(``bambuddy/resolver.py`` ``resolve``, ``slot_presets``). So every slot has its own
colour, and the slicer plans the real purges. A tray id moved when a spool was swapped,
and a spool's colour is not the visitor's; a type's preset is neither. The profile keeps
no copy of a preset: a change to the type's presets is the next price's.

The profile holds no process, support, bed or nozzle setting. As in the back office, the
template (or a file listing's file, #6710) gives the support and process settings, and
the print dialog's opening choices and the setup it seeds from the remembered choices
give the rest (owner decision of 2026-11-14 on #6153: "Quoting should be no different
than slicing and printing in back office"; #6710 §7.2, decided 2026-11-21)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from scadbuddy.orders.storefront.wire import is_support

if TYPE_CHECKING:
    from scadbuddy.bambuddy.resolver import PrintChoices
    from scadbuddy.filament.models import EffectivePreset
    from scadbuddy.library.settings_store import ModelPrintChoices
    from scadbuddy.orders.quote import SliceQuoteRequest

#: The most materials a profile names.
MAX_MATERIALS = 50

Material = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class StorefrontSliceProfile(BaseModel):
    """The stored setting ``storefront_slice_profile`` (#6153, #6574)."""

    model_config = ConfigDict(extra="forbid")

    #: The registry printer: it routes the slice and prices machine time.
    printer: str = Field(min_length=1, max_length=64)
    #: Material (casefolded) to the filament type (`filament_spool_types.id`) whose
    #: presets a slot of it is sliced with.
    materials: dict[Material, UUID] = Field(default_factory=dict, max_length=MAX_MATERIALS)

    @field_validator("materials")
    @classmethod
    def _casefolded(cls, materials: dict[str, UUID]) -> dict[str, UUID]:
        folded: dict[str, UUID] = {}
        for material, type_id in materials.items():
            key = material.casefold()
            if key in folded:
                raise ValueError(f"two entries name the material {key!r}")
            folded[key] = type_id
        return folded


class ProfileMissingMaterialError(LookupError):
    """The profile names no filament type for ``material``."""

    def __init__(self, material: str) -> None:
        super().__init__(f"the shop slice profile has no filament type for {material!r}")
        self.material = material


def profile_digest(profile: StorefrontSliceProfile) -> str:
    """The sha256 of the profile's content: a part of a storefront slice key."""
    body = json.dumps(profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def slot_types(profile: StorefrontSliceProfile, slot_materials: Sequence[str]) -> list[UUID]:
    """The filament type of each slot (from 0) whose material is ``slot_materials[i]``.
    `ProfileMissingMaterialError` for a material with no type."""
    types: list[UUID] = []
    for material in slot_materials:
        type_id = profile.materials.get(material.casefold())
        if type_id is None:
            raise ProfileMissingMaterialError(material)
        types.append(type_id)
    return types


def slot_presets(
    types: Sequence[UUID], presets: Mapping[UUID, Sequence[EffectivePreset]]
) -> dict[int, list[EffectivePreset]]:
    """Each slot's filament type's presets, by slot id (from 1): what the resolver reads
    for a slot with no spool (``resolve``'s ``slot_presets``). A type with no presets
    gives an empty list, which the resolver refuses as a slot with no preset."""
    return {index + 1: list(presets.get(type_id, ())) for index, type_id in enumerate(types)}


def slice_request(
    profile: StorefrontSliceProfile,
    choices: PrintChoices,
    remembered: ModelPrintChoices | None,
) -> SliceQuoteRequest:
    """The slice quote's request for a variant: the profile's printer, the dialog's
    ``choices`` and no spool. Each slot's filament type's presets go beside it
    (:func:`slot_presets`), so each slot is sliced with its type's preset in the render's
    own colour (#6574). From the ``remembered`` entry of the subject's scope it takes what
    the print dialog seeds from it (#6710 §7.2, ``usePrintChoices.ts`` ``seedDialog``): the
    print sequence, the process overrides (any Bambu Studio key, #2998) and the settings
    per part. It sets no path, so the slice takes the run's own route, and no supports or
    prime tower; with nothing remembered it sets none of the three either, so the
    template's (or the file's own) settings apply."""
    # Here, not at the top: the settings store imports this module, and the quote's
    # imports reach the settings store.
    from scadbuddy.bambuddy.filaments import FilamentPlan
    from scadbuddy.orders.quote import SliceQuoteRequest

    return SliceQuoteRequest(
        printer=profile.printer,
        filament_plan=FilamentPlan(ids="sb"),
        choices=choices,
        print_sequence=None if remembered is None else remembered.print_sequence,
        process_overrides=None if remembered is None else remembered.process_overrides,
        part_settings=None if remembered is None else remembered.part_settings,
    )


def missing_materials(
    profile: StorefrontSliceProfile | None, materials: Iterable[str]
) -> list[str]:
    """The materials of ``materials`` that are not support materials and have no type in
    ``profile``, each once (casefolded), in the order first seen. With no profile there
    is no slice price, so nothing is missing (#6153)."""
    if profile is None:
        return []
    missing: dict[str, str] = {}
    for material in materials:
        key = material.casefold()
        if is_support(material) or key in profile.materials or key in missing:
            continue
        missing[key] = material
    return list(missing.values())
