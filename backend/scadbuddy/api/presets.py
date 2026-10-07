"""Per-template presets: named parameter sets to start a customization from."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Path, Response, status
from pydantic import ValidationError

from scadbuddy.api.deps import (
    AssetsDep,
    CatalogueDep,
    ConfigDep,
    EventsDep,
    FetcherDep,
    FontsDep,
    HistoryDep,
    PathsDep,
    PresetsDep,
    SlugPath,
)
from scadbuddy.api.models import require_model_exists
from scadbuddy.api.params import require_valid_presets
from scadbuddy.core.config import Config
from scadbuddy.core.events import EventBus, PresetsChanged, emit
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import CheckoutFetcher
from scadbuddy.library.presets import (
    MAX_PRESET_DESCRIPTION,
    MAX_PRESET_NAME,
    MAX_PRESET_TAG,
    MAX_PRESET_TAGS,
    TEMPLATE_ID_PREFIX,
    ParamPreset,
    ParamPresetCreate,
    ParamPresetDuplicate,
    ParamPresetUpdate,
    PresetExistsError,
    PresetNotFoundError,
    PresetStore,
    TooManyPresetsError,
)
from scadbuddy.library.slugs import MAX_SLUG_LENGTH
from scadbuddy.render.inputs import InputsError, legacy_inputs
from scadbuddy.render.schema import ParamValue

router = APIRouter(tags=["presets"])

#: A saved preset's 32 hex digits, or ``template-`` plus a template preset's key.
PresetIdPath = Annotated[
    str, Path(pattern=rf"^[a-z0-9-]{{1,{len(TEMPLATE_ID_PREFIX) + MAX_SLUG_LENGTH}}}$")
]


async def _require_valid(
    slug: str,
    params: dict[str, ParamValue],
    *,
    paths: DataPaths,
    history: ModelHistory,
    config: Config,
    assets: AssetStore,
    fetcher: CheckoutFetcher,
    fonts: FontService,
) -> None:
    """422 unless ``params`` would render the template as it is now -- the same check
    a render makes, so a preset saved here never fails the render it is applied to."""
    await require_valid_presets(
        slug,
        [params],
        paths=paths,
        history=history,
        config=config,
        assets=assets,
        fetcher=fetcher,
        fonts=fonts,
    )


# #357: problems are shown as they are, so they are written for people: no template
# or preset ids, and no Python quoting. The ids stay in the problem's own fields.


def invalid_copy_detail(error: InputsError | ValidationError) -> str:
    """Every problem with a copy that failed validation, not only the first (#1274)."""
    if isinstance(error, ValidationError):
        return "; ".join(str(e["msg"]) for e in error.errors())
    return str(error)


def _taken(error: PresetExistsError) -> ApiError:
    """Names the preset that has the name, as it is spelled, not the spelling asked for."""
    (name,) = error.args
    return ApiError(
        status.HTTP_409_CONFLICT,
        f'A preset named "{error.existing}" already exists.',
        name=name,
        existing=error.existing,
    )


def _missing(preset_id: str) -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND,
        "That preset no longer exists; it may have been deleted elsewhere.",
        preset_id=preset_id,
    )


def _require_saved(presets: PresetStore, slug: str, preset_id: str) -> None:
    """403 for a preset the template ships; 404 for a `template-*` id it does not have."""
    if not preset_id.startswith(TEMPLATE_ID_PREFIX):
        return
    try:
        presets.find(slug, preset_id)
    except PresetNotFoundError:
        raise _missing(preset_id) from None
    raise ApiError(
        status.HTTP_403_FORBIDDEN,
        "This preset ships with the template and is read-only; duplicate it to change it.",
        preset_id=preset_id,
    )


def _changed(events: EventBus, slug: str) -> None:
    """Tells another tab, or the assistant's page, to read the presets again (#357)."""
    emit(events, PresetsChanged(slug=slug))


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
    return presets.presets(slug)


@router.post(
    "/models/{slug}/presets",
    response_model=ParamPreset,
    status_code=status.HTTP_201_CREATED,
    summary="Save a preset",
    description=(
        "Saves a named set of parameter values on any template, built-in or mine. The "
        "values are checked as a render checks them (422 naming an unknown parameter "
        f"or a wrong type). Names are at most {MAX_PRESET_NAME} characters and unique "
        "per template, ignoring case (409). An optional `description` (short Markdown, "
        f"at most {MAX_PRESET_DESCRIPTION} characters) and `tags` (at most "
        f"{MAX_PRESET_TAGS}, each at most {MAX_PRESET_TAG} characters; trimmed, and "
        "each kept once ignoring case) describe it (422 past a limit)."
    ),
)
async def create_preset(
    slug: SlugPath,
    body: ParamPresetCreate,
    catalogue: CatalogueDep,
    presets: PresetsDep,
    events: EventsDep,
    paths: PathsDep,
    assets: AssetsDep,
    history: HistoryDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    fonts: FontsDep,
) -> ParamPreset:
    require_model_exists(catalogue, slug)
    await _require_valid(
        slug,
        body.params,
        paths=paths,
        history=history,
        config=config,
        assets=assets,
        fetcher=fetcher,
        fonts=fonts,
    )
    try:
        created = await asyncio.to_thread(presets.create, slug, body)
    except PresetExistsError as error:
        raise _taken(error) from None
    except TooManyPresetsError as error:
        raise ApiError(status.HTTP_409_CONFLICT, str(error)) from None
    _changed(events, slug)
    return created


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
    events: EventsDep,
    paths: PathsDep,
    assets: AssetsDep,
    history: HistoryDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    fonts: FontsDep,
) -> ParamPreset:
    require_model_exists(catalogue, slug)
    try:
        source = await asyncio.to_thread(presets.find, slug, preset_id)
    except PresetNotFoundError:
        raise _missing(preset_id) from None
    await _require_valid(
        slug,
        source.params,
        paths=paths,
        history=history,
        config=config,
        assets=assets,
        fetcher=fetcher,
        fonts=fonts,
    )
    # The original's details come along too (#327): only the name is the copy's own.
    # Every write path checks inputs before storing them, so this should not fail; a
    # stored value that slipped past those checks is a 422 here, never a 500.
    try:
        copy = ParamPresetCreate(
            name=body.name,
            inputs=source.inputs or legacy_inputs(source.params),
            description=source.description,
            tags=source.tags,
        )
    except (InputsError, ValidationError) as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, invalid_copy_detail(error)) from None
    try:
        created = await asyncio.to_thread(presets.create, slug, copy)
    except PresetExistsError as error:
        raise _taken(error) from None
    except TooManyPresetsError as error:
        raise ApiError(status.HTTP_409_CONFLICT, str(error)) from None
    _changed(events, slug)
    return created


@router.patch(
    "/models/{slug}/presets/{preset_id}",
    response_model=ParamPreset,
    summary="Change a preset's name, values or details",
    description=(
        "Each field given replaces the old one whole: `params` its values, "
        "`description` and `tags` its details (an empty string or list clears them). "
        "A template's own presets are read-only (403)."
    ),
)
async def update_preset(
    slug: SlugPath,
    preset_id: PresetIdPath,
    body: ParamPresetUpdate,
    catalogue: CatalogueDep,
    presets: PresetsDep,
    events: EventsDep,
    paths: PathsDep,
    assets: AssetsDep,
    history: HistoryDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    fonts: FontsDep,
) -> ParamPreset:
    require_model_exists(catalogue, slug)
    await asyncio.to_thread(_require_saved, presets, slug, preset_id)
    if body.params is not None:
        await _require_valid(
            slug,
            body.params,
            paths=paths,
            history=history,
            config=config,
            assets=assets,
            fetcher=fetcher,
            fonts=fonts,
        )
    try:
        updated = await asyncio.to_thread(presets.update, slug, preset_id, body)
    except InputsError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except PresetNotFoundError:
        raise _missing(preset_id) from None
    except PresetExistsError as error:
        raise _taken(error) from None
    _changed(events, slug)
    return updated


@router.delete(
    "/models/{slug}/presets/{preset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a preset",
    description="A template's own presets are read-only (403).",
)
def delete_preset(
    slug: SlugPath,
    preset_id: PresetIdPath,
    catalogue: CatalogueDep,
    presets: PresetsDep,
    events: EventsDep,
) -> Response:
    require_model_exists(catalogue, slug)
    _require_saved(presets, slug, preset_id)
    try:
        presets.delete(slug, preset_id)
    except PresetNotFoundError:
        raise _missing(preset_id) from None
    _changed(events, slug)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
