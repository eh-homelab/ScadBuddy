"""`/api/v1/filament` (#6261, epic #6260): ScadBuddy's own filament inventory. This
module holds the catalogs a spool type or a spool picks from (plain reads and writes
over `filament/catalog.py`), the spool types (`filament/types.py`), and the spools,
their tags and their ledger (`filament/spools.py`, `filament/ledger.py`). Some routes reach
Bambuddy. Only reads: the import (`POST /filament/bambuddy/import`,
`filament/bambuddy_pull.py`), the list of Home Assistant entities
(`GET /filament/sensors/entities`, #6268), `GET /rfid/status` and `POST /rfid/scan`.
Writes, each with its own rule: a tag change (`POST /filament/spools/{id}/tags`, its unbind,
and the NTAG confirm,
`filament/rfid_push.py`, #6264) tells Bambuddy only while ``filament_sync.tags`` pushes
(``sb_to_bb`` or ``two_way``);
`PUT /rfid/override-mode` writes Bambuddy's auto-add setting (`filament/rfid_mode.py`) and
`POST /rfid/reconcile` runs the mode, the tag push and the AMS slot configure of each override
tag (`filament/rfid_slots.py`), whatever ``tags`` says; the NTAG write and cancel
(`filament/ntag.py`) act on a SpoolBuddy device only when the caller asks.

Every 2xx answer to a write here, in `api/filament_purchasing.py` and in
`api/tray_spools.py` nudges the ScadBuddy-to-Bambuddy sync (`nudge_after_write`, #6265
Decision 17)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from pydantic import AwareDatetime, BaseModel, ValidationError

from scadbuddy.api.deps import SettingsStoreDep, StateDep
from scadbuddy.auth.component import CallerDep
from scadbuddy.auth.models import Caller, Person, PersonalToken, Service
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import HaEntity, SpoolAssignment
from scadbuddy.core.problems import DATABASE_ERRORS, ApiError
from scadbuddy.crud.idempotency import (
    MaybeIdempotencyKey,
    Replay,
    idempotent_write,
    required_key,
)
from scadbuddy.filament import capture, ntag, rfid_mode
from scadbuddy.filament import models as m
from scadbuddy.filament.assignments_cache import AssignmentsCache
from scadbuddy.filament.bambuddy_pull import PullReport, Pulls, read_and_pull, try_assignments
from scadbuddy.filament.catalog import (
    CATALOGS,
    CatalogExistsError,
    CatalogRefError,
    CatalogSpec,
)
from scadbuddy.filament.component import Filament, FilamentDep
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
from scadbuddy.filament.rfid_mode import read_status, reconcile_mode_now
from scadbuddy.filament.rfid_push import push_pending
from scadbuddy.filament.rfid_slots import reconcile_slots_now
from scadbuddy.filament.sensors import SensorBandError
from scadbuddy.filament.spools import (
    OverrideKindError,
    SpoolBambuddyLinkedError,
    SpoolInUseError,
    SpoolTypelessError,
    TagKeysClashError,
    TagUnnamedError,
)
from scadbuddy.filament.types import TypeRowsDuplicateError, TypeTempError
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.operations.kinds import waiting_on_bambuddy
from scadbuddy.orders.store import FilamentExistsError
from scadbuddy.printers.component import PrintersDep


async def nudge_after_write(request: Request, state: StateDep) -> AsyncIterator[None]:
    """A router's dependency: after a write's 2xx answer, nudge the filament sync in the
    background (Decision 17). A route that raised (any error answer, a 422 of its body
    too) nudges nothing, and neither does a read."""
    yield
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        state.filament_sync.nudge_soon()


router = APIRouter(prefix="/filament", tags=["filament"], dependencies=[Depends(nudge_after_write)])
logger = logging.getLogger(__name__)

#: How long a tag change waits to tell Bambuddy before the reply goes (#6264); the
#: refresh Schedule finishes the rest.
RFID_INLINE_SECONDS = 2.5

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
    OverrideKindError,
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


@router.post(
    "/spools/sync-ams-weights",
    response_model=m.AmsSyncResult,
    summary="Sync weights from the AMS",
    description=(
        "Books the AMS's remaining percent of each spool loaded in a tray now, as a reading "
        "of its grams left (remain times its label weight) that outranks older ones. Skips "
        "a spool whose weight is locked, every spool of a printer that is printing, one "
        "whose tray reports no `remain` (only RFID trays do), and every spool of a printer "
        "with no report or a stale one. Each spool in a printer's tray is in one count; "
        "`spools` are the ones booked."
    ),
)
async def sync_ams_weights(filament: FilamentDep, caller: CallerDep) -> m.AmsSyncResult:
    return await asyncio.to_thread(
        capture.sync_ams_weights, filament.spools.pool, actor=actor(caller), now=datetime.now(UTC)
    )


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
        "tags, 422 when neither key is left once cleaned. With `filament_sync.tags` set to "
        "`sb_to_bb` or `two_way` the change is also told to Bambuddy, for at most 2.5 seconds; "
        "what is not "
        "done by then is finished by the spool refresh, and a Bambuddy that is down never "
        "fails the request."
    ),
)
async def bind_spool_tag(
    filament: FilamentDep,
    store: SettingsStoreDep,
    caller: CallerDep,
    spool_id: UUID,
    body: m.TagBind,
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
    await _push_now(filament, store)
    return bound


async def _push_now(filament: Filament, store: SettingsStore) -> None:
    """Tell Bambuddy of the queued tags once, for at most `RFID_INLINE_SECONDS`. Whatever
    it does not finish stays queued for the refresh; nothing here fails the request."""
    try:
        async with asyncio.timeout(RFID_INLINE_SECONDS), waiting_on_bambuddy():
            await push_pending(
                filament,
                store.load,
                lambda: store.read({"filament_sync"})["filament_sync"],
            )
    except Exception as error:
        logger.info(
            "the tags were not pushed in the request", extra={"error": type(error).__name__}
        )


@router.delete(
    "/tags/{tag_id}/binding",
    response_model=m.TagBinding,
    summary="Take a tag off its spool",
    description=(
        "Closes the tag's open binding, with a `tag_unbound` ledger event on its spool, "
        "and answers the closed binding. 404 when the tag is on no spool."
    ),
)
async def unbind_tag(
    filament: FilamentDep, store: SettingsStoreDep, caller: CallerDep, tag_id: UUID
) -> m.TagBinding:
    closed = await asyncio.to_thread(filament.spools.unbind_tag, tag_id, actor=actor(caller))
    if closed is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"tag {tag_id} is on no spool")
    await _push_now(filament, store)
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
        "`bb`) in `grams_after`, print use in `delta_g`. `grams_left` is "
        "`ledger.basis`: the newest reading less the use after it, a paired plate counted "
        "once and Bambuddy's usage rows counted. 404 for no such spool."
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


async def _assignments(store: SettingsStore, cache: AssignmentsCache) -> list[SpoolAssignment]:
    """Bambuddy's tray assignments, through the filament component's `AssignmentsCache`.
    503 when the settings cannot be read, as when the spools cannot."""
    try:
        settings = await asyncio.to_thread(store.load)
    except DATABASE_ERRORS as error:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "the settings could not be read; try again"
        ) from error
    if not settings.bambuddy_url:
        return []
    return await cache.get(settings, try_assignments)


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
    assignments = await _assignments(store, filament.assignments)
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
    assignments = await _assignments(store, filament.assignments)
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


# -- location sensors (#6268) ---------------------------------------------------------


@router.get(
    "/sensors",
    response_model=list[m.Sensor],
    summary="List the location sensors",
    description=(
        "The unarchived humidity and temperature sensors of the places, by name; of one "
        "place with `location_id`. Each carries its last stored value and time, `reachable` "
        "(false when its last read had no number), and `alerting` (the last value is "
        "outside `alert_below` / `alert_above`)."
    ),
)
async def list_sensors(filament: FilamentDep, location_id: UUID | None = None) -> list[m.Sensor]:
    return await asyncio.to_thread(filament.sensors.list, location_id)


@router.get(
    "/sensors/entities",
    response_model=list[HaEntity],
    summary="List Home Assistant entities",
    description=(
        "The Home Assistant entities Bambuddy can read, with their states, for choosing a "
        "sensor's `entity_id`; `search` narrows them. Reads Bambuddy and writes nothing. "
        "409 when no Bambuddy is configured; Bambuddy's own errors as they come."
    ),
)
async def list_ha_entities(store: SettingsStoreDep, search: str | None = None) -> list[HaEntity]:
    settings = await asyncio.to_thread(store.load)
    async with client_for(settings) as client:
        return await client.ha_entities(search)


@router.post(
    "/sensors",
    response_model=m.Sensor,
    status_code=status.HTTP_201_CREATED,
    summary="Add a location sensor",
    description=(
        "A `home_assistant` sensor reads the state of an `entity_id` through Bambuddy (the "
        "pull reads it) and is put on a place; it has a `kind` (`humidity` or "
        "`temperature`), a `name`, an optional `unit`, an alert band (`alert_below`, "
        "`alert_above`) and `notify`. An `ams` sensor is made by ScadBuddy from the "
        "printer's report, never by hand (422). 422 for a bad field or a place that does "
        "not exist or is archived."
    ),
)
async def create_sensor(filament: FilamentDep, caller: CallerDep, body: m.SensorIn) -> m.Sensor:
    if body.source == "ams":
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "an ams sensor is made from the printer's report, not by hand",
        )
    try:
        return await asyncio.to_thread(filament.sensors.create, body, actor(caller))
    except CatalogRefError as exc:
        raise _refused(exc) from None


@router.patch(
    "/sensors/{sensor_id}",
    response_model=m.Sensor,
    summary="Change a location sensor",
    description=(
        "Only the fields sent change: `location_id`, `name`, `unit`, the alert band and "
        "`notify`; `null` clears a nullable one. Its source, kind and where it reads from "
        "stay as made. 404 for no such sensor, 422 for a bad field or a place that does "
        "not exist."
    ),
)
async def patch_sensor(
    filament: FilamentDep, caller: CallerDep, sensor_id: UUID, body: m.SensorPatch
) -> m.Sensor:
    try:
        return await asyncio.to_thread(filament.sensors.update, sensor_id, body, actor(caller))
    except LookupError:
        raise _no_sensor(sensor_id) from None
    except (CatalogRefError, SensorBandError) as exc:
        raise _refused(exc) from None


@router.post(
    "/sensors/{sensor_id}/archive",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive a location sensor",
    description=(
        "Takes the sensor off the lists; its readings are kept. 404 for no such sensor, or "
        "one already archived."
    ),
)
async def archive_sensor(filament: FilamentDep, caller: CallerDep, sensor_id: UUID) -> Response:
    try:
        await asyncio.to_thread(filament.sensors.archive, sensor_id, actor(caller))
    except LookupError:
        raise _no_sensor(sensor_id) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/sensors/{sensor_id}/readings",
    response_model=list[m.SensorReading],
    summary="A sensor's readings",
    description=(
        "The stored readings (`at`, `value`) from `since` (default: the last 24 hours) on, "
        "oldest first; with more than `limit` (default 500, at most 5000) in the window, the "
        "newest `limit`. A reading is stored "
        "when the value changed, or 15 minutes after the last stored one. 404 for no such "
        "sensor (an archived one still answers)."
    ),
)
async def sensor_readings(
    filament: FilamentDep,
    sensor_id: UUID,
    since: AwareDatetime | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> list[m.SensorReading]:
    if not await asyncio.to_thread(filament.sensors.exists, sensor_id):
        raise _no_sensor(sensor_id)
    start = since or datetime.now(UTC) - timedelta(hours=24)
    return await asyncio.to_thread(filament.sensors.readings, sensor_id, start, limit)


def _no_sensor(sensor_id: UUID) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no sensor {sensor_id}")


@router.post(
    "/spools/{spool_id}/move",
    response_model=m.Spool,
    summary="Move a spool",
    description=(
        "Put the spool in a place (`location_id: null`: in none) and write a `moved` ledger "
        "event, `{from, to}`. A spool already there is answered as it is, with no event. "
        "404 for no such spool, 422 for a place that does not exist, is archived or is an "
        "AMS unit's place."
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
        "left is a `bb` reading unless the spool's weight is locked. "
        "It pulls the spools whatever `filament_sync.spools` says. With `filament_sync.weights` "
        "`off` or `sb_to_bb`, no Bambuddy scale weighing or drying is booked, and a spool "
        "ScadBuddy already tracks takes no `bb` weight reading and no weight lock; a new spool "
        "still gets its first weight and Bambuddy's lock. With `filament_sync.tags` or "
        "`presets` `off` or `sb_to_bb`, a linked or newly linked spool takes no tag and no "
        "preset from Bambuddy. Usage rows are booked on linked spools. "
        "A BB spool whose tag a "
        "ScadBuddy spool holds, or whose record a ScadBuddy spool is linked to, is linked; any "
        "other becomes a new spool, typed by `material_number`, else by brand, material, "
        "subtype, colour and label weight, else left to need a type. A BB spool Bambuddy added "
        "for a tag a linked ScadBuddy spool holds makes no spool (`rfid_duplicates` and "
        "`rfid_duplicate_bb_ids` name it, and it stays skipped). Never writes to Bambuddy. "
        "An archived ScadBuddy spool is never restored or changed; it is listed as `archived`. "
        "The ledger events name the caller. Answers the counts and ids made, linked, changed, "
        "still needing a type and archived, the BB spool ids it could not pull, the count "
        "(and ids) of BB rows it could not read, and the duplicates it skipped. 409 when no "
        "Bambuddy is configured; Bambuddy's own errors as they come."
    ),
)
async def import_bambuddy(
    filament: FilamentDep, store: SettingsStoreDep, caller: CallerDep
) -> PullReport:
    settings = await asyncio.to_thread(store.load)
    sync = (await asyncio.to_thread(store.read, {"filament_sync"}))["filament_sync"]
    # A person's import pulls the spools whatever `spools` says; the rest follow the setting.
    pulls = dataclasses.replace(Pulls.of(sync), spools=True)
    return await read_and_pull(filament, settings, actor=actor(caller), pulls=pulls)


@router.get(
    "/rfid/status",
    response_model=m.RfidStatus,
    summary="Read the RFID override mode",
    description=(
        "Whether the RFID override mode is on, and where Bambuddy's `auto_add_unknown_rfid` "
        "stands: `applied` (mode on, Bambuddy's value is false), `pending` (a write has not "
        "landed, or Bambuddy could not be reached; `detail` names the error), `blocked` "
        "(Bambuddy refused the write because its auth is on) or `off`. Also lists the "
        "SpoolBuddy devices. Reads Bambuddy and writes nothing; when Bambuddy fails, "
        "`bambuddy_auto_add` is null and `devices` is empty."
    ),
)
async def get_rfid_status(filament: FilamentDep, store: SettingsStoreDep) -> m.RfidStatus:
    return await read_status(filament.rfid, store.load)


@router.put(
    "/rfid/override-mode",
    response_model=m.RfidStatus,
    summary="Turn the RFID override mode on or off",
    description=(
        "While the mode is on, Bambuddy's auto-add of unknown RFID spools is turned off; "
        "the value Bambuddy had is saved first and put back when the mode turns off. The "
        "mode is written first, then Bambuddy is reconciled once, and the answer is the "
        "status. It is 200 even when Bambuddy refuses the write (`blocked`: its auth is "
        "on) or cannot be reached (`pending`); the mode then stays as asked and the "
        "refresh retries. Another reconcile may hold the mode lock: the route waits up to 5 "
        "seconds, then answers `pending` with detail `busy`, and the refresh applies the mode."
    ),
)
async def set_rfid_override_mode(
    filament: FilamentDep, store: SettingsStoreDep, body: m.RfidModeSet
) -> m.RfidStatus:
    await asyncio.to_thread(filament.rfid.set_mode, body.enabled)
    return await reconcile_mode_now(
        filament.rfid, store.load, wait=rfid_mode.RFID_MODE_LOCK_WAIT_SECONDS
    )


@router.post(
    "/rfid/reconcile",
    response_model=m.RfidReconcileReport,
    summary="Reconcile RFID with Bambuddy now",
    description=(
        "Runs once what the spool refresh Schedule runs: Bambuddy's `auto_add_unknown_rfid` "
        "is brought to the override mode, then the queued tag changes are pushed (only with "
        "`filament_sync.tags` `sb_to_bb` or `two_way`), then the AMS slot of each override tag is "
        "configured with its type's preset. Answers the mode's status and the tag push "
        "counts, and the override slots' counts (`slots` is null with no Bambuddy)."
    ),
)
async def reconcile_rfid(
    filament: FilamentDep, store: SettingsStoreDep, printers: PrintersDep
) -> m.RfidReconcileReport:
    # Each step fails alone, as in the activity: one broken step never skips the others.
    try:
        mode = await reconcile_mode_now(filament.rfid, store.load)
    except Exception as error:
        logger.warning("could not reconcile the RFID mode", extra={"error": type(error).__name__})
        try:
            on = (await asyncio.to_thread(filament.rfid.state)).override_mode
        except Exception:
            on = False
        mode = m.RfidStatus(override_mode=on, state="pending", detail="internal")
    try:
        tags = await push_pending(
            filament, store.load, lambda: store.read({"filament_sync"})["filament_sync"]
        )
    except Exception as error:
        logger.warning("could not push the tags", extra={"error": type(error).__name__})
        tags = m.TagPushReport(failed=1)
    try:
        slots = await reconcile_slots_now(
            filament, store.load, printers=printers.store, reports=printers.reports
        )
    except Exception as error:
        logger.warning("could not configure the slots", extra={"error": type(error).__name__})
        slots = m.SlotReport(failed=1)
    return m.RfidReconcileReport(mode=mode, tags=tags, slots=slots)


@router.post(
    "/rfid/scan",
    response_model=m.ScanResult,
    summary="Wait for a tag at a SpoolBuddy reader",
    description=(
        "Listens on Bambuddy's WebSocket for up to `timeout_s` seconds (1 to 50, default 30) "
        "while a person holds a tag to a SpoolBuddy reader; `device_id` limits it to one "
        "reader. Answers 200 either way: `status` `tag` with the tag's `tag_uid`, "
        "`tray_uuid`, `tag_type`, its `kind` (`bambu_mifare` with a tray UUID, `ntag_opentag` for "
        "an NTAG chip, else `other`; for a tag Bambuddy already knows, which names no chip, the "
        "kind of the ScadBuddy tag bound to it, else `ntag_opentag` when Bambuddy's record says "
        "OpenTag3D wrote it, else `other`), the Bambuddy spool it matched (`bambuddy_spool_id`) "
        "and the ScadBuddy spool that holds the tag (`sb_spool`, else the one linked to that "
        "Bambuddy spool, else null); or `status` `timeout` with nothing else when the wait ran "
        "out. Writes nothing. 409 when no Bambuddy is configured or its key lacks the Read "
        "Status scope; 502 or 504 when Bambuddy cannot be reached or closes the socket first."
    ),
)
async def scan_rfid_tag(
    filament: FilamentDep, store: SettingsStoreDep, body: m.ScanRequest
) -> m.ScanResult:
    settings = await asyncio.to_thread(store.load)
    async with client_for(settings) as client:
        return await ntag.scan(filament, client, timeout=body.timeout_s, device_id=body.device_id)


async def _spool_or_404(filament: Filament, spool_id: UUID) -> None:
    if await asyncio.to_thread(filament.spools.get, spool_id) is None:
        raise _no_spool(spool_id)


def _ntag_refused(exc: ntag.NtagNoBambuddySpoolError | ntag.NtagOverrideOpenError) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, str(exc), code=exc.code)


@router.post(
    "/spools/{spool_id}/ntag/write",
    response_model=m.NtagQueued,
    summary="Queue an NTAG write for a spool",
    description=(
        "Asks the SpoolBuddy `device_id` to write an OpenTag3D tag for the spool's Bambuddy "
        "record; the person then holds a blank tag to the reader. It changes no tag binding: "
        "call confirm after the write. `warnings` lists what Bambuddy says is missing from "
        "the record. 404 for no such spool. 409 with `code` `no_bambuddy_spool` when the "
        "spool is not linked to a Bambuddy spool, and `override_tag_open` when its open tag "
        "was bound with `override` and `replace` is false."
    ),
)
async def write_ntag(
    filament: FilamentDep, store: SettingsStoreDep, spool_id: UUID, body: m.NtagWrite
) -> m.NtagQueued:
    await _spool_or_404(filament, spool_id)
    settings = await asyncio.to_thread(store.load)
    try:
        async with client_for(settings) as client:
            return await ntag.write(
                filament, client, spool_id, body.device_id, replace=body.replace
            )
    except (ntag.NtagNoBambuddySpoolError, ntag.NtagOverrideOpenError) as exc:
        raise _ntag_refused(exc) from None


@router.post(
    "/spools/{spool_id}/ntag/confirm",
    response_model=m.NtagConfirm,
    summary="Bind the NTAG that was written for a spool",
    description=(
        "Reads the spool's Bambuddy record. When it holds a tag UID written by OpenTag3D "
        "that differs from the one it held when the write was queued, binds that tag as "
        "`ntag_opentag` (the ledger names the caller) and answers `bound` true with "
        "`tag_uid`; otherwise (also when no write is queued) answers `bound` false and "
        "changes nothing, so it can be called again. 404 for no such "
        "spool, 409 with `code` `no_bambuddy_spool` when the spool is not linked to a "
        "Bambuddy spool."
    ),
)
async def confirm_ntag(
    filament: FilamentDep, store: SettingsStoreDep, caller: CallerDep, spool_id: UUID
) -> m.NtagConfirm:
    await _spool_or_404(filament, spool_id)
    settings = await asyncio.to_thread(store.load)
    try:
        async with client_for(settings) as client:
            confirmed = await ntag.confirm(filament, client, spool_id, actor=actor(caller))
    except ntag.NtagNoBambuddySpoolError as exc:
        raise _ntag_refused(exc) from None
    if confirmed.bound:
        await _push_now(filament, store)
    return confirmed


@router.post(
    "/spools/{spool_id}/ntag/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel a queued NTAG write",
    description=("Drops the write the SpoolBuddy `device_id` has queued. 404 for no such spool."),
)
async def cancel_ntag(
    filament: FilamentDep, store: SettingsStoreDep, spool_id: UUID, body: m.NtagCancel
) -> Response:
    await _spool_or_404(filament, spool_id)
    settings = await asyncio.to_thread(store.load)
    async with client_for(settings) as client:
        await ntag.cancel(filament, client, spool_id, body.device_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
