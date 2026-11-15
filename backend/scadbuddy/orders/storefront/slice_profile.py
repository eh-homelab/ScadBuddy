"""The shop's slice profile (#6153): what a storefront variant is sliced with that a
visitor cannot choose. It holds the registry printer, which routes the slice and prices
machine time, and one spool per material. The spool picks the filament preset only: the
slot's colour comes from the 3MF, and its price comes from the price-book filament the
visitor picked.

The profile holds no process, support, bed or nozzle setting. As in the back office, the
template gives the support and process settings, and the print dialog's remembered
choices give the rest (owner decision of 2026-11-14 on #6153: "Quoting should be no
different than slicing and printing in back office")."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from scadbuddy.bambuddy.models import SlotChoice
from scadbuddy.orders.storefront.wire import is_support

if TYPE_CHECKING:
    from scadbuddy.bambuddy.resolver import PrintChoices
    from scadbuddy.orders.quote import SliceQuoteRequest

#: The most materials a profile names.
MAX_MATERIALS = 50

Material = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class StorefrontSliceProfile(BaseModel):
    """The stored setting ``storefront_slice_profile`` (#6153)."""

    model_config = ConfigDict(extra="forbid")

    #: The registry printer: it routes the slice and prices machine time.
    printer: str = Field(min_length=1, max_length=64)
    #: Material (casefolded) to a spool id as the print dialog's ``filament_plan`` names
    #: it: a Bambuddy spool, or a loaded tray's negative id on a native route.
    spools: dict[Material, int] = Field(default_factory=dict, max_length=MAX_MATERIALS)

    @field_validator("spools")
    @classmethod
    def _casefolded(cls, spools: dict[str, int]) -> dict[str, int]:
        folded: dict[str, int] = {}
        for material, spool in spools.items():
            key = material.casefold()
            if key in folded:
                raise ValueError(f"two entries name the material {key!r}")
            folded[key] = spool
        return folded


class ProfileMissingMaterialError(LookupError):
    """The profile names no spool for ``material``."""

    def __init__(self, material: str) -> None:
        super().__init__(f"the shop slice profile has no spool for {material!r}")
        self.material = material


def profile_digest(profile: StorefrontSliceProfile) -> str:
    """The sha256 of the profile's content: a part of a storefront slice key."""
    body = json.dumps(profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def slice_request(
    profile: StorefrontSliceProfile, slot_materials: Sequence[str], choices: PrintChoices
) -> SliceQuoteRequest:
    """The slice quote's request for a variant whose slot ``i`` (from 0) is of
    ``slot_materials[i]``: one spool per slot by its material, the profile's printer, and
    the dialog's ``choices``. It sets no path, so the slice takes the run's own route, and
    no supports, prime tower, print sequence or part settings, so the template's apply.
    `ProfileMissingMaterialError` for a material with no spool."""
    # Here, not at the top: the settings store imports this module, and the quote's
    # imports reach the settings store.
    from scadbuddy.bambuddy.filaments import FilamentPlan
    from scadbuddy.orders.quote import SliceQuoteRequest

    slots: list[SlotChoice] = []
    for index, material in enumerate(slot_materials):
        spool = profile.spools.get(material.casefold())
        if spool is None:
            raise ProfileMissingMaterialError(material)
        slots.append(SlotChoice(slot_id=index + 1, spool_id=spool))
    return SliceQuoteRequest(
        printer=profile.printer, filament_plan=FilamentPlan(slots=slots), choices=choices
    )


def missing_materials(
    profile: StorefrontSliceProfile | None, materials: Iterable[str]
) -> list[str]:
    """The materials of ``materials`` that are not support materials and have no spool in
    ``profile``, each once (casefolded), in the order first seen. With no profile there is
    no slice price, so nothing is missing (#6153)."""
    if profile is None:
        return []
    missing: dict[str, str] = {}
    for material in materials:
        key = material.casefold()
        if is_support(material) or key in profile.spools or key in missing:
            continue
        missing[key] = material
    return list(missing.values())
