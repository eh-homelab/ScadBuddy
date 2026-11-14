"""`/api/v1/filament` (#6261, epic #6260): ScadBuddy's own filament inventory. This
module holds the catalogs a spool type or a spool picks from (plain reads and writes
over `filament/catalog.py`) and the spool types (`filament/types.py`). Nothing here
reaches Bambuddy."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Body, status
from pydantic import BaseModel, ValidationError

from scadbuddy.core.problems import ApiError
from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import (
    CATALOGS,
    CatalogExistsError,
    CatalogRefError,
    CatalogSpec,
)
from scadbuddy.filament.component import FilamentDep
from scadbuddy.filament.types import TypeRowsDuplicateError, TypeTempError
from scadbuddy.orders.store import FilamentExistsError

router = APIRouter(prefix="/filament", tags=["filament"])

CatalogRow = (
    m.Material
    | m.Subtype
    | m.Brand
    | m.Category
    | m.Effect
    | m.InsertMaterial
    | m.InsertType
    | m.Colour
    | m.LocationKind
)

_KINDS = ", ".join(CATALOGS)


def _spec(kind: str) -> CatalogSpec:
    spec = CATALOGS.get(kind)
    if spec is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no catalog {kind!r}; the kinds are {_KINDS}")
    return spec


def _body(model: type[BaseModel], body: dict[str, Any]) -> BaseModel:
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'body'}: {e['msg']}" for e in exc.errors()
        )
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, problems) from None


def _row(row: BaseModel | None, kind: str, row_id: UUID) -> CatalogRow:
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no {kind} row {row_id}")
    return cast("CatalogRow", row)


def _refused(exc: ValueError) -> ApiError:
    code = (
        status.HTTP_409_CONFLICT
        if isinstance(exc, CatalogExistsError | FilamentExistsError)
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return ApiError(code, str(exc))


@router.get(
    "/catalog/{kind}",
    response_model=list[CatalogRow],
    summary="List one filament catalog",
    description=(
        "A catalog a spool type or spool picks from, by kind: `materials`, `subtypes`, "
        "`brands`, `categories`, `effects`, `insert-materials`, `insert-types`, `colours` "
        "or `location-kinds`. In sort order; archived rows only with `include_archived`. "
        "404 for an unknown kind."
    ),
)
async def list_catalog(
    filament: FilamentDep, kind: str, include_archived: bool = False
) -> list[CatalogRow]:
    _spec(kind)
    rows = await asyncio.to_thread(filament.catalog.list, kind, include_archived=include_archived)
    return cast("list[CatalogRow]", rows)


@router.post(
    "/catalog/{kind}",
    response_model=CatalogRow,
    status_code=status.HTTP_201_CREATED,
    summary="Add a row to a filament catalog",
    description=(
        "The fields depend on the kind (every one takes `name` and `sort_order`; materials "
        "add print and drying defaults and handling flags, colours an `rgba`). 404 for an "
        "unknown kind, 409 when an unarchived row has the name, 422 for a bad field or a "
        "row it points at that does not exist."
    ),
)
async def create_catalog_row(
    filament: FilamentDep, kind: str, body: Annotated[dict[str, Any], Body()]
) -> CatalogRow:
    spec = _spec(kind)
    parsed = _body(spec.create, body)
    try:
        made = await asyncio.to_thread(filament.catalog.create, kind, parsed)
    except (CatalogExistsError, CatalogRefError) as exc:
        raise _refused(exc) from None
    return _row(made, kind, UUID(int=0))


@router.patch(
    "/catalog/{kind}/{row_id}",
    response_model=CatalogRow,
    summary="Change a row of a filament catalog",
    description=(
        "Only the fields sent change; `null` clears a nullable one. `archived: true` takes "
        "the row off the lists and frees its name, `false` puts it back. 404 for an unknown "
        "kind or row, 409 when another unarchived row has the name, 422 for a bad field."
    ),
)
async def patch_catalog_row(
    filament: FilamentDep, kind: str, row_id: UUID, body: Annotated[dict[str, Any], Body()]
) -> CatalogRow:
    spec = _spec(kind)
    parsed = _body(spec.patch, body)
    try:
        changed = await asyncio.to_thread(filament.catalog.update, kind, row_id, parsed)
    except (CatalogExistsError, CatalogRefError) as exc:
        raise _refused(exc) from None
    return _row(changed, kind, row_id)


# -- spool types ----------------------------------------------------------------------

_TYPE_REFUSED = (
    FilamentExistsError,
    CatalogExistsError,
    CatalogRefError,
    TypeTempError,
    TypeRowsDuplicateError,
)


def _no_type(type_id: UUID) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no spool type {type_id}")


async def _detail(filament: FilamentDep, made: m.SpoolType) -> m.SpoolTypeDetail:
    presets = await asyncio.to_thread(filament.types.presets, made.id)
    k_values = await asyncio.to_thread(filament.types.k_values, made.id)
    return m.SpoolTypeDetail(**made.model_dump(), presets=presets, k_values=k_values)


@router.get(
    "/types",
    response_model=list[m.SpoolType],
    summary="List spool types",
    description=(
        "The spool types (a product: what a maker sells under one code) by code. Archived "
        "types only with `include_archived`."
    ),
)
async def list_types(filament: FilamentDep, include_archived: bool = False) -> list[m.SpoolType]:
    return await asyncio.to_thread(filament.types.list, include_archived=include_archived)


@router.post(
    "/types",
    response_model=m.SpoolType,
    status_code=status.HTTP_201_CREATED,
    summary="Add a spool type",
    description=(
        "A type has a `code`, a material, an `rgba` (`#RRGGBB` or `#RRGGBBAA`, stored with "
        "its alpha), a `label_weight_g` and optional print values a spool inherits. With a "
        "`cost_per_kg` it also writes the price book row of its material, subtype, brand "
        "and colour (`filament_id`); two types that differ only in alpha share one row "
        "(#6209). 409 when an unarchived type has the code, 422 for a bad field, a row it "
        "points at that does not exist, or a minimum temperature above its maximum."
    ),
)
async def create_type(filament: FilamentDep, body: m.SpoolTypeCreate) -> m.SpoolType:
    try:
        return await asyncio.to_thread(filament.types.create, body)
    except _TYPE_REFUSED as exc:
        raise _refused(exc) from None


@router.get(
    "/types/{type_id}",
    response_model=m.SpoolTypeDetail,
    summary="Read a spool type",
    description=(
        "One type (archived too) with its slicer presets and pressure-advance values. 404 for none."
    ),
)
async def get_type(filament: FilamentDep, type_id: UUID) -> m.SpoolTypeDetail:
    found = await asyncio.to_thread(filament.types.get, type_id)
    if found is None:
        raise _no_type(type_id)
    return await _detail(filament, found)


@router.patch(
    "/types/{type_id}",
    response_model=m.SpoolType,
    summary="Change a spool type",
    description=(
        "Only the fields sent change; `null` clears a nullable one. `archived: true` takes "
        "the type off the lists and frees its code, `false` puts it back. A priced type "
        "keeps its price book row in step. 404 for no such type, 409 when another "
        "unarchived type has the code, 422 for a bad field."
    ),
)
async def patch_type(filament: FilamentDep, type_id: UUID, body: m.SpoolTypePatch) -> m.SpoolType:
    try:
        changed = await asyncio.to_thread(filament.types.update, type_id, body)
    except _TYPE_REFUSED as exc:
        raise _refused(exc) from None
    if changed is None:
        raise _no_type(type_id)
    return changed


@router.put(
    "/types/{type_id}/presets",
    response_model=list[m.TypePreset],
    summary="Replace a spool type's slicer presets",
    description=(
        "The whole list: one slicer filament per printer model and nozzle diameter. 404 "
        "for no such type, 422 for two rows with the same model and nozzle."
    ),
)
async def put_type_presets(
    filament: FilamentDep, type_id: UUID, body: list[m.TypePresetCreate]
) -> list[m.TypePreset]:
    rows = [m.TypePreset(type_id=type_id, **one.model_dump()) for one in body]
    try:
        saved = await asyncio.to_thread(filament.types.set_presets, type_id, rows)
    except _TYPE_REFUSED as exc:
        raise _refused(exc) from None
    if saved is None:
        raise _no_type(type_id)
    return saved


@router.put(
    "/types/{type_id}/k-values",
    response_model=list[m.TypeKValue],
    summary="Replace a spool type's pressure-advance values",
    description=(
        "The whole list: one K value per printer, extruder and nozzle diameter. 404 for "
        "no such type, 422 for a printer that does not exist or a repeated key."
    ),
)
async def put_type_k_values(
    filament: FilamentDep, type_id: UUID, body: list[m.TypeKValueCreate]
) -> list[m.TypeKValue]:
    rows = [m.TypeKValue(type_id=type_id, **one.model_dump()) for one in body]
    try:
        saved = await asyncio.to_thread(filament.types.set_k_values, type_id, rows)
    except _TYPE_REFUSED as exc:
        raise _refused(exc) from None
    if saved is None:
        raise _no_type(type_id)
    return saved
