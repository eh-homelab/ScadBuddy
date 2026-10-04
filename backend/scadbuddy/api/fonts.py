from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from scadbuddy.api.deps import AppState, FontsDep
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.core.events import FontInstalled, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.fonts import (
    FontFamily,
    FontNotFoundError,
    FontNotResolvedError,
    InstalledFamily,
)
from scadbuddy.library.googlefonts import CatalogueSource, FontVariant, GoogleFontsError
from scadbuddy.operations.component import OperationsDep

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

    # fc-list shells out; off the loop so a browse cannot stall other requests.
    installed = await asyncio.to_thread(fonts.installed_families)
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
    description=(
        "Downloads every face of the family, rebuilds fontconfig's cache and then checks "
        "that fontconfig resolves the family under the environment renders run in. A 500 "
        "when it does not: the files are on the volume, but a render naming the family "
        "would silently fall back to the default font (#253)."
    ),
    responses=OPERATION_RESPONSES,
)
async def install_font(
    body: InstallRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> InstalledFamily | JSONResponse:
    """The ``font_install`` operation (#1054): a second press of the same install, with
    the same key, joins the first rather than downloading again."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["font_install"],
        subject=body.family.casefold(),
        request=body,
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, InstalledFamily)


async def install_run(family: str, force: bool, state: AppState) -> InstalledFamily:
    """The ``font_install`` operation's run (#1054)."""
    try:
        installed = await state.fonts.install(family, force=force)
    except FontNotFoundError as exc:
        raise ApiError(404, f"{family!r} is not in the Google Fonts catalogue") from exc
    except GoogleFontsError as exc:
        raise ApiError(502, f"{family!r} could not be downloaded: {exc}") from exc
    except FontNotResolvedError as exc:
        # Nothing to emit: fontconfig, and so every render, sees no new family.
        raise ApiError(
            500,
            f"{exc.family!r} was downloaded ({', '.join(exc.files)}), but fontconfig does "
            "not resolve that family afterwards, so a render naming it would fall back to "
            "the default font; its files may name another family, see GET /fonts. A repeat "
            "install answers this again without a download; force=true fetches it anew",
            family=exc.family,
            files=exc.files,
        ) from exc
    emit(state.events, FontInstalled(family=installed.family))
    if state.store.fonts is not None:
        try:
            await state.store.fonts.publish(family)
        except Exception:
            # The family is installed here; the next boot's `backfill` publishes it, so
            # a Bambuddy error does not fail an install that worked.
            logger.exception("could not publish an installed font family", extra={"family": family})
    return installed
