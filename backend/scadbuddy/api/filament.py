"""`/api/v1/filament` (#6261, epic #6260): ScadBuddy's own filament inventory. This
module holds the catalogs a spool type or a spool picks from; plain reads and writes
over `filament/catalog.py`. Nothing here reaches Bambuddy."""

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
        if isinstance(exc, CatalogExistsError)
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
