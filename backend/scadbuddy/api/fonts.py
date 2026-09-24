from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from scadbuddy.api.deps import FontsDep
from scadbuddy.core.problems import ApiError
from scadbuddy.library.fonts import FontFamily, FontNotFoundError, InstalledFamily
from scadbuddy.library.googlefonts import CatalogueSource, FontVariant, GoogleFontsError

router = APIRouter(tags=["fonts"])

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 60
MAX_LIMIT = 500


class CatalogueEntry(BaseModel):
    family: str
    category: str = ""
    variants: list[FontVariant] = Field(default_factory=list)
    popularity: int | None = None
    installed: bool = False


class FontCatalogueView(BaseModel):
    """``source`` says which of the two catalogue endpoints answered — the keyed
    Developer API or the keyless fonts.google.com metadata."""

    source: CatalogueSource
    fetched_at: datetime
    total: int
    fonts: list[CatalogueEntry] = Field(default_factory=list)


class InstallRequest(BaseModel):
    family: str = Field(min_length=1, max_length=100)
    # Re-download a family that is already resolvable, e.g. after a partial install.
    force: bool = False


@router.get("/fonts", response_model=list[FontFamily], summary="Installed font families")
def get_fonts(fonts: FontsDep) -> list[FontFamily]:
    return fonts.installed()


@router.get(
    "/fonts/catalogue",
    response_model=FontCatalogueView,
    summary="Search the Google Fonts catalogue",
)
async def get_catalogue(
    fonts: FontsDep,
    q: Annotated[str, Query(max_length=100)] = "",
    category: Annotated[str, Query(max_length=40)] = "",
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> FontCatalogueView:
    try:
        catalogue = await fonts.catalogue()
    except GoogleFontsError as exc:
        # 503, not 502: the picker treats this as "browse what is installed instead",
        # which is exactly the air-gapped case.
        raise ApiError(503, f"the Google Fonts catalogue is unavailable: {exc}") from exc

    installed = fonts.installed_families()
    needle = q.strip().casefold()
    wanted = category.strip().casefold()
    matched = [
        font
        for font in catalogue.fonts
        if (not needle or needle in font.family.casefold())
        and (not wanted or font.category.casefold() == wanted)
    ]
    if needle:
        # A prefix match is what the typist meant; popularity breaks the rest of the tie.
        matched.sort(
            key=lambda font: (
                not font.family.casefold().startswith(needle),
                font.popularity if font.popularity is not None else len(catalogue.fonts),
            )
        )
    return FontCatalogueView(
        source=catalogue.source,
        fetched_at=catalogue.fetched_at,
        total=len(matched),
        fonts=[
            CatalogueEntry(
                family=font.family,
                category=font.category,
                variants=font.variants,
                popularity=font.popularity,
                installed=font.family.casefold() in installed,
            )
            for font in matched[:limit]
        ],
    )


@router.post(
    "/fonts/install",
    response_model=InstalledFamily,
    summary="Install a family onto the data volume",
)
async def install_font(body: InstallRequest, fonts: FontsDep) -> InstalledFamily:
    try:
        return await fonts.install(body.family, force=body.force)
    except FontNotFoundError as exc:
        raise ApiError(404, f"{body.family!r} is not in the Google Fonts catalogue") from exc
    except GoogleFontsError as exc:
        raise ApiError(502, f"{body.family!r} could not be downloaded: {exc}") from exc
