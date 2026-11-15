"""The shop slice profile's choices and its check (#6153 PR C): the price-book materials,
and the spools the print dialog offers on the profile's printer.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
import time."""

import asyncio
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filaments import normalise_colour
from scadbuddy.bambuddy.print_run import dialog_route
from scadbuddy.bambuddy.trays import loaded_trays
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.storefront.slice_profile import StorefrontSliceProfile
from scadbuddy.orders.storefront.wire import is_support
from scadbuddy.printers.component import Printers, PrintersDep
from scadbuddy.spools.component import Spools, SpoolsDep
from scadbuddy.spools.overlay import status_overlay

router = APIRouter(prefix="/storefront/slice-profile", tags=["storefront"])

#: Where a refusal of the profile points in a ``PUT /settings`` body.
FIELD = "storefront_slice_profile"


class ProfileMaterial(BaseModel):
    """A material of the price book's filaments not archived."""

    material: str
    #: A support material (`wire.py` `is_support`): the shop's own, never a visitor's.
    support: bool


class ProfileSpool(BaseModel):
    """A spool the print dialog offers on the profile's printer, under the id its
    ``filament_plan`` names: a Bambuddy spool, or a loaded tray's negative id on a native
    route."""

    spool_id: int
    material: str
    colour: str | None = None
    label: str


class ProfileOptions(BaseModel):
    materials: list[ProfileMaterial]
    #: Empty with no printer named.
    spools: list[ProfileSpool]


def _label(*parts: str | None) -> str:
    return " ".join(part for part in parts if part)


async def offered_spools(
    settings: StoredSettings, printers: Printers, spools: Spools, printer: str
) -> list[ProfileSpool] | None:
    """The spools the print dialog offers on registry printer ``printer``, as its route
    reads them: the loaded trays on a native route, else Bambuddy's inventory. ``None``
    when the registry has no such printer."""
    row = await asyncio.to_thread(printers.store.get, printer)
    if row is None:
        return None
    route = await dialog_route(
        settings, printers.store, printers.reports, printer=row.id, printer_id=None
    )
    if route is not None and route.row is not None and route.status is not None:
        overlay = await status_overlay(spools, route.row, route.status, now=datetime.now(UTC))
        trays = loaded_trays(route.status, route.printer_id or 0, route.row.name, overlay)
        return [
            ProfileSpool(
                spool_id=tray.spool_id,
                material=tray.material,
                colour=tray.colour,
                label=_label(tray.label, tray.material, tray.color_name),
            )
            for tray in trays
        ]
    async with client_for(settings) as client:
        rows = await client.spools()
    return [
        ProfileSpool(
            spool_id=spool.id,
            material=spool.material,
            colour=normalise_colour(spool.rgba),
            label=_label(spool.brand, spool.material, spool.subtype, spool.color_name)
            + f" (#{spool.id})",
        )
        for spool in rows
    ]


def profile_errors(
    profile: StorefrontSliceProfile, offered: Collection[int] | None
) -> list[dict[str, Any]]:
    """What is wrong with ``profile``, as a 422's ``errors``: a printer the registry does
    not hold (``offered`` is ``None``), or a spool the dialog does not offer on it."""
    if offered is None:
        return [{"loc": ["body", FIELD, "printer"], "msg": f"no printer {profile.printer}"}]
    return [
        {
            "loc": ["body", FIELD, "spools", material],
            "msg": f"the printer offers no spool {spool} for {material}",
        }
        for material, spool in profile.spools.items()
        if spool not in offered
    ]


async def check_profile(
    profile: StorefrontSliceProfile, settings: StoredSettings, printers: Printers, spools: Spools
) -> None:
    """422 unless the profile's printer is in the registry and the print dialog offers
    each of its spools there (#6153)."""
    offered = await offered_spools(settings, printers, spools, profile.printer)
    errors = profile_errors(profile, None if offered is None else {s.spool_id for s in offered})
    if errors:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "; ".join(error["msg"] for error in errors),
            errors=errors,
        )


@router.get(
    "/options",
    response_model=ProfileOptions,
    summary="What the shop slice profile can name",
)
async def get_options(
    orders: OrdersDep,
    store: SettingsStoreDep,
    printers: PrintersDep,
    spools: SpoolsDep,
    printer: Annotated[str | None, Query(max_length=64)] = None,
) -> ProfileOptions:
    """Each material of the price book's filaments not archived, with whether it is a
    support material, and the spools the print dialog offers on registry printer
    ``printer`` (none without one; 422 for a printer the registry does not hold)."""
    filaments = await asyncio.to_thread(orders.store.filaments)
    materials: dict[str, str] = {}
    for filament in filaments:
        materials.setdefault(filament.material.casefold(), filament.material)
    offered: list[ProfileSpool] = []
    if printer is not None:
        found = await offered_spools(store.load(), printers, spools, printer)
        if found is None:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, f"no printer {printer}")
        offered = found
    return ProfileOptions(
        materials=[
            ProfileMaterial(material=material, support=is_support(material))
            for material in sorted(materials.values(), key=str.casefold)
        ],
        spools=offered,
    )
