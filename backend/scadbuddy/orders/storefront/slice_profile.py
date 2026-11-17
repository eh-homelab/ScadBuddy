"""The shop's slice profile (#6153, #6574): what a storefront variant is sliced with that
a visitor cannot choose. It holds the registry printer, which routes the slice and prices
machine time, and one filament preset per material. The slot's colour is the visitor's
(the render's 3MF has it), and its price comes from the price-book filament the visitor
picked.

The profile names no spool or tray (#6574). A slot is sliced as a back-office quote slices
a slot with a filament that is not loaded: no spool, and the material's preset as the
slot's Advanced preset (``choices.filament_overrides``), in the file's own colour
(``bambuddy/resolver.py`` ``resolve``, ``unspooled``). So every slot has its own colour,
and the slicer plans the real purges. A tray id moved when a spool was swapped; a preset
does not.

The profile holds no process, support, bed or nozzle setting. As in the back office, the
template gives the support and process settings, and the print dialog's opening choices
give the rest (owner decision of 2026-11-14 on #6153: "Quoting should be no different
than slicing and printing in back office")."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.orders.storefront.wire import is_support

if TYPE_CHECKING:
    from scadbuddy.bambuddy.resolver import PrintChoices
    from scadbuddy.orders.quote import SliceQuoteRequest

#: The most materials a profile names.
MAX_MATERIALS = 50

Material = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class StorefrontSliceProfile(BaseModel):
    """The stored setting ``storefront_slice_profile`` (#6153, #6574)."""

    model_config = ConfigDict(extra="forbid")

    #: The registry printer: it routes the slice and prices machine time.
    printer: str = Field(min_length=1, max_length=64)
    #: Material (casefolded) to the filament preset a slot of it is sliced with, as the
    #: print dialog's Advanced per-slot preset names it (``PrintChoices.filament_overrides``).
    materials: dict[Material, PresetRef] = Field(default_factory=dict, max_length=MAX_MATERIALS)

    @field_validator("materials")
    @classmethod
    def _casefolded(cls, materials: dict[str, PresetRef]) -> dict[str, PresetRef]:
        folded: dict[str, PresetRef] = {}
        for material, preset in materials.items():
            key = material.casefold()
            if key in folded:
                raise ValueError(f"two entries name the material {key!r}")
            folded[key] = preset
        return folded


class ProfileMissingMaterialError(LookupError):
    """The profile names no filament preset for ``material``."""

    def __init__(self, material: str) -> None:
        super().__init__(f"the shop slice profile has no filament preset for {material!r}")
        self.material = material


def profile_digest(profile: StorefrontSliceProfile) -> str:
    """The sha256 of the profile's content: a part of a storefront slice key."""
    body = json.dumps(profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def slot_presets(profile: StorefrontSliceProfile, slot_materials: Sequence[str]) -> list[PresetRef]:
    """The filament preset of each slot (from 0) whose material is ``slot_materials[i]``.
    `ProfileMissingMaterialError` for a material with no preset."""
    presets: list[PresetRef] = []
    for material in slot_materials:
        preset = profile.materials.get(material.casefold())
        if preset is None:
            raise ProfileMissingMaterialError(material)
        presets.append(preset)
    return presets


def slice_request(
    profile: StorefrontSliceProfile, slot_materials: Sequence[str], choices: PrintChoices
) -> SliceQuoteRequest:
    """The slice quote's request for a variant whose slot ``i`` (from 0) is of
    ``slot_materials[i]``: the profile's printer, the dialog's ``choices`` with each
    slot's material preset as its Advanced preset, and no spool, so each slot is sliced
    in the render's own colour for it (#6574). A back-office quote that names the same
    presets and no spools sends this request. It sets no path, so the slice takes the
    run's own route, and no supports, prime tower, print sequence or part settings, so
    the template's apply. `ProfileMissingMaterialError` for a material with no preset."""
    # Here, not at the top: the settings store imports this module, and the quote's
    # imports reach the settings store.
    from scadbuddy.bambuddy.filaments import FilamentPlan
    from scadbuddy.orders.quote import SliceQuoteRequest

    presets = slot_presets(profile, slot_materials)
    overrides = {index + 1: preset for index, preset in enumerate(presets)}
    return SliceQuoteRequest(
        printer=profile.printer,
        filament_plan=FilamentPlan(),
        choices=choices.model_copy(update={"filament_overrides": overrides}),
    )


def missing_materials(
    profile: StorefrontSliceProfile | None, materials: Iterable[str]
) -> list[str]:
    """The materials of ``materials`` that are not support materials and have no preset
    in ``profile``, each once (casefolded), in the order first seen. With no profile there
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
