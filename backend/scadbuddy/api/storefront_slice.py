"""The shop slice profile's choices and its check (#6153 PR C, #6574): the price-book
materials, and the filament types (`filament/types.py`, #6262) whose presets a slot of a
material is sliced with.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
import time."""

import asyncio
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel

from scadbuddy.core.problems import ApiError
from scadbuddy.filament.component import Filament, FilamentDep
from scadbuddy.filament.models import Material
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.storefront.slice_profile import StorefrontSliceProfile
from scadbuddy.orders.storefront.wire import is_support
from scadbuddy.printers.component import Printers

router = APIRouter(prefix="/storefront/slice-profile", tags=["storefront"])

#: Where a refusal of the profile points in a ``PUT /settings`` body.
FIELD = "storefront_slice_profile"


class ProfileMaterial(BaseModel):
    """A material of the price book's filaments not archived."""

    material: str
    #: A support material (`wire.py` `is_support`): the shop's own, never a visitor's.
    support: bool


class ProfileType(BaseModel):
    """A filament type not archived (`filament_spool_types`). A slot of its material is
    sliced with its preset for the H2C and the nozzle the dialog opens on (#6574); a
    type with no such preset gives no slice price (`NO_TYPE_PRESET`)."""

    id: UUID
    code: str
    #: The type's catalog material, as its name reads.
    material: str
    label: str


class ProfileOptions(BaseModel):
    materials: list[ProfileMaterial]
    #: The filament types a material can name (#6574), by code.
    types: list[ProfileType]


def _label(*parts: str | None) -> str:
    return " ".join(part for part in parts if part)


def _types(filament: Filament) -> list[ProfileType]:
    """The types not archived, each with its material's name. Blocking."""
    materials = {
        row.id: row.name
        for row in filament.catalog.list("materials", include_archived=True)
        if isinstance(row, Material)
    }
    return [
        ProfileType(
            id=row.id,
            code=row.code,
            material=materials.get(row.material_id, ""),
            label=_label(row.code, materials.get(row.material_id), row.colour_name),
        )
        for row in filament.types.list()
    ]


def profile_errors(
    profile: StorefrontSliceProfile, known: bool, types: set[UUID]
) -> list[dict[str, object]]:
    """What is wrong with ``profile``, as a 422's ``errors``: a printer the registry does
    not hold, and a material whose filament type is not one of ``types`` (the types not
    archived). A type with no preset for the nozzle gives that variant no price."""
    errors: list[dict[str, object]] = []
    if not known:
        errors.append({"loc": ["body", FIELD, "printer"], "msg": f"no printer {profile.printer}"})
    for material, type_id in profile.materials.items():
        if type_id not in types:
            errors.append(
                {
                    "loc": ["body", FIELD, "materials", material],
                    "msg": f"no filament type {type_id} for {material}",
                }
            )
    return errors


async def check_profile(
    profile: StorefrontSliceProfile, printers: Printers, filament: Filament
) -> None:
    """422 unless the profile's printer is in the registry and each of its filament types
    is one not archived (#6153, #6574)."""
    row = await asyncio.to_thread(printers.store.get, profile.printer)
    types = await asyncio.to_thread(filament.types.list)
    errors = profile_errors(profile, row is not None, {t.id for t in types})
    if errors:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "; ".join(str(error["msg"]) for error in errors),
            errors=errors,
        )


@router.get(
    "/options",
    response_model=ProfileOptions,
    summary="What the shop slice profile can name",
)
async def get_options(orders: OrdersDep, filament: FilamentDep) -> ProfileOptions:
    """Each material of the price book's filaments not archived, with whether it is a
    support material, and the filament types not archived, whose presets a slot of a
    material is sliced with (#6574)."""
    filaments = await asyncio.to_thread(orders.store.filaments)
    materials: dict[str, str] = {}
    for book in filaments:
        materials.setdefault(book.material.casefold(), book.material)
    return ProfileOptions(
        materials=[
            ProfileMaterial(material=material, support=is_support(material))
            for material in sorted(materials.values(), key=str.casefold)
        ],
        types=await asyncio.to_thread(_types, filament),
    )
