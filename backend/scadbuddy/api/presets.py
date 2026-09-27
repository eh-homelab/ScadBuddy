"""Per-template presets: named parameter sets to start a customization from."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Path, Response, status

from scadbuddy.api.deps import (
    CatalogueDep,
    ConfigDep,
    HistoryDep,
    PathsDep,
    PresetsDep,
    SlugPath,
)
from scadbuddy.api.jobs import require_valid_params, schema_of
from scadbuddy.api.models import require_model_exists
from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.presets import (
    MAX_PRESET_NAME,
    TEMPLATE_ID_PREFIX,
    InvalidPresetsFileError,
    ParamPreset,
    ParamPresetCreate,
    ParamPresetDuplicate,
    ParamPresetUpdate,
    PresetExistsError,
    PresetNotFoundError,
    TooManyPresetsError,
)
from scadbuddy.render.schema import ParamValue

router = APIRouter(tags=["presets"])

PresetIdPath = Annotated[str, Path(pattern=r"^[a-z0-9-]{1,64}$")]


async def _require_valid(
    slug: str,
    params: dict[str, ParamValue],
    *,
    paths: DataPaths,
    history: ModelHistory,
    config: Config,
) -> None:
    """422 unless ``params`` would render the template as it is now -- the same check
    a render makes, so a preset saved here never fails the render it is applied to."""
    _, schema = await schema_of(slug, None, paths=paths, history=history, config=config)
    require_valid_params(schema, params)
    # Stricter than a render, which takes any value of the right type: a preset is
    # kept and replayed, so it holds only what the dropdown itself could pick.
    by_name = {parameter.name: parameter for parameter in schema.parameters}
    for name, value in params.items():
        options = [option.value for option in by_name[name].options]
        if options and value not in options:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{value!r} is not one of the options of {name!r}",
                parameters=[name],
            )


def _unreadable(error: InvalidPresetsFileError) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, str(error))


def _taken(slug: str, name: str) -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT, f"{slug!r} already has a preset named {name!r}", name=name
    )


def _missing(slug: str, preset_id: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"{slug!r} has no preset {preset_id!r}")


def _require_saved(slug: str, preset_id: str) -> None:
    if preset_id.startswith(TEMPLATE_ID_PREFIX):
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            f"{preset_id!r} ships with {slug!r} and is read-only; duplicate it to change it",
        )


@router.get(
    "/models/{slug}/presets",
    response_model=list[ParamPreset],
    summary="A template's presets",
    description=(
        "The presets the template ships with (`origin: template`, read-only), then the "
        "ones saved on it (`origin: mine`). A preset holds only the values it sets: "
        "apply it over the template's defaults. A value for a parameter the template "
        "no longer has is kept here and is the client's to skip."
    ),
)
def list_presets(slug: SlugPath, catalogue: CatalogueDep, presets: PresetsDep) -> list[ParamPreset]:
    require_model_exists(catalogue, slug)
    try:
        return presets.presets(slug)
    except InvalidPresetsFileError as error:
        raise _unreadable(error) from None


@router.post(
    "/models/{slug}/presets",
    response_model=ParamPreset,
    status_code=status.HTTP_201_CREATED,
    summary="Save a preset",
    description=(
        "Saves a named set of parameter values on any template, built-in or mine. The "
        "values are checked as a render checks them (422 naming an unknown parameter "
        f"or a wrong type). Names are at most {MAX_PRESET_NAME} characters and unique "
        "per template, ignoring case (409)."
    ),
)
async def create_preset(
    slug: SlugPath,
    body: ParamPresetCreate,
    catalogue: CatalogueDep,
    presets: PresetsDep,
    paths: PathsDep,
    history: HistoryDep,
    config: ConfigDep,
) -> ParamPreset:
    require_model_exists(catalogue, slug)
    await _require_valid(slug, body.params, paths=paths, history=history, config=config)
    try:
        return await asyncio.to_thread(presets.create, slug, body)
    except PresetExistsError:
        raise _taken(slug, body.name) from None
    except TooManyPresetsError as error:
        raise ApiError(status.HTTP_409_CONFLICT, str(error)) from None
    except InvalidPresetsFileError as error:
        raise _unreadable(error) from None


@router.post(
    "/models/{slug}/presets/{preset_id}/duplicate",
    response_model=ParamPreset,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate a preset",
    description=(
        "Copies any preset of the template, shipped or saved, to a new saved preset "
        "under `name`, with the original's values: the way to change a shipped preset, "
        "which is read-only. The values are checked as a save checks them (422), so a "
        "shipped preset naming a parameter the template has since dropped cannot be "
        "copied as it is. Names are unique per template, ignoring case (409)."
    ),
)
async def duplicate_preset(
    slug: SlugPath,
    preset_id: PresetIdPath,
    body: ParamPresetDuplicate,
    catalogue: CatalogueDep,
    presets: PresetsDep,
    paths: PathsDep,
    history: HistoryDep,
    config: ConfigDep,
) -> ParamPreset:
    require_model_exists(catalogue, slug)
    try:
        source = await asyncio.to_thread(presets.find, slug, preset_id)
    except PresetNotFoundError:
        raise _missing(slug, preset_id) from None
    except InvalidPresetsFileError as error:
        raise _unreadable(error) from None
    await _require_valid(slug, source.params, paths=paths, history=history, config=config)
    copy = ParamPresetCreate(name=body.name, params=source.params)
    try:
        return await asyncio.to_thread(presets.create, slug, copy)
    except PresetExistsError:
        raise _taken(slug, body.name) from None
    except TooManyPresetsError as error:
        raise ApiError(status.HTTP_409_CONFLICT, str(error)) from None
    except InvalidPresetsFileError as error:
        raise _unreadable(error) from None


@router.patch(
    "/models/{slug}/presets/{preset_id}",
    response_model=ParamPreset,
    summary="Rename a preset or replace its values",
    description=(
        "`params`, when given, replaces the preset's values whole. A template's own "
        "presets are read-only (403)."
    ),
)
async def update_preset(
    slug: SlugPath,
    preset_id: PresetIdPath,
    body: ParamPresetUpdate,
    catalogue: CatalogueDep,
    presets: PresetsDep,
    paths: PathsDep,
    history: HistoryDep,
    config: ConfigDep,
) -> ParamPreset:
    require_model_exists(catalogue, slug)
    _require_saved(slug, preset_id)
    if body.params is not None:
        await _require_valid(slug, body.params, paths=paths, history=history, config=config)
    try:
        return await asyncio.to_thread(presets.update, slug, preset_id, body)
    except PresetNotFoundError:
        raise _missing(slug, preset_id) from None
    except PresetExistsError:
        raise _taken(slug, body.name or "") from None
    except InvalidPresetsFileError as error:
        raise _unreadable(error) from None


@router.delete(
    "/models/{slug}/presets/{preset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a preset",
    description="A template's own presets are read-only (403).",
)
def delete_preset(
    slug: SlugPath, preset_id: PresetIdPath, catalogue: CatalogueDep, presets: PresetsDep
) -> Response:
    require_model_exists(catalogue, slug)
    _require_saved(slug, preset_id)
    try:
        presets.delete(slug, preset_id)
    except PresetNotFoundError:
        raise _missing(slug, preset_id) from None
    except InvalidPresetsFileError as error:
        raise _unreadable(error) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
