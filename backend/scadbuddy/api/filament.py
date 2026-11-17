"""`/api/v1/filament` (#6261, epic #6260): ScadBuddy's own filament inventory. This
module holds the catalogs a spool type or a spool picks from (plain reads and writes
over `filament/catalog.py`), the spool types (`filament/types.py`), and the spools,
their tags and their ledger (`filament/spools.py`, `filament/ledger.py`). Only the import
(`POST /filament/bambuddy/import`, `filament/bambuddy_pull.py`) reaches Bambuddy, and it
only reads."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Body, Query, Response, status
from pydantic import AwareDatetime, BaseModel, ValidationError

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.auth.component import CallerDep
from scadbuddy.auth.models import Caller, Person, PersonalToken, Service
from scadbuddy.bambuddy.models import SpoolAssignment
from scadbuddy.core.problems import DATABASE_ERRORS, ApiError
from scadbuddy.crud.idempotency import (
    MaybeIdempotencyKey,
    Replay,
    idempotent_write,
    required_key,
)
from scadbuddy.filament import models as m
from scadbuddy.filament.bambuddy_pull import PullReport, read_and_pull, try_assignments
from scadbuddy.filament.catalog import (
    CATALOGS,
    CatalogExistsError,
    CatalogRefError,
    CatalogSpec,
)
from scadbuddy.filament.component import FilamentDep
from scadbuddy.filament.csv_import import (
    ImportPreview,
    ImportRefusedError,
    SpoolCsvImport,
    import_on,
    import_rows,
)
from scadbuddy.filament.ledger import BadCursorError
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
from scadbuddy.library.settings_store import SettingsStore
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
    TypeRowsDuplicateError,
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
        "type's, else its subtype's and material's print defaults), the names of the "
        "fields it `overridden`, the `names` behind its ids and `low_stock` (the grams left "
        "are under its low-stock percentage of its label weight, else 20 %; false when "
        "either weight is unknown). `needs_type=true` keeps only spools with no type yet, "
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


IMPORT_ROUTE = "POST /filament/spools/import"


@router.post(
    "/spools/import",
    response_model=ImportPreview,
    summary="Import spools from CSV rows",
    description=(
        "Rows of a spools CSV, parsed by the caller (cells as text, under the export's "
        "column names). A row with a `number` changes that spool, only where a cell differs "
        "from what it shows now; an empty cell changes nothing. A row with no `number` makes "
        "a spool. Material, subtype, brand, category and effect names resolve in any case "
        "and are added to their catalog when missing; `insert_type` and `type` (a spool "
        "type's code) must exist; `location` must name exactly one unarchived place and is "
        "a move. Derived columns (`grams_left`, `last_used_at`, ...) are ignored; any other "
        "unknown column is a 422. `dry_run: true` answers what each row would do (`create`, "
        "`update` or `error`, with its changes as `[before, after]`) and writes nothing. "
        "`dry_run: false` writes every row in one transaction, and needs an "
        "`Idempotency-Key` (428 without one): a retry with the same key answers the first "
        "result and writes nothing, and the key with another body is a 422. A write with "
        "any row in error is a 422 naming each line, and writes nothing."
    ),
)
async def import_spools(
    filament: FilamentDep,
    caller: CallerDep,
    body: SpoolCsvImport,
    idempotency_key: MaybeIdempotencyKey = None,
) -> ImportPreview:
    who = actor(caller)
    if body.dry_run:
        return await asyncio.to_thread(import_rows, filament, body.rows, dry_run=True, actor=who)
    # A person's key, under their own name: one person's key never replays another's.
    key = f"{who}\x1f{required_key(idempotency_key)}"
    asked = body.model_dump(mode="json")

    def write() -> Replay:
        with filament.spools.transaction() as conn:

            def run() -> Replay:
                done = import_on(conn, filament, body.rows, dry_run=False, actor=who)
                return Replay(status.HTTP_200_OK, done.model_dump(mode="json"))

            return idempotent_write(conn, "filament", key, IMPORT_ROUTE, asked, run)

    try:
        done = await asyncio.to_thread(write)
    except ImportRefusedError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    return ImportPreview.model_validate(done.body)


@router.get(
    "/spools/{spool_id}",
    response_model=m.SpoolDetail,
    summary="Read a spool",
    description=(
        "One spool (archived too) with its effective values and `names`, and what its page "
        "shows: the `tag` bound now, the `last_tag` it had before and the number of the "
        "spool that holds that one (`held_by`), its effective `presets` and `k_values`, the "
        "`basis` of its `grams_left` (the anchor reading and the deltas taken off it), and "
        "`in_use` (it has printed, so it is archived rather than deleted). 404 for none."
    ),
)
async def get_spool(filament: FilamentDep, spool_id: UUID) -> m.SpoolDetail:
    found = await asyncio.to_thread(filament.spools.detail, spool_id)
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
    "/spools/{spool_id}/presets",
    response_model=list[m.EffectivePreset],
    summary="Read a spool's slicer presets",
    description=(
        "The presets the spool prints with: one slicer filament per printer model and "
        "nozzle diameter, the spool's own row for a key over its type's (`overridden` is "
        "true for the spool's own). 404 for no such spool."
    ),
)
async def get_spool_presets(filament: FilamentDep, spool_id: UUID) -> list[m.EffectivePreset]:
    found = await asyncio.to_thread(filament.spools.presets, spool_id)
    if found is None:
        raise _no_spool(spool_id)
    return found


@router.put(
    "/spools/{spool_id}/presets",
    response_model=list[m.EffectivePreset],
    summary="Replace a spool's own slicer presets",
    description=(
        "The whole list of the spool's own presets; the answer is the effective set. An "
        "empty list resets the spool to its type's presets. Writes one `edited` event. "
        "404 for no such spool, 422 for two rows with the same model and nozzle."
    ),
)
async def put_spool_presets(
    filament: FilamentDep, caller: CallerDep, spool_id: UUID, body: list[m.SpoolPresetCreate]
) -> list[m.EffectivePreset]:
    try:
        saved = await asyncio.to_thread(
            filament.spools.set_presets, spool_id, body, actor=actor(caller)
        )
    except _SPOOL_REFUSED as exc:
        raise _refused(exc) from None
    if saved is None:
        raise _no_spool(spool_id)
    return saved


@router.get(
    "/spools/{spool_id}/k-values",
    response_model=list[m.EffectiveKValue],
    summary="Read a spool's pressure-advance values",
    description=(
        "The K values the spool prints with: one per printer, extruder and nozzle diameter, "
        "the spool's own row for a key over its type's (`overridden` is true for the "
        "spool's own). 404 for no such spool."
    ),
)
async def get_spool_k_values(filament: FilamentDep, spool_id: UUID) -> list[m.EffectiveKValue]:
    found = await asyncio.to_thread(filament.spools.k_values, spool_id)
    if found is None:
        raise _no_spool(spool_id)
    return found


@router.put(
    "/spools/{spool_id}/k-values",
    response_model=list[m.EffectiveKValue],
    summary="Replace a spool's own pressure-advance values",
    description=(
        "The whole list of the spool's own K values; the answer is the effective set. An "
        "empty list resets the spool to its type's values. Writes one `edited` event. 404 "
        "for no such spool, 422 for a printer that does not exist or a repeated key."
    ),
)
async def put_spool_k_values(
    filament: FilamentDep, caller: CallerDep, spool_id: UUID, body: list[m.SpoolKValueCreate]
) -> list[m.EffectiveKValue]:
    try:
        saved = await asyncio.to_thread(
            filament.spools.set_k_values, spool_id, body, actor=actor(caller)
        )
    except _SPOOL_REFUSED as exc:
        raise _refused(exc) from None
    if saved is None:
        raise _no_spool(spool_id)
    return saved


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


@router.get(
    "/tags",
    response_model=list[m.TagRow],
    summary="List tags",
    description=(
        "Every tag, oldest first, with its open `binding` and the `spool_number` it is on. "
        "A tag on no spool only with `include_unbound`."
    ),
)
async def list_tags(filament: FilamentDep, include_unbound: bool = False) -> list[m.TagRow]:
    return await asyncio.to_thread(filament.spools.tags, include_unbound=include_unbound)


@router.get(
    "/tags/{tag_id}",
    response_model=m.TagDetail,
    summary="Read a tag",
    description=(
        "One tag with its open binding and every binding it has had, newest first. 404 for none."
    ),
)
async def get_tag(filament: FilamentDep, tag_id: UUID) -> m.TagDetail:
    found = await asyncio.to_thread(filament.spools.tag, tag_id)
    if found is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no tag {tag_id}")
    return found


@router.get(
    "/events",
    response_model=m.EventPage,
    summary="Read the ledger",
    description=(
        "Every spool's events, newest first by time then booking order, each with its "
        "`spool_number`. Filter by `spool_id`, `kind`, `source` and `since` (a time with "
        "its offset, included) and `until` (the same, excluded, so `since` and `until` give a "
        "range that tiles). A page holds up to `limit` events (at most 500); `next_cursor` is "
        "opaque, and passing it back as `cursor` gives the next page with no row lost or "
        "repeated. 422 for a cursor no page gave."
    ),
)
async def list_events(
    filament: FilamentDep,
    spool_id: UUID | None = None,
    kind: m.EventKind | None = None,
    source: m.EventSource | None = None,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> m.EventPage:
    try:
        return await asyncio.to_thread(
            filament.spools.ledger,
            spool_id=spool_id,
            kind=kind,
            source=source,
            since=since,
            until=until,
            cursor=cursor,
            limit=limit,
        )
    except BadCursorError as exc:
        raise _refused(exc) from None


@router.get(
    "/summary",
    response_model=m.InventorySummary,
    summary="Summarise the inventory",
    description=(
        "The unarchived spools in total: their number, grams left, the value of those "
        "grams at each spool's cost per kg (`null` when none has a cost), the stock by "
        "material, how many sit in a printer's tray (a place with `printer_id` set), how "
        "many are low on stock, and how many still need a type."
    ),
)
async def get_summary(filament: FilamentDep) -> m.InventorySummary:
    return await asyncio.to_thread(filament.spools.summary)


#: How long a read of Bambuddy's assignments serves the next requests (I-1 of the final
#: review of #6262), and how long after a failed or timed-out read none is tried again:
#: the page and the hover card then wait on Bambuddy at most once per window, not on
#: every load.
ASSIGNMENTS_FRESH_SECONDS = 5.0
ASSIGNMENTS_BACKOFF_SECONDS = 30.0
_now = time.monotonic
_assignments_lock = asyncio.Lock()
#: The BB URL read, when, and what it answered (``None``: it failed, serve no assignments).
_assignments_seen: tuple[str, float, list[SpoolAssignment] | None] | None = None


async def _assignments(store: SettingsStore) -> list[SpoolAssignment]:
    """Bambuddy's tray assignments (`try_assignments`), read at most once per
    `ASSIGNMENTS_FRESH_SECONDS`, one read at a time; after a failed read, none for
    `ASSIGNMENTS_BACKOFF_SECONDS` without asking Bambuddy again. 503 when the settings
    cannot be read, as when the spools cannot."""
    global _assignments_seen
    try:
        settings = await asyncio.to_thread(store.load)
    except DATABASE_ERRORS as error:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "the settings could not be read; try again"
        ) from error
    if not settings.bambuddy_url:
        return []
    async with _assignments_lock:
        seen = _assignments_seen
        if seen is not None and seen[0] == settings.bambuddy_url:
            window = (
                ASSIGNMENTS_FRESH_SECONDS if seen[2] is not None else ASSIGNMENTS_BACKOFF_SECONDS
            )
            if _now() - seen[1] < window:
                return seen[2] or []
        found = await try_assignments(settings)
        _assignments_seen = (settings.bambuddy_url, _now(), found)
        return found or []


@router.get(
    "/inventory",
    response_model=m.FilamentInventoryView,
    summary="Spools and the trays they are in",
    description=(
        "Every spool by number, with its effective values (`rgba` is `#RRGGBBAA`), its "
        "presets and the tray it is in (`loaded`: the tray a registry printer's latest "
        "report holds it in, else the tray it was last linked to on a printer with no "
        "report; `null` on the shelf), and its linked Bambuddy spool. `trays` is each "
        "registry printer's reported trays, with the spool each holds and how it was found "
        "(`tag` or `link`). Archived spools only with `include_archived`. While Bambuddy is "
        "linked, a spool no registry printer reports is in the tray Bambuddy assigns its "
        "linked Bambuddy spool to (`GET /inventory/assignments`; plan Decision 3, until "
        "#6263); a failed read places the spools by the reports alone. Nothing else is read "
        "from Bambuddy."
    ),
)
async def get_inventory(
    filament: FilamentDep, store: SettingsStoreDep, include_archived: bool = False
) -> m.FilamentInventoryView:
    assignments = await _assignments(store)
    return await asyncio.to_thread(
        filament.inventory.view, include_archived=include_archived, bb_assignments=assignments
    )


@router.get(
    "/spools/{spool_id}/summary",
    response_model=m.FilamentSpoolSummary,
    summary="One spool's stock and price, and its filament's",
    description=(
        "What a filament hover card shows (#785): the spool's grams left, its tray and its "
        "effective `cost_per_kg`, the value of what is left and, with `grams`, what those "
        "grams cost; and every unarchived spool of the same filament (effective material, "
        "subtype, brand and `#RRGGBBAA`, so a translucent spool is not an opaque one's) with "
        "their total grams left and where they are loaded. An archived spool is still "
        "summarised. With no cost per kg every price and cost is `null`. Each spool is placed "
        "as `GET /filament/inventory` places it. 404 for no such spool."
    ),
)
async def get_spool_summary(
    filament: FilamentDep,
    store: SettingsStoreDep,
    spool_id: UUID,
    grams: Annotated[
        float | None,
        Query(ge=0, description="Grams to price at this spool's cost, e.g. a plate's use"),
    ] = None,
) -> m.FilamentSpoolSummary:
    assignments = await _assignments(store)
    found = await asyncio.to_thread(filament.inventory.summary, spool_id, grams, assignments)
    if found is None:
        raise _no_spool(spool_id)
    return found


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
