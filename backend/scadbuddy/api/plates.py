"""The plate the preview draws and checks the model against (#81).

Served from :mod:`scadbuddy.render.plate` — the same geometry the 3MF writer lays
the model out on — so the browser never keeps a second table of build volumes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.render.plate import PlateGeometry, plate_for
from scadbuddy.render.plate_profiles import PLATE_PROFILES

router = APIRouter(tags=["plates"])

_LAB_PREFIX = "Bambu Lab "


class PlateArea(BaseModel):
    min_x: float
    min_y: float
    max_x: float
    max_y: float


class PlateView(BaseModel):
    """One printer's build volume, in millimetres."""

    #: The profile's ``printer_model``; ``None`` for the 256 mm fallback plate.
    model: str | None
    #: What the UI calls it: ``"H2C"``, or ``"Default plate"``.
    name: str
    #: The bed, X by Y — what the preview draws.
    size: tuple[float, float]
    #: The Z limit.
    height: float
    #: Where every extruder reaches — what the send refuses a model for overflowing,
    #: so what the fit warning checks X and Y against.
    usable: PlateArea


class PlateCatalogue(BaseModel):
    #: What an unknown or unchosen printer gets, after the ``default_plate`` setting.
    default: PlateView
    plates: list[PlateView]


def _view(plate: PlateGeometry) -> PlateView:
    name = plate.model.removeprefix(_LAB_PREFIX) if plate.model else "Default plate"
    usable = plate.usable
    return PlateView(
        model=plate.model,
        name=name,
        size=plate.size,
        height=plate.height,
        usable=PlateArea(
            min_x=usable.min_x, min_y=usable.min_y, max_x=usable.max_x, max_y=usable.max_y
        ),
    )


def _resolve(model: str | None, default_plate: str | None) -> PlateGeometry:
    plate = plate_for(model)
    return plate if plate.model is not None else plate_for(default_plate)


@router.get("/plate", response_model=PlateView, summary="The plate for a printer model")
def get_plate(
    store: SettingsStoreDep,
    model: Annotated[
        str | None,
        Query(description='Bambuddy\'s printer model ("H2C") or a profile name'),
    ] = None,
) -> PlateView:
    """An absent or unknown model is the configured default plate, not an error: that is
    what a customizer with no printer chosen, or no Bambuddy at all, draws."""
    return _view(_resolve(model, store.load().default_plate))


@router.get("/plates", response_model=PlateCatalogue, summary="Every plate ScadBuddy knows")
def list_plates(store: SettingsStoreDep) -> PlateCatalogue:
    return PlateCatalogue(
        default=_view(_resolve(None, store.load().default_plate)),
        plates=[_view(plate_for(model)) for model in PLATE_PROFILES],
    )
