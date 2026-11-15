"""`/api/v1/filament` (#6261, epic #6260): ScadBuddy's own filament inventory. This
module holds the catalogs a spool type or a spool picks from (plain reads and writes
over `filament/catalog.py`), the spool types (`filament/types.py`), and the spools,
their tags and their ledger (`filament/spools.py`, `filament/ledger.py`). Only the import
(`POST /filament/bambuddy/import`, `filament/bambuddy_pull.py`) reaches Bambuddy, and it
only reads."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Body, Response, status
from pydantic import BaseModel, ValidationError

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.auth.component import CallerDep
from scadbuddy.auth.models import Caller, Person, PersonalToken, Service
from scadbuddy.core.problems import ApiError
from scadbuddy.filament import models as m
from scadbuddy.filament.bambuddy_pull import PullReport, read_and_pull
from scadbuddy.filament.catalog import (
    CATALOGS,
    CatalogExistsError,
    CatalogRefError,
    CatalogSpec,
)
from scadbuddy.filament.component import FilamentDep
from scadbuddy.filament.locations import (
    LocationCycleError,
    LocationExistsError,
    MoveTargetError,
)
from scadbuddy.filament.spools import (
    SpoolBambuddyLinkedError,
    SpoolInUseError,
    SpoolTypelessError,
    TagKeysClashError,
    TagUnnamedError,
)
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
        if isinstance(
            exc,
            CatalogExistsError
            | FilamentExistsError
            | SpoolInUseError
            | SpoolBambuddyLinkedError
            | TagKeysClashError
            | LocationExistsError
            | LocationCycleError,
        )
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
        "unknown kind, 409 when an unarchived row has the name, 422 for a bad field, a "
        "row it points at that does not exist, or a minimum temperature above its maximum."
    ),
)
async def create_catalog_row(
    filament: FilamentDep, kind: str, body: Annotated[dict[str, Any], Body()]
) -> CatalogRow:
    spec = _spec(kind)
    parsed = _body(spec.create, body)
    try:
        made = await asyncio.to_thread(filament.catalog.create, kind, parsed)
    except (CatalogExistsError, CatalogRefError, TypeTempError) as exc:
        raise _refused(exc) from None
    return _row(made, kind, UUID(int=0))


@router.patch(
    "/catalog/{kind}/{row_id}",
    response_model=CatalogRow,
    summary="Change a row of a filament catalog",
    description=(
        "Only the fields sent change; `null` clears a nullable one. `archived: true` takes "
        "the row off the lists and frees its name, `false` puts it back. 404 for an unknown "
        "kind or row, 409 when another unarchived row has the name, 422 for a bad field or a "
        "minimum temperature above its maximum."
    ),
)
async def patch_catalog_row(
    filament: FilamentDep, kind: str, row_id: UUID, body: Annotated[dict[str, Any], Body()]
) -> CatalogRow:
    spec = _spec(kind)
    parsed = _body(spec.patch, body)
    try:
        changed = await asyncio.to_thread(filament.catalog.update, kind, row_id, parsed)
    except (CatalogExistsError, CatalogRefError, TypeTempError) as exc:
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
        "`cost_per_kg` it also writes the price book row of its material, subtype, brand, "
        "colour and alpha (`filament_id`), which takes its stops and effect when it is new "
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


# -- spools, tags, the ledger ---------------------------------------------------------

_SPOOL_REFUSED = (
    CatalogRefError,
    TypeTempError,
    SpoolTypelessError,
    SpoolInUseError,
    SpoolBambuddyLinkedError,
    TagUnnamedError,
    TagKeysClashError,
)


def actor(caller: Caller) -> str:
    """Who a ledger event names: a person by their principal, the same through a session
    or a token of theirs (a name or email can change, and would split one person in
    two); a service by its name."""
    if isinstance(caller, Person | PersonalToken):
        return caller.principal
    if isinstance(caller, Service):
        return f"service:{caller.caller}"
    return "anonymous"


def _no_spool(spool_id: UUID) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no spool {spool_id}")


@router.get(
    "/spools",
    response_model=list[m.Spool],
    summary="List spools",
    description=(
        "The spools by number, each with its `effective` values (its own override, else its "
        "type's, else its subtype's and material's print defaults) and the names of the "
        "fields it `overridden`. `needs_type=true` keeps only spools with no type yet, "
        "`false` only typed ones. Archived spools only with `include_archived`."
    ),
)
async def list_spools(
    filament: FilamentDep, include_archived: bool = False, needs_type: bool | None = None
) -> list[m.Spool]:
    return await asyncio.to_thread(
        filament.spools.list, include_archived=include_archived, needs_type=needs_type
    )


@router.post(
    "/spools",
    response_model=m.Spool,
    status_code=status.HTTP_201_CREATED,
    summary="Add a spool",
    description=(
        "A spool names a `type_id`, or, while it needs a type, its own `material_id`. Every "
        "other value overrides the type's. `grams_left` is the first reading on its ledger. "
        "422 for a bad field, a row it points at that does not exist, or an effective "
        "minimum temperature above its maximum."
    ),
)
async def create_spool(filament: FilamentDep, caller: CallerDep, body: m.SpoolCreate) -> m.Spool:
    try:
        return await asyncio.to_thread(filament.spools.create, body, actor=actor(caller))
    except _SPOOL_REFUSED as exc:
        raise _refused(exc) from None


@router.get(
    "/spools/{spool_id}",
    response_model=m.Spool,
    summary="Read a spool",
    description="One spool (archived too) with its effective values. 404 for none.",
)
async def get_spool(filament: FilamentDep, spool_id: UUID) -> m.Spool:
    found = await asyncio.to_thread(filament.spools.get, spool_id)
    if found is None:
        raise _no_spool(spool_id)
    return found


@router.patch(
    "/spools/{spool_id}",
    response_model=m.Spool,
    summary="Change a spool",
    description=(
        "Only the fields sent change; `null` on an override clears it, and the spool takes "
        "its type's value again. One `edited` ledger event records each change as `[before, "
        "after]`. `archived` takes the spool off the lists or puts it back. 404 for no such "
        "spool, 422 for a bad field, a patch that leaves neither `type_id` nor "
        "`material_id`, or an effective minimum temperature above its maximum."
    ),
)
async def patch_spool(
    filament: FilamentDep, caller: CallerDep, spool_id: UUID, body: m.SpoolPatch
) -> m.Spool:
    try:
        changed = await asyncio.to_thread(
            filament.spools.update, spool_id, body, actor=actor(caller)
        )
    except _SPOOL_REFUSED as exc:
        raise _refused(exc) from None
    if changed is None:
        raise _no_spool(spool_id)
    return changed


@router.delete(
    "/spools/{spool_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a spool",
    description=(
        "Only a spool that never printed and has no Bambuddy link: its ledger and tag "
        "bindings go with it. 404 for no such spool, 409 when its ledger holds a print or "
        "Bambuddy links it (the next pull would make it again); archive it instead."
    ),
)
async def delete_spool(filament: FilamentDep, spool_id: UUID) -> Response:
    try:
        deleted = await asyncio.to_thread(filament.spools.delete, spool_id)
    except _SPOOL_REFUSED as exc:
        raise _refused(exc) from None
    if not deleted:
        raise _no_spool(spool_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/spools/{spool_id}/archive",
    response_model=m.Spool,
    summary="Archive or restore a spool",
    description=(
        "`archived: true` takes the spool off the lists, `false` puts it back; each writes "
        "an `archived` or `restored` ledger event. 404 for no such spool."
    ),
)
async def archive_spool(
    filament: FilamentDep, caller: CallerDep, spool_id: UUID, body: m.SpoolArchive
) -> m.Spool:
    changed = await asyncio.to_thread(
        filament.spools.archive, spool_id, body.archived, actor=actor(caller)
    )
    if changed is None:
        raise _no_spool(spool_id)
    return changed


@router.post(
    "/spools/{spool_id}/tags",
    response_model=m.TagBinding,
    status_code=status.HTTP_201_CREATED,
    summary="Put a tag on a spool",
    description=(
        "The tag is found by its `tag_uid` or `tray_uuid` (cleaned to hex as a tray reports "
        "them), or made with `kind`. A binding the tag had to another spool, or the spool "
        "had to another tag, is closed, with a `tag_unbound` ledger event on that spool and "
        "`tag_bound` on this one. 404 for no such spool, 409 when the two keys name two "
        "tags, 422 when neither key is left once cleaned."
    ),
)
async def bind_spool_tag(
    filament: FilamentDep, caller: CallerDep, spool_id: UUID, body: m.TagBind
) -> m.TagBinding:
    try:
        bound = await asyncio.to_thread(
            lambda: filament.spools.bind_tag(
                spool_id,
                tag_uid=body.tag_uid,
                tray_uuid=body.tray_uuid,
                kind=body.kind,
                override=body.override,
                actor=actor(caller),
            )
        )
    except _SPOOL_REFUSED as exc:
        raise _refused(exc) from None
    if bound is None:
        raise _no_spool(spool_id)
    return bound


@router.delete(
    "/tags/{tag_id}/binding",
    response_model=m.TagBinding,
    summary="Take a tag off its spool",
    description=(
        "Closes the tag's open binding, with a `tag_unbound` ledger event on its spool, "
        "and answers the closed binding. 404 when the tag is on no spool."
    ),
)
async def unbind_tag(filament: FilamentDep, caller: CallerDep, tag_id: UUID) -> m.TagBinding:
    closed = await asyncio.to_thread(filament.spools.unbind_tag, tag_id, actor=actor(caller))
    if closed is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"tag {tag_id} is on no spool")
    return closed


@router.get(
    "/spools/{spool_id}/events",
    response_model=list[m.SpoolEvent],
    summary="Read a spool's ledger",
    description=(
        "Every event of the spool, oldest first: readings (`scale`, `person`, `ams_remain`, "
        "`bb`) in `grams_after`, print use in `delta_g`. `grams_left` is the newest reading "
        "less the later use. 404 for no such spool."
    ),
)
async def list_spool_events(filament: FilamentDep, spool_id: UUID) -> list[m.SpoolEvent]:
    if await asyncio.to_thread(filament.spools.get, spool_id) is None:
        raise _no_spool(spool_id)
    return await asyncio.to_thread(filament.spools.events, spool_id)


@router.post(
    "/spools/{spool_id}/events",
    response_model=m.SpoolEvent,
    status_code=status.HTTP_201_CREATED,
    summary="Record a weighing or a drying",
    description=(
        "`weighed` with `grams` (the filament left, without the empty spool) is a new "
        "reading: `grams_left` starts again from it. `dried` takes `temp_c` and `hours`. "
        "404 for no such spool, 422 for a field the kind does not take."
    ),
)
async def add_spool_event(
    filament: FilamentDep, caller: CallerDep, spool_id: UUID, body: m.SpoolEventAdd
) -> m.SpoolEvent:
    made = await asyncio.to_thread(filament.spools.add_event, spool_id, body, actor=actor(caller))
    if made is None:
        raise _no_spool(spool_id)
    return made


# -- locations ------------------------------------------------------------------------

_LOCATION_REFUSED = (CatalogRefError, LocationExistsError, LocationCycleError)


@router.get(
    "/locations",
    response_model=list[m.LocationNode],
    summary="The location tree",
    description=(
        "The places spools sit in (room, shelf, bin, AMS tray) nested under their parents, "
        "each level by sort order then name. Each carries `spool_count`, the unarchived "
        "spools in it, and its `children`. Archived places only with `include_archived`; a "
        "place whose parent is left out is then a root."
    ),
)
async def list_locations(
    filament: FilamentDep, include_archived: bool = False
) -> list[m.LocationNode]:
    return await asyncio.to_thread(filament.locations.tree, include_archived=include_archived)


@router.post(
    "/locations",
    response_model=m.Location,
    status_code=status.HTTP_201_CREATED,
    summary="Add a location",
    description=(
        "A place has a `name`, a `kind_id` (the `location-kinds` catalog) and optionally a "
        "`parent_id`. An AMS tray also names its `printer_id`, `ams_id` and `tray_id`. 409 "
        "when an unarchived place of the name is under the parent, or one is already that "
        "tray; 422 for a bad field or a row it points at that does not exist."
    ),
)
async def create_location(filament: FilamentDep, body: m.LocationCreate) -> m.Location:
    try:
        return await asyncio.to_thread(filament.locations.create, body)
    except _LOCATION_REFUSED as exc:
        raise _refused(exc) from None


@router.patch(
    "/locations/{location_id}",
    response_model=m.Location,
    summary="Change a location",
    description=(
        "Only the fields sent change; `null` clears a nullable one. `archived: true` takes "
        "the place off the tree and frees its name, `false` puts it back. 404 for no such "
        "place, 409 for a name or tray taken, or a `parent_id` that would make the place "
        "its own ancestor; 422 for a bad field."
    ),
)
async def patch_location(
    filament: FilamentDep, location_id: UUID, body: m.LocationPatch
) -> m.Location:
    try:
        changed = await asyncio.to_thread(filament.locations.update, location_id, body)
    except _LOCATION_REFUSED as exc:
        raise _refused(exc) from None
    if changed is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no location {location_id}")
    return changed


@router.post(
    "/spools/{spool_id}/move",
    response_model=m.Spool,
    summary="Move a spool",
    description=(
        "Put the spool in a place (`location_id: null`: in none) and write a `moved` ledger "
        "event, `{from, to}`. A spool already there is answered as it is, with no event. "
        "404 for no such spool, 422 for a place that does not exist or is archived."
    ),
)
async def move_spool(
    filament: FilamentDep, caller: CallerDep, spool_id: UUID, body: m.SpoolMove
) -> m.Spool:
    try:
        await asyncio.to_thread(
            filament.locations.move, spool_id, body.location_id, actor=actor(caller)
        )
    except LookupError:
        raise _no_spool(spool_id) from None
    except MoveTargetError as exc:
        raise _refused(exc) from None
    moved = await asyncio.to_thread(filament.spools.get, spool_id)
    if moved is None:
        raise _no_spool(spool_id)
    return moved


@router.post(
    "/bambuddy/import",
    response_model=PullReport,
    summary="Import Bambuddy's spools",
    description=(
        "Read Bambuddy's unarchived spools and pull them into ScadBuddy's own, once (the spool "
        "refresh Schedule does the same). A BB spool already linked takes the fields BB changed "
        "since the last pull, unless ScadBuddy changed that field later; a change of what is "
        "left is a `bb` reading unless the spool's weight is locked. A BB spool whose tag a "
        "ScadBuddy spool holds, or whose record a ScadBuddy spool is linked to, is linked; any "
        "other becomes a new spool, typed by `material_number`, else by brand, material, "
        "subtype, colour and label weight, else left to need a type. Never writes to Bambuddy. "
        "An archived ScadBuddy spool is never restored or changed; it is listed as `archived`. "
        "The ledger events name the caller. Answers the counts and ids made, linked, changed, "
        "still needing a type and archived, the BB spool ids it could not pull, and the count "
        "(and ids) of BB rows it could not read. 409 when no Bambuddy is configured; "
        "Bambuddy's own errors as they come."
    ),
)
async def import_bambuddy(
    filament: FilamentDep, store: SettingsStoreDep, caller: CallerDep
) -> PullReport:
    settings = await asyncio.to_thread(store.load)
    return await read_and_pull(filament, settings, actor=actor(caller))
