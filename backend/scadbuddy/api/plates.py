"""The plate the preview draws and checks the model against (#81).

Served from :mod:`scadbuddy.render.plate` — the same geometry the 3MF writer lays
the model out on — so the browser never keeps a second table of build volumes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.render.plate import Axis, PlateGeometry, fit_problem, overshoots, plate_for
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
    #: Where every extruder reaches.
    usable: PlateArea


class Overshoot(BaseModel):
    axis: Axis
    size: float
    limit: float


class PlateFit(BaseModel):
    """Whether a model of a given size can be sent to a printer, judged by the code the
    send itself runs, so the customizer never says "fits" to a model the send refuses."""

    plate: PlateView
    #: Each axis the bounding box overflows: X and Y against where every extruder
    #: reaches, Z against the printable height.
    overshoots: list[Overshoot]
    #: What ``place_on_plate`` refuses a box that does fit those for — no room for the
    #: prime tower, or no way round the filament cutter. ``None`` when it would place it.
    problem: str | None = None


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


@router.get("/plate/fit", response_model=PlateFit, summary="Whether a model fits a printer")
def get_plate_fit(
    store: SettingsStoreDep,
    x: Annotated[float, Query(ge=0, description="Bounding box width, mm")],
    y: Annotated[float, Query(ge=0, description="Bounding box depth, mm")],
    z: Annotated[float, Query(ge=0, description="Bounding box height, mm")],
    model: Annotated[str | None, Query(description="As for GET /plate")] = None,
    colours: Annotated[
        int, Query(ge=1, description="More than one needs a prime tower, as at send time")
    ] = 1,
) -> PlateFit:
    plate = _resolve(model, store.load().default_plate)
    size = (x, y, z)
    over = [
        Overshoot(axis=axis, size=want, limit=have) for axis, want, have in overshoots(size, plate)
    ]
    # An axis overflow is already the more useful answer; the placement would only say
    # the same thing less precisely.
    problem = None if over else fit_problem(size, plate, tower=colours > 1)
    return PlateFit(plate=_view(plate), overshoots=over, problem=problem)


@router.get("/plates", response_model=PlateCatalogue, summary="Every plate ScadBuddy knows")
def list_plates(store: SettingsStoreDep) -> PlateCatalogue:
    return PlateCatalogue(
        default=_view(_resolve(None, store.load().default_plate)),
        plates=[_view(plate_for(model)) for model in PLATE_PROFILES],
    )
