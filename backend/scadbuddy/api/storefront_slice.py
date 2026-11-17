"""The shop slice profile's choices and its check (#6153 PR C, #6574): the price-book
materials, and the filament presets the print dialog offers on the profile's printer.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
import time."""

import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field

from scadbuddy.api.deps import PathsDep, SettingsStoreDep
from scadbuddy.bambuddy.catalogue import (
    Catalogue,
    cached_catalogue,
    catalogue_name_key,
    catalogue_refresh_claim,
)
from scadbuddy.bambuddy.choices import FilamentPresetOption, filament_presets_by_size
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.print_run import dialog_route
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.storefront.slice_profile import StorefrontSliceProfile
from scadbuddy.orders.storefront.wire import is_support
from scadbuddy.printers.component import Printers, PrintersDep
from scadbuddy.slicing.catalogue import catalogue_for
from scadbuddy.slicing.component import SlicingDep
from scadbuddy.slicing.profiles import Profiles

router = APIRouter(prefix="/storefront/slice-profile", tags=["storefront"])

#: Where a refusal of the profile points in a ``PUT /settings`` body.
FIELD = "storefront_slice_profile"
#: The nozzle size whose presets the editor offers: the dialog's default size
#: (``extruders.default_nozzles``). A model remembered at another size is sliced at that
#: size, and a preset that does not fit it gives no price.
OFFERED_SIZE = "0.4"


class ProfileMaterial(BaseModel):
    """A material of the price book's filaments not archived."""

    material: str
    #: A support material (`wire.py` `is_support`): the shop's own, never a visitor's.
    support: bool


class ProfileOptions(BaseModel):
    materials: list[ProfileMaterial]
    #: The nozzle size ``presets`` fit.
    nozzle_size: str = OFFERED_SIZE
    #: The filament presets the print dialog's Advanced step offers on the printer for
    #: ``nozzle_size``, by name (#6574). Empty with no printer named.
    presets: list[FilamentPresetOption] = Field(default_factory=list)


async def _catalogue(
    settings: StoredSettings,
    printers: Printers,
    printer: str,
    *,
    store: SettingsStore,
    paths_cache: Path,
    profiles: Profiles | None,
) -> Catalogue:
    """The preset catalogue the dialog reads for ``printer``: the image's on a route a
    run takes over the LAN, else Bambuddy's (kept between reads, as the dialog keeps it)."""
    route = await dialog_route(
        settings, printers.store, printers.reports, printer=printer, printer_id=None
    )
    if route is not None:
        return await catalogue_for(None, native=profiles)
    async with client_for(settings) as client:
        return await cached_catalogue(
            client,
            paths_cache,
            name_key=catalogue_name_key(store.kek),
            refresh_claim=catalogue_refresh_claim(store.defaults.database_url),
        )


def profile_errors(profile: StorefrontSliceProfile, known: bool) -> list[dict[str, object]]:
    """What is wrong with ``profile``, as a 422's ``errors``: a printer the registry does
    not hold. A preset that does not fit the printer is the slice's to refuse, and the
    variant then has no price."""
    if known:
        return []
    return [{"loc": ["body", FIELD, "printer"], "msg": f"no printer {profile.printer}"}]


async def check_profile(profile: StorefrontSliceProfile, printers: Printers) -> None:
    """422 unless the profile's printer is in the registry (#6153)."""
    row = await asyncio.to_thread(printers.store.get, profile.printer)
    errors = profile_errors(profile, row is not None)
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
async def get_options(
    orders: OrdersDep,
    store: SettingsStoreDep,
    printers: PrintersDep,
    paths: PathsDep,
    slicing: SlicingDep,
    printer: Annotated[str | None, Query(max_length=64)] = None,
) -> ProfileOptions:
    """Each material of the price book's filaments not archived, with whether it is a
    support material, and the filament presets the print dialog offers on registry
    printer ``printer`` for a 0.4 mm nozzle (none without one; 422 for a printer the
    registry does not hold)."""
    filaments = await asyncio.to_thread(orders.store.filaments)
    materials: dict[str, str] = {}
    for filament in filaments:
        materials.setdefault(filament.material.casefold(), filament.material)
    presets: list[FilamentPresetOption] = []
    if printer is not None:
        if await asyncio.to_thread(printers.store.get, printer) is None:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, f"no printer {printer}")
        catalogue = await _catalogue(
            await asyncio.to_thread(store.load),
            printers,
            printer,
            store=store,
            paths_cache=paths.cache,
            profiles=slicing.profiles,
        )
        presets = filament_presets_by_size(catalogue).get(OFFERED_SIZE, [])
    return ProfileOptions(
        materials=[
            ProfileMaterial(material=material, support=is_support(material))
            for material in sorted(materials.values(), key=str.casefold)
        ],
        presets=presets,
    )
