"""`/api/v1/filament` (#6261, epic #6260): ScadBuddy's own filament inventory. This
module holds the catalogs a spool type or a spool picks from (plain reads and writes
over `filament/catalog.py`), the spool types (`filament/types.py`), and the spools,
their tags and their ledger (`filament/spools.py`, `filament/ledger.py`). Some routes reach
Bambuddy. Only reads: the import (`POST /filament/bambuddy/import`, whose Bambuddy reads
the print worker makes since #6772, `filament/bambuddy_pull.py`), the list of Home Assistant
entities (`GET /filament/sensors/entities`, #6268), `GET /rfid/status` and `POST /rfid/scan`.
Writes, each with its own rule: a tag change (`POST /filament/spools/{id}/tags`, its unbind,
and the NTAG confirm,
`filament/rfid_push.py`, #6264) tells Bambuddy only while ``filament_sync.tags`` pushes
(``sb_to_bb`` or ``two_way``);
`PUT /rfid/override-mode` writes Bambuddy's auto-add setting (`filament/rfid_mode.py`) and
`POST /rfid/reconcile` runs the mode, the tag push and the AMS slot configure of each override
tag (`filament/rfid_slots.py`), whatever ``tags`` says; the NTAG write and cancel
(`filament/ntag.py`) act on a SpoolBuddy device only when the caller asks.
`POST /filament/sync/run` asks the ScadBuddy-to-Bambuddy sync for a full pass (#6265),
which writes Bambuddy only for the data types set to push; `GET /filament/sync/status`
reads how its passes went. `GET /filament/sync/reset-links` counts what a reset of the
Bambuddy links would delete, and `POST /filament/sync/reset-links` deletes them after the
Bambuddy URL changed (#6773), then nudges a full pass.

Every 2xx answer to a write here, in `api/filament_purchasing.py` and in
`api/tray_spools.py` nudges the ScadBuddy-to-Bambuddy sync (`nudge_after_write`, #6265
Decision 17)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ValidationError
from temporalio.service import RPCError

from scadbuddy.api.components import component_dep
from scadbuddy.api.deps import SettingsStoreDep, StateDep
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
    temporal_refused,
    temporal_unavailable,
)
from scadbuddy.auth.component import CallerDep
from scadbuddy.auth.models import Caller, Person, PersonalToken, Service
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.errors import not_configured
from scadbuddy.bambuddy.models import HaEntity, SpoolAssignment
from scadbuddy.core.problems import DATABASE_ERRORS, ApiError
from scadbuddy.crud.idempotency import (
    MaybeIdempotencyKey,
    Replay,
    idempotent_write,
    required_key,
)
from scadbuddy.filament import capture, label_pdf, label_templates, ntag, rfid_mode, type_proposals
from scadbuddy.filament import metrics_models as mm
from scadbuddy.filament import models as m
from scadbuddy.filament.assignments_cache import AssignmentsCache
from scadbuddy.filament.bambuddy_pull import (
    PullReport,
    Pulls,
    links_elsewhere,
    parse_rows,
    try_assignments,
)
from scadbuddy.filament.bb_pull_apply import (
    execute_pull,
    forget_actor,
    new_pull_id,
    remember_actor,
)
from scadbuddy.filament.bb_pull_models import PullStart
from scadbuddy.filament.bb_sync_models import (
    LinksResetDone,
    LinksResetPreview,
    LinksResetRequest,
    SyncPassPage,
    SyncRun,
    SyncStatus,
    skip_reason,
)
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.filament.bb_sync_passes import PAGE_MAX, BadPassCursorError
from scadbuddy.filament.bb_sync_results import (
    Listing,
    ResetRefusedError,
    reset_links,
    reset_preview,
    sync_status,
)
from scadbuddy.filament.catalog import (
    CATALOGS,
    CatalogExistsError,
    CatalogKeyClashError,
    CatalogKeyEmptyError,
    CatalogRefError,
    CatalogSpec,
)
from scadbuddy.filament.colour_catalog import ColourSync, ColourSyncRequest, ColourSyncs
from scadbuddy.filament.component import Filament, FilamentDep
from scadbuddy.filament.csv_import import (
    ImportPreview,
    ImportRefusedError,
    SpoolCsvImport,
    import_on,
    import_rows,
)
from scadbuddy.filament.label_templates import LabelTemplateChoice
from scadbuddy.filament.labels import (
    LabelNotFoundError,
    LabelPayloadTooLongError,
    NoLabelBaseError,
    label_base_url,
    pdf_labels,
)
from scadbuddy.filament.labels import printer_labels as label_logic
from scadbuddy.filament.ledger import BadCursorError
from scadbuddy.filament.locations import (
    LocationCycleError,
    LocationExistsError,
    MoveTargetError,
    NoPrinterKindError,
)
from scadbuddy.filament.operations import KIND as COLOUR_SYNC_KIND
from scadbuddy.filament.operations import subject as colour_sync_subject
from scadbuddy.filament.profile_fetch import (
    PROBE_TIMEOUT_SECONDS,
    PROBLEMS,
    START_TIMEOUT_SECONDS,
    FetchNotStartedError,
    FetchRefusedError,
    ProfileFetchStarted,
    ProfileFetchStatus,
    end_unserved,
    fetches,
    start_fetch,
)
from scadbuddy.filament.profile_fill import (
    BulkFillRequest,
    FillRequest,
    ProfileChangedError,
    TypeFill,
    fill_quietly,
)
from scadbuddy.filament.rfid_mode import read_status, reconcile_mode_now
from scadbuddy.filament.rfid_push import failure_label, push_pending
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
from scadbuddy.filament.tdfp import CREATE_WAIT, SEARCH_WAIT, Tdfp, tdfp
from scadbuddy.filament.types import TypeRowsDuplicateError, TypeTempError
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.operations.component import OperationsDep
from scadbuddy.operations.kinds import waiting_on_bambuddy
from scadbuddy.orders.store import FilamentExistsError
from scadbuddy.printers.component import PrintersDep
from scadbuddy.profile_fetch.wire import (
    CreateAnswer,
    CreateAsk,
    SearchAnswer,
    SearchAsk,
    TdfpOutcome,
)
from scadbuddy.profile_fetch.workflow import NOT_STARTED as TDFP_NOT_STARTED
from scadbuddy.rows.component import ROWS, Rows
from scadbuddy.workflows.commands import (
    TemporalUnavailableError,
    TemporalUnreachableError,
    temporal_failure,
)

#: The write routes, by route name, that `nudge_after_write` skips: the AMS weights
#: come from the printers' reports and leave Bambuddy to the timer, as the capture's do
#: (Decision 17, R-T8-3); "Sync now" and the links' reset (#6773) nudge a full pass
#: themselves; the Bambuddy import pulls
#: and the RFID scan writes nothing, so neither has anything to push (Decision 163), nor
#: has the labels PDF (#7157);
#: the printer labels (#6955) make only the printer and AMS places that capture makes
#: too, un-nudged, and every labels-page load and agent read calls them.
NOT_NUDGED = frozenset(
    {
        "sync_ams_weights",
        "run_filament_sync",
        "reset_filament_sync_links",
        "import_bambuddy",
        "scan_rfid_tag",
        "printer_labels",
        "render_labels_pdf",
    }
)


async def nudge_after_write(request: Request, state: StateDep) -> AsyncIterator[None]:
    """A router's dependency: after a write's 2xx answer, nudge the filament sync in the
    background (Decision 17). A route that raised (any error answer, a 422 of its body
    too) nudges nothing, and neither does a read or a route in `NOT_NUDGED`."""
    yield
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if getattr(request.scope.get("route"), "name", None) in NOT_NUDGED:
        return
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
    if isinstance(exc, CatalogKeyClashError):
        holder: dict[str, Any] = {
            f"{exc.noun}_id": str(exc.holder_id),
            f"{exc.noun}_name": exc.holder_name,
        }
        return ApiError(status.HTTP_409_CONFLICT, str(exc), **holder)
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
        "A material, subtype, brand, category or effect has `aliases`, its other names. "
        "`q` keeps the rows where every word matches some text, in any case: the name, and "
        "for a colour its manufacturer, `material_name` and `#RRGGBBAA` (#6907). "
        "404 for an unknown kind."
    ),
)
async def list_catalog(
    filament: FilamentDep,
    kind: str,
    include_archived: bool = False,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[CatalogRow]:
    _spec(kind)
    rows = await asyncio.to_thread(
        filament.catalog.list, kind, include_archived=include_archived, q=q
    )
    return cast("list[CatalogRow]", rows)


@router.post(
    "/catalog/{kind}",
    response_model=CatalogRow,
    status_code=status.HTTP_201_CREATED,
    summary="Add a row to a filament catalog",
    description=(
        "The fields depend on the kind (every one takes `name` and `sort_order`; materials "
        "add print and drying defaults and handling flags, colours an `rgba`). 404 for an "
        "unknown kind, 409 when a row has the name, 422 for a bad field, a row it points "
        "at that does not exist, or a minimum temperature above its maximum. In the "
        "materials, subtypes, brands, categories and effects catalogs a name matches by its "
        "key (letters and digits, any case: `dual-color` is `Dual Color`) against every "
        "name and alias, archived rows too, a subtype's within its material; the 409 then "
        "names the row that holds it (`<kind>_id`, `<kind>_name`, e.g. `effect_id`); a name "
        "there with no letter or digit is a 422. `+` counts as `PLUS` (`PLA+` is `PLA Plus`, "
        "not `PLA`)."
    ),
)
async def create_catalog_row(
    filament: FilamentDep, kind: str, body: Annotated[dict[str, Any], Body()]
) -> CatalogRow:
    spec = _spec(kind)
    parsed = _body(spec.create, body)
    try:
        made = await asyncio.to_thread(filament.catalog.create, kind, parsed)
    except (CatalogExistsError, CatalogRefError, CatalogKeyEmptyError, TypeTempError) as exc:
        raise _refused(exc) from None
    return _row(made, kind, UUID(int=0))


@router.patch(
    "/catalog/{kind}/{row_id}",
    response_model=CatalogRow,
    summary="Change a row of a filament catalog",
    description=(
        "Only the fields sent change; `null` clears a nullable one. `archived: true` takes "
        "the row off the lists, `false` puts it back; an archived material, subtype, brand, "
        "category or effect keeps its names and aliases, any other row frees its name. 404 "
        "for an unknown kind or row, 409 when another row has the name (by key, as on "
        "create, naming the holder), 422 for a bad field, a name with no letter or digit "
        "(those catalogs), or a minimum temperature above its maximum."
    ),
)
async def patch_catalog_row(
    filament: FilamentDep, kind: str, row_id: UUID, body: Annotated[dict[str, Any], Body()]
) -> CatalogRow:
    spec = _spec(kind)
    parsed = _body(spec.patch, body)
    try:
        changed = await asyncio.to_thread(filament.catalog.update, kind, row_id, parsed)
    except (CatalogExistsError, CatalogRefError, CatalogKeyEmptyError, TypeTempError) as exc:
        raise _refused(exc) from None
    return _row(changed, kind, row_id)


_ALIASED = ", ".join(kind for kind, spec in CATALOGS.items() if spec.names is not None)


def _aliased(kind: str) -> CatalogSpec:
    spec = _spec(kind)
    if spec.names is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"the {kind} catalog has no aliases; these do: {_ALIASED}"
        )
    return spec


@router.put(
    "/catalog/{kind}/{row_id}/aliases",
    response_model=CatalogRow,
    summary="Set the aliases of a catalog row",
    description=(
        "The whole alias list of a material, subtype, brand, category or effect: its other "
        "names, which every name lookup (the Bambuddy import, the CSV import, the price "
        "book) resolves to it. Each is trimmed. 404 for another kind or no such row, 409 "
        "when an alias's key (letters and digits, any case) is another name or alias of "
        "the catalog, the row's own name or an earlier alias of the list (it names the "
        "holder: `<kind>_id`, `<kind>_name`), 422 for an empty alias or one with no "
        "letter or digit. Nothing changes on a refusal."
    ),
)
async def put_catalog_aliases(
    filament: FilamentDep, kind: str, row_id: UUID, body: Annotated[dict[str, Any], Body()]
) -> CatalogRow:
    _aliased(kind)
    parsed = cast("m.AliasesPut", _body(m.AliasesPut, body))
    try:
        changed = await asyncio.to_thread(
            filament.catalog.set_aliases, kind, row_id, list(parsed.aliases)
        )
    except (CatalogExistsError, CatalogKeyEmptyError) as exc:
        raise _refused(exc) from None
    return _row(changed, kind, row_id)


@router.get(
    "/catalog/{kind}/resolve",
    response_model=CatalogRow,
    summary="Find a catalog row by any of its names",
    description=(
        "The material, subtype, brand, category or effect whose name or alias has the key "
        "of `name` (letters and digits, any case), archived or not: what the imports "
        "resolve a name to. A subtype is `material_id`'s own, else one for every "
        "material. 404 for another kind or no match."
    ),
)
async def resolve_catalog_row(
    filament: FilamentDep, kind: str, name: str, material_id: UUID | None = None
) -> CatalogRow:
    _aliased(kind)
    row = await asyncio.to_thread(filament.catalog.resolve, kind, name, material_id)
    if row is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no {kind} row is named {name!r}")
    return cast("CatalogRow", row)


# -- the colour catalog's syncs (#6907) -------------------------------------------------


@router.get(
    "/colour-syncs",
    response_model=list[ColourSync],
    summary="The colour catalog's newest sync of each source",
    description=(
        "The newest run of each colour sync (`filamentcolors`, `bambu`): `state` `running` "
        "(with `pages_read` of `pages_total` and the counts so far), `done`, `unchanged` "
        "(filamentcolors.xyz had no new swatch since the last run, so no page was read) or "
        "`failed` (`error` says why; what it wrote stays). A source never run is absent."
    ),
)
async def list_colour_syncs(
    rows: Annotated[Rows, component_dep(ROWS)],
) -> list[ColourSync]:
    return await asyncio.to_thread(ColourSyncs(rows.pool).latest)


@router.post(
    "/colour-syncs",
    response_model=ColourSync,
    summary="Fill the colour catalog from filamentcolors.xyz or Bambu's colour codes",
    description=(
        "`source` `filamentcolors`: every swatch of https://filamentcolors.xyz/api/swatch/ "
        "(about 23 pages), unless its `GET /api/version/` is unchanged since the last run "
        "(`force` reads them anyway). `bambu`: Bambu Studio's colour codes as Bambu Lab's "
        "colours, alpha as the table gives it (`80` for a translucent one). A colour is "
        "added, or updated by its maker, name and material; a row a person made or edited "
        "is left as it is, and no row is deleted. A failed read answers the run `failed` "
        "with `error`, not an error status. Past the command deadline the answer is 202 "
        "with the operation to follow; `GET /filament/colour-syncs` shows its progress. "
        "409 `colour_sync_running` while a run of that source runs."
    ),
    responses=OPERATION_RESPONSES,
)
async def post_colour_sync(
    body: ColourSyncRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ColourSync | JSONResponse:
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds[COLOUR_SYNC_KIND],
        subject=colour_sync_subject(body),
        request=body,
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, ColourSync)


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
        "(#6209). A type made has no preset, so nothing is filled yet: it is filled from its "
        "slicer profile when its first presets arrive (`PUT /filament/types/{type_id}/presets`, "
        "or a 3DFP preset fetch's import, #6714). 409 when an "
        "unarchived type has the code, 422 for a bad field, a row it "
        "points at that does not exist, or a minimum temperature above its maximum."
    ),
)
async def create_type(filament: FilamentDep, body: m.SpoolTypeCreate) -> m.SpoolType:
    try:
        return await asyncio.to_thread(filament.types.create, body)
    except _TYPE_REFUSED as exc:
        raise _refused(exc) from None


_FILL_DESCRIPTION = (
    "Reads the type's slicer filament profile: of its presets, the H2C 0.4 mm one, else "
    "any 0.4 mm one, else the first. A numeric preset id is Bambuddy's local preset (read "
    "while Bambuddy is linked, over the tree preset it inherits), any other the image's "
    "Bambu Studio preset of that `filament_id` for the printer and nozzle (else its "
    "`@base`) or of that `setting_id`. It maps `nozzle_temperature_range_low/high`, the "
    "H2C plates' temperatures (SuperTack, Engineering, Textured PEI; above 0), "
    "`filament_density`, `filament_diameter` and the AMS HT's idle drying temperature and "
    "time (`filament_dev_ams_drying_temperature`/`_time`, the second value) onto the type, "
    "and fills only its empty fields (`diameter_mm` is never empty); a drying field the "
    "type's subtype or material sets is left empty, said in `skipped`, and only a refresh "
    "offers it. `filament_cost` is "
    "`cost_hint`, never written. A type with no K values takes those of the Bambuddy spools "
    "its spools are linked to (`source` `profile`); then each Bambuddy-linked printer of a "
    "preset's model gives its K calibrations for the preset's nozzle "
    "(`GET /printers/{id}/kprofiles/`, a row of another shape skipped and counted in "
    "`k_reason`), and one whose `filament_id` is the preset's `slicer_filament` fills that "
    "printer, extruder and nozzle (`source` `printer_calibration`) when the type has no row "
    "there, or replaces a `printer_calibration` row there that differs (a `person`, "
    "`profile` or `profile_fetch` row is never overwritten; calibrations of one key that "
    "differ are skipped, said in `k_reason`). "
    "A profile that cannot be read leaves the type alone and says why in `reason`. "
)


@router.post(
    "/types/fill-from-profile",
    response_model=list[TypeFill],
    summary="Fill every spool type from its slicer profile",
    description=(
        _FILL_DESCRIPTION + "Answers each unarchived type: the fields `filled`, their "
        "`source` preset, and any `reason`. `refresh: true` answers, per type, the profile's "
        "values that differ from the type's (`filled`, with `previous`) and writes nothing."
    ),
)
async def fill_types_from_profile(
    filament: FilamentDep, caller: CallerDep, store: SettingsStoreDep, body: BulkFillRequest
) -> list[TypeFill]:
    settings = await asyncio.to_thread(store.load)
    return await filament.filler.fill(settings, None, actor=actor(caller), refresh=body.refresh)


@router.post(
    "/types/{type_id}/fill-from-profile",
    response_model=TypeFill,
    summary="Fill a spool type from its slicer profile",
    description=(
        _FILL_DESCRIPTION + "`refresh: true` answers the profile's values that differ from "
        "the type's, set or not (`filled`, with `previous`), and writes nothing. `apply` "
        "writes those fields, as the refresh showed them, over what the type has, and with "
        "`k_values: true` the K values it showed (sent back as `shown_k_values`; a row's "
        "`type_id` and `source` may be sent and are ignored): the spools' while the type "
        "has none, the printers' into keys it has no row for or an older calibration holds; 409 "
        "when the profile, or Bambuddy's spools or printers, no longer give one of "
        "them, 422 for `k_values: true` without `shown_k_values` (`[]` for none), a field "
        "it does not fill or a "
        "range it would leave backwards. 404 for no such type."
    ),
)
async def fill_type_from_profile(
    filament: FilamentDep,
    caller: CallerDep,
    store: SettingsStoreDep,
    type_id: UUID,
    body: FillRequest,
) -> TypeFill:
    settings = await asyncio.to_thread(store.load)
    if body.apply is not None:
        try:
            applied = await filament.filler.apply(
                settings,
                type_id,
                body.apply,
                actor=actor(caller),
                k_values=body.shown_k_values if body.k_values else None,
            )
        except ProfileChangedError as exc:
            raise ApiError(status.HTTP_409_CONFLICT, str(exc)) from None
        except _TYPE_REFUSED as exc:
            raise _refused(exc) from None
        if applied is None:
            raise _no_type(type_id)
        return applied
    found = await filament.filler.fill(
        settings, [type_id], actor=actor(caller), refresh=body.refresh
    )
    if not found:
        raise _no_type(type_id)
    return found[0]


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
async def patch_type(
    filament: FilamentDep, caller: CallerDep, type_id: UUID, body: m.SpoolTypePatch
) -> m.SpoolType:
    try:
        changed = await asyncio.to_thread(filament.types.update, type_id, body, actor=actor(caller))
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
        "The whole list: one slicer filament per printer model and nozzle diameter. The "
        "first presets of a type that had none fill its empty fields from their profile "
        "(`POST /filament/types/{type_id}/fill-from-profile`). 404 for no such type, 422 "
        "for two rows with the same model and nozzle."
    ),
)
async def put_type_presets(
    filament: FilamentDep,
    caller: CallerDep,
    store: SettingsStoreDep,
    type_id: UUID,
    body: list[m.TypePresetCreate],
) -> list[m.TypePreset]:
    rows = [m.TypePreset(type_id=type_id, **one.model_dump()) for one in body]
    had = await asyncio.to_thread(filament.types.presets, type_id)
    try:
        saved = await asyncio.to_thread(
            filament.types.set_presets, type_id, rows, actor=actor(caller)
        )
    except _TYPE_REFUSED as exc:
        raise _refused(exc) from None
    if saved is None:
        raise _no_type(type_id)
    if saved and not had:
        # A type's first presets come right after it is made (makeTypeFromSpool, the type
        # page): it is filled from them then (R-6714-6), never on a later change.
        await fill_quietly(lambda: filament.filler, store.load, [type_id], actor=actor(caller))
    return saved


@router.put(
    "/types/{type_id}/k-values",
    response_model=list[m.TypeKValue],
    summary="Replace a spool type's pressure-advance values",
    description=(
        "The whole list: one K value per printer, extruder and nozzle diameter. A row it "
        "adds or changes is `source` `person`; a row sent as it was keeps its `source`, so a "
        "later printer calibration may still replace a `printer_calibration` row. 404 for "
        "no such type, 422 for a printer that does not exist or a repeated key."
    ),
)
async def put_type_k_values(
    filament: FilamentDep, caller: CallerDep, type_id: UUID, body: list[m.TypeKValueCreate]
) -> list[m.TypeKValue]:
    rows = [m.TypeKValue(type_id=type_id, **one.model_dump()) for one in body]
    try:
        saved = await asyncio.to_thread(
            filament.types.set_k_values, type_id, rows, actor=actor(caller)
        )
    except _TYPE_REFUSED as exc:
        raise _refused(exc) from None
    if saved is None:
        raise _no_type(type_id)
    return saved


@router.post(
    "/types/{type_id}/profile-fetch",
    response_model=ProfileFetchStarted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Fetch a spool type's slicer presets from 3DFP",
    description=(
        "Starts a fetch of the type's slicer presets from 3dfilamentprofiles.com (its "
        "`tdfp_id`): the profile-fetch worker exports one Bambu Studio preset per printer "
        "model ScadBuddy's printers have and per nozzle in the `profile_fetch_nozzles` "
        "setting, and the API imports them into Bambuddy's local presets and makes them the "
        "type's presets for those keys. It answers 202 at once with the run (`running`); "
        "read its progress with `GET` on this path. A start while a fetch of the type runs "
        "joins it (`joined: true`, the same run) and starts nothing new, unless no "
        "profile-fetch worker serves that run: it then ends `failed` and a new run starts. "
        "404 for no such type, 422 `no_tdfp_id` for a type without a 3DFP id, 422 "
        "`no_printer_models` when no printer has a model, 503 when Temporal did not take the "
        "start within 10 s (the run ends `failed`)."
    ),
)
async def start_type_profile_fetch(
    state: StateDep, caller: CallerDep, type_id: UUID
) -> ProfileFetchStarted:
    try:
        return await start_fetch(state, type_id, actor=actor(caller), timeout=START_TIMEOUT_SECONDS)
    except FetchRefusedError as refused:
        if refused.code == "type_not_found":
            raise _no_type(type_id) from None
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(refused),
            type_=PROBLEMS + refused.code,
            code=refused.code,
        ) from None
    except FetchNotStartedError as exc:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None


@router.get(
    "/types/{type_id}/profile-fetch",
    response_model=ProfileFetchStatus,
    summary="Read a spool type's 3DFP preset fetch",
    description=(
        "The type's last preset fetch from 3dfilamentprofiles.com (`run`, null when it never "
        "had one): its `status` (`running`, `imported`, `partial`, `failed`), the keys it "
        "could not set (`missing`), its notes and error. `gaps` lists each printer model "
        "and nozzle a fetch would export now that the type has no preset for, with the "
        "reason its last fetch gave. A run that no profile-fetch worker serves (none polls "
        "its queue 10 min after it started, or it outlived every timeout) is ended `failed` "
        "on this read. 404 for no such type."
    ),
)
async def get_type_profile_fetch(
    state: StateDep, filament: FilamentDep, type_id: UUID
) -> ProfileFetchStatus:
    if await asyncio.to_thread(filament.types.get, type_id) is None:
        raise _no_type(type_id)
    runs = fetches(state)
    run = await asyncio.to_thread(runs.latest, type_id)
    if run is not None:
        run = await end_unserved(state, run)
    gaps = await asyncio.to_thread(runs.gaps, type_id)
    return ProfileFetchStatus(run=run, gaps=gaps)


# -- 3DFP search and create (#7025) ---------------------------------------------------------

TDFP_PROBLEMS = "https://scadbuddy.dev/problems/tdfp/"
#: The failure kinds that are the ask's fault (422); any other is the site's or the
#: browser's (502), or no worker took the step (503).
_TDFP_REFUSED = frozenset({"option_missing", "no_defaults"})


def _tdfp_problem(status_code: int, code: str, detail: str) -> ApiError:
    return ApiError(status_code, detail, type_=TDFP_PROBLEMS + code, code=code)


def _tdfp_failed(outcome: TdfpOutcome) -> ApiError:
    kind = outcome.kind or "browser"
    detail = outcome.reason or "the 3dfilamentprofiles.com step failed"
    if kind in _TDFP_REFUSED:
        return _tdfp_problem(status.HTTP_422_UNPROCESSABLE_CONTENT, kind, detail)
    if kind == TDFP_NOT_STARTED:
        return _tdfp_problem(status.HTTP_503_SERVICE_UNAVAILABLE, kind, detail)
    return _tdfp_problem(status.HTTP_502_BAD_GATEWAY, kind, detail)


async def _tdfp_worker(runner: Tdfp) -> None:
    """503 ``no_worker`` when Temporal says no profile-fetch worker polls; a Temporal that
    does not answer leaves the call to the step's own timeouts."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            served = await runner.has_worker()
    except Exception:
        logger.warning("could not read the pollers of the profile-fetch queue", exc_info=True)
        return
    if not served:
        raise _tdfp_problem(
            status.HTTP_503_SERVICE_UNAVAILABLE, "no_worker", "no profile-fetch worker is running"
        )


def _tdfp_temporal(
    error: BaseException, what: str, *, may_have_started: bool | None
) -> ApiError | None:
    """A call to Temporal that failed, read as every route reads one (`temporal_failure`,
    R-7025-15, R-7025-16): unavailable or busy (a call that timed out among them), or
    unreached (the lazy client's first connect failed) is 503 `temporal-unavailable` with
    `Retry-After`, worth sending again; a refusal is 500 `temporal-refused`. ``None`` for a
    failure that is not Temporal's, which the caller raises again. A client that never
    connected wrote no request, so nothing can have started (`may_have_started` false where
    the route says it)."""
    failure = temporal_failure(error, what)
    if failure is None:
        return None
    if isinstance(failure, TemporalUnreachableError) and may_have_started is not None:
        may_have_started = False
    if isinstance(failure, TemporalUnavailableError):
        return temporal_unavailable(
            what,
            f"ScadBuddy's background service did not answer for {what}; send it again",
            may_have_started=may_have_started,
        )
    return temporal_refused(
        what, f"ScadBuddy's background service refused {what}", may_have_started=may_have_started
    )


@router.post(
    "/tdfp/search",
    response_model=SearchAnswer,
    summary="Search 3dfilamentprofiles.com",
    description=(
        "Searches 3dfilamentprofiles.com (3DFP) on the profile-fetch worker, the one process "
        "with its browser and login, and answers the first page of filaments: each one's "
        "`tdfp_id`, brand, material, material type, colour name (`name`), `hexes` and the "
        '`nominal_weight_g` its name carries ("(250g)"; null when it carries none). The site '
        "filters only by its `?q=` text, which is the brand, the material type and the colour "
        "(`query`); the material ranks the rows (its matches first, then the type's) and "
        "filters nothing. Its index misses some brands: none found is not proof that 3DFP "
        "lacks the filament. Reads only; ScadBuddy stores nothing. 503 `no_worker` when no "
        "profile-fetch worker runs, 503 `not_started` when the worker, busy with other "
        "searches, did not take the search within a minute (a preset fetch never holds a "
        "search up), 502 when the site or the browser "
        "failed (`code` is the failure's kind; the search is tried once, so send it again), "
        "504 when no outcome came within the wait, 503 `temporal-unavailable` (with "
        "`Retry-After`) when ScadBuddy's background service did not answer, 500 "
        "`temporal-refused` when it refused the call (sending it again will not help)."
    ),
)
async def search_tdfp(state: StateDep, caller: CallerDep, body: SearchAsk) -> SearchAnswer:
    runner = tdfp(state)
    await _tdfp_worker(runner)
    try:
        outcome = await runner.search(body, wait=SEARCH_WAIT)
    except TimeoutError:
        raise _tdfp_problem(
            status.HTTP_504_GATEWAY_TIMEOUT, "no_outcome", "the 3DFP search gave no answer in time"
        ) from None
    except (RPCError, RuntimeError) as error:
        # A TimeoutError here is the wait's (504 above), not Temporal's.
        if (problem := _tdfp_temporal(error, "the 3DFP search", may_have_started=None)) is None:
            raise
        raise problem from None
    if outcome.search is None:
        raise _tdfp_failed(outcome)
    return outcome.search


class TdfpCreateRequest(CreateAsk):
    """``POST /filament/tdfp/filaments``: the filament to add, and the spool type whose
    ``tdfp_id`` the new id becomes (optional)."""

    type_id: UUID | None = None


class TdfpCreated(BaseModel):
    """What the create did (`CreateAnswer`), and whether the type took the new id."""

    create: CreateAnswer
    type_id: UUID | None
    type_tdfp_id_set: bool


@router.post(
    "/tdfp/filaments",
    response_model=TdfpCreated,
    summary="Add a missing filament to 3dfilamentprofiles.com",
    description=(
        "Adds a filament to 3dfilamentprofiles.com (3DFP) on the profile-fetch worker: its "
        "add form gets the brand, material and material type (each must be one the form "
        "offers, else 422 `option_missing`), the colour name, `hexes` (1 to 4), the nominal "
        "weight and the identifiers `mpn`, `gtin`, `asin`, `manufacturer_url`, "
        "`empty_spool_weight_g` and `td` where the form has a field for them (`sent`, "
        "`not_sent`; ScadBuddy stores none of them). A brand, material and type with no site "
        "defaults are refused, 422 `no_defaults`. Before it saves, the form's own duplicate "
        "list is read: a filament there of the same brand, material and type with a close "
        "name or colour (CIEDE2000 within 5) stops the create with `status` `duplicate`, the "
        "list (`duplicates`, `duplicate_text`) and why (`likely`), and nothing is saved; a "
        "person who judges those to be other filaments sends their ids in `not_duplicates`. "
        "Else it saves and answers `status` `created` with the new `tdfp_id`, which becomes "
        "the `tdfp_id` of `type_id` when one is sent. Needs an `Idempotency-Key` (428 "
        "without): the same key, body and `type_id` answer the first create's outcome and "
        "never add a second filament, except after a failure before the save (422, 503 "
        "`not_started`, or a site failure before the save), which the same key tries again. "
        "The create is tried once: 502 `not_created`, or a 502 whose detail says the filament "
        "may exist, means it may exist, so search before sending another key. 404 for no "
        "such type, 409 `type_has_tdfp_id` for a type that already has one. "
        "503 `no_worker`, 503 `not_started` (the worker busy with a preset fetch), 502 "
        "and 504 as for the search; on 504 the "
        "create may still run, and the same request with the same key waits for it; 503 "
        "`temporal-unavailable` and 500 `temporal-refused` as for the search "
        "(`may_have_started` says whether a create may run). The type's `tdfp_id` is set "
        "only while it has none (R-7025-14)."
    ),
)
async def create_tdfp_filament(
    state: StateDep,
    filament: FilamentDep,
    caller: CallerDep,
    body: TdfpCreateRequest,
    idempotency_key: MaybeIdempotencyKey = None,
) -> TdfpCreated:
    who = actor(caller)
    # The caller's key under their own name, and the type the new id goes to: the same key
    # for another type is another create, never the first one's id on it (R-7025-12).
    key = f"{who}\x1f{required_key(idempotency_key)}\x1f{body.type_id or ''}"
    ask = CreateAsk.model_validate(body.model_dump(exclude={"type_id"}))
    runner = tdfp(state)
    if body.type_id is not None:
        kind = await asyncio.to_thread(filament.types.get, body.type_id)
        if kind is None:
            raise _no_type(body.type_id)
        try:
            started = kind.tdfp_id is not None and await runner.started(ask, key=key)
        except (RPCError, RuntimeError, TimeoutError) as error:
            problem = _tdfp_temporal(error, "the 3DFP create's check", may_have_started=False)
            if problem is None:
                raise
            raise problem from None
        if kind.tdfp_id is not None and not started:
            raise _tdfp_problem(
                status.HTTP_409_CONFLICT,
                "type_has_tdfp_id",
                f"spool type {body.type_id} already has 3DFP id {kind.tdfp_id}",
            )
    await _tdfp_worker(runner)
    try:
        outcome = await runner.create(ask, key=key, wait=CREATE_WAIT)
    except TimeoutError:
        raise _tdfp_problem(
            status.HTTP_504_GATEWAY_TIMEOUT,
            "no_outcome",
            "the 3DFP create gave no answer in time; it may still run: send the same request"
            " with the same Idempotency-Key to wait for it",
        ) from None
    except (RPCError, RuntimeError) as error:
        # The start may have reached Temporal before it failed: the same key joins it. A
        # TimeoutError here is the wait's (504 above), not Temporal's.
        if (problem := _tdfp_temporal(error, "the 3DFP create", may_have_started=True)) is None:
            raise
        raise problem from None
    if outcome.create is None:
        raise _tdfp_failed(outcome)
    made = outcome.create
    taken = False
    if made.status == "created" and made.tdfp_id is not None and body.type_id is not None:
        taken = await asyncio.to_thread(set_type_tdfp_id, filament, body.type_id, made.tdfp_id)
    return TdfpCreated(create=made, type_id=body.type_id, type_tdfp_id_set=taken)


def set_type_tdfp_id(filament: Filament, type_id: UUID, tdfp_id: int) -> bool:
    """Set the type's ``tdfp_id`` to the created one, unless it has another by now; whether
    the type holds it. One conditional write (`SpoolTypeStore.set_tdfp_id`, R-7025-14), so
    two creates for one type under different keys never replace each other's id."""
    return filament.types.set_tdfp_id(type_id, tdfp_id)


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


@router.get(
    "/type-proposals",
    response_model=m.TypeProposals,
    summary="Propose spool types for the spools that need one",
    description=(
        "Groups the unarchived spools with no type by brand, material, subtype, RGBA, label "
        "weight and the `material_number` of their last Bambuddy reading (spec §8.1; none "
        "when it is longer than 128 characters). A group with no `material_number` folds into "
        "the one group of its other fields that has one, when exactly one does. Each "
        "group has a proposed `code` (the `material_number`, else `BRAND-MATERIAL-SUBTYPE-"
        "COLOUR`; one another type holds takes `-<label weight>G`), its catalog names, its "
        "spools and their count, and `type_id` when a type matches it now. The largest group "
        "first. `ungrouped` lists the spools that lack a material, an RGBA or a label weight, "
        "with what each lacks. Writes nothing."
    ),
)
async def list_type_proposals(filament: FilamentDep) -> m.TypeProposals:
    return await asyncio.to_thread(type_proposals.proposals, filament)


@router.post(
    "/type-proposals/confirm",
    response_model=m.TypeProposalResult,
    summary="Make the proposed spool types and type their spools",
    description=(
        "Each group is a proposal's key, the spools it named and the `code` to give the "
        "type. In one transaction, per group: a type that matches the key now is used, else "
        "the type is made (the key, the most common colour name, and each other value every "
        "spool shares; no cost). Each named spool that is still unarchived, untyped and of "
        "the key gets the type, loses each override equal to the type's value, and gets one "
        "`edited` event under the caller's name. Any other spool is in `skipped` with its "
        "reason (`not_found`, `archived`, `typed`, `changed`). Per group, `made` says it "
        "made the type and `new` that this request made it: `new` without `made` means the "
        "group joined a type an earlier group just made, so its `code` was not used. 409 "
        "when an unarchived type "
        "has one of the codes (nothing is written), 422 for a bad body."
    ),
)
async def confirm_type_proposals(
    filament: FilamentDep, caller: CallerDep, body: m.TypeProposalConfirm
) -> m.TypeProposalResult:
    try:
        return await asyncio.to_thread(type_proposals.confirm, filament, body, actor=actor(caller))
    except CatalogExistsError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "a spool type already has one of these codes; choose another name",
        ) from None
    except _SPOOL_REFUSED as exc:
        raise _refused(exc) from None


IMPORT_ROUTE = "POST /filament/spools/import"


@router.post(
    "/spools/import",
    response_model=ImportPreview,
    summary="Import spools from CSV rows",
    description=(
        "Rows of a spools CSV, parsed by the caller (cells as text, under the export's column "
        "names). A row with a `number` changes that spool, only where a cell differs from what "
        "it shows now; an empty cell changes nothing. A row with no `number` makes a spool. "
        "Material, subtype, brand, category and effect names resolve by key (any case and "
        "punctuation) against every name and alias, archived rows too, and are added to their "
        'catalog when missing (a name with no letter or digit is skipped, marked `{"skipped": '
        "...}`, and the row goes on); `insert_type` and `type` (a spool type's code) must "
        "exist; `location` must name exactly one unarchived place and is a move. Derived "
        "columns (`grams_left`, `last_used_at`, ...) are ignored; any other unknown column is a"
        " 422. `dry_run: true` answers what each row would do (`create`, `update` or `error`, "
        "with its changes as `[before, after]`) and writes nothing. `dry_run: false` writes "
        "every row in one transaction, and needs an `Idempotency-Key` (428 without one): a "
        "retry with the same key answers the first result and writes nothing, and the key with "
        "another body is a 422. A write with any row in error is a 422 naming each line, and "
        "writes nothing."
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
        "`spools` are the ones booked, and `stale_printers` names (`id`, `name`) each "
        "printer whose spools are in `stale`."
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
        "Only a spool that never printed, has no Bambuddy link and is not being created in "
        "Bambuddy: its ledger and tag bindings go with it. 404 for no such spool, 409 when "
        "its ledger holds a print, Bambuddy links it, or the sync's create of it in "
        "Bambuddy is open (failed or unanswered ones included; the next pull would make "
        "it again); archive it instead."
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
        "reading: `grams_left` starts again from it. `dried` takes `temp_c` and `hours`; "
        "one left out is the spool's `drying_defaults` value (its type's, else its "
        "subtype's, else its material's), and `null` records none. "
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
        "many are low on stock, how many still need a type, and `consumed_g`: the grams "
        "of every spool's plates, archived ones too, a plate SB and Bambuddy both booked "
        "counted once."
    ),
)
async def get_summary(filament: FilamentDep) -> m.InventorySummary:
    return await asyncio.to_thread(filament.spools.summary)


#: The longest range the metrics routes read: three years, one leap day included.
METRICS_MAX_DAYS = 3 * 365 + 1


def _range(since: date | None, until: date | None, default_days: int) -> tuple[date, date]:
    """``until`` defaults to today (UTC), ``since`` to ``default_days`` before it. 422
    when ``since`` is after ``until`` (a single day, ``since == until``, is allowed) or
    the range passes `METRICS_MAX_DAYS`."""
    until = datetime.now(UTC).date() if until is None else until
    since = until - timedelta(days=default_days) if since is None else since
    if since > until:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "since must not be after until")
    if (until - since).days > METRICS_MAX_DAYS:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the range is longer than {METRICS_MAX_DAYS} days (three years)",
        )
    return since, until


@router.get(
    "/spools/{spool_id}/history",
    response_model=mm.SpoolHistory,
    summary="A spool's history and metrics",
    description=(
        "The spool's timeline, newest first by `(at, seq)`: each ledger event (its "
        "`actor` and `data.author` say who made it) with `order_id`, the order whose "
        "attempt printed the plate, and `archive_id`, the Bambuddy print archive it "
        "matched. `series` is the grams left after each event, oldest first, the readings "
        "the grams anchored on marked `anchor` (at most 500 points). `metrics` is the "
        "spool's use, waste, cost used and value left (in `currency`, the shop's), burn "
        "rate over the last `window_days` and empty day, days since opened and last use, "
        "hours outside dry storage and AMS error; `type_ams_error` is the AMS error over "
        "every spool of its type. 404 for no such spool."
    ),
)
async def get_spool_history(filament: FilamentDep, spool_id: UUID) -> mm.SpoolHistory:
    found = await asyncio.to_thread(filament.metrics.history, spool_id)
    if found is None:
        raise _no_spool(spool_id)
    return found


@router.get(
    "/forecast",
    response_model=mm.Forecast,
    summary="When spools and types run out",
    description=(
        "Each unarchived spool's burn rate (grams per day over the last `window_days`, the "
        "setting `filament_burn_window_days`) and the day it runs out, and each spool "
        "type's stock, burn over all its spools, days of stock, empty day and reorder day "
        "(the empty day less its lead time and safety margin, else the settings' "
        "`filament_lead_time_days` and `filament_safety_margin_days`). `reorder_now` is a "
        "reorder day today or past. A spool or type with no use in the window has no "
        "burn, no empty day and no reorder day."
    ),
)
async def get_forecast(filament: FilamentDep) -> mm.Forecast:
    return await asyncio.to_thread(filament.metrics.forecast)


@router.get(
    "/metrics",
    response_model=mm.InventoryMetrics,
    summary="Stock, consumption and waste",
    description=(
        "The unarchived spools' stock (spools, grams left and value in `currency`, the "
        "shop's) by `dimension` (`material`, `brand`, `type`, `supplier` or `location`; "
        "default `material`); the grams used and wasted (failed and cancelled plates) "
        "per `bucket` (`week` from Monday or `month`; default `month`) and dimension, from "
        "`since` to `until`, both included (default the 180 days before today); and the "
        "waste by material, type and printer with its rate. Archived spools' plates "
        "count. A key is the id, `null` when none is set. 422 when `since` is after "
        "`until` or the range is longer than three years."
    ),
)
async def get_metrics(
    filament: FilamentDep,
    dimension: mm.Dimension = "material",
    bucket: mm.Bucket = "month",
    since: date | None = None,
    until: date | None = None,
) -> mm.InventoryMetrics:
    start, end = _range(since, until, 180)
    return await asyncio.to_thread(filament.metrics.inventory, dimension, bucket, start, end)


@router.get(
    "/metrics/purchasing",
    response_model=mm.PurchasingMetrics,
    summary="Price per kg and supplier spend",
    description=(
        "Each spool type's price per kg at each supplier over time, one series per "
        "currency, from its price points; and the confirmed invoices' totals per "
        "supplier, month and currency, never converted. From `since` to `until`, both "
        "included (default the 365 days before today). 422 when `since` is after "
        "`until` or the range is longer than three years."
    ),
)
async def get_purchasing_metrics(
    filament: FilamentDep, since: date | None = None, until: date | None = None
) -> mm.PurchasingMetrics:
    start, end = _range(since, until, 365)
    return await asyncio.to_thread(filament.metrics.purchasing, start, end)


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
        "(`tag` or `link`). An untagged tray whose placed spool does not match the "
        "reported material and colour, where Bambuddy assigns no spool that does, carries "
        "`mismatch` (what the printer reports; #6713). Archived spools only with "
        "`include_archived`. While Bambuddy is "
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
        "tray or that AMS unit (a `printer_id` and an `ams_id` with no `tray_id`); 422 for "
        "a bad field or a row it points at that does not exist."
    ),
)
async def create_location(filament: FilamentDep, body: m.LocationCreate) -> m.Location:
    try:
        return await asyncio.to_thread(filament.locations.create, body)
    except _LOCATION_REFUSED as exc:
        raise _refused(exc) from None


@router.get(
    "/locations/{location_id}",
    response_model=m.LocationDetail,
    summary="A location's page",
    description=(
        "The place with its `code` (`L-12`), its kind's name, its `path` (the places above "
        "it, root first), its unarchived `children` and the unarchived `spools` in it. An "
        "archived place still answers. 404 for no such place."
    ),
)
async def get_location(filament: FilamentDep, location_id: UUID) -> m.LocationDetail:
    found = await asyncio.to_thread(filament.locations.detail, location_id)
    if found is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no location {location_id}")
    return found


@router.post(
    "/printer-labels",
    response_model=m.PrinterLabels,
    summary="The labels of the printers' places",
    description=(
        "For each printer (the `printer_ids`, or every printer when null; by name): its own "
        "place, each AMS unit with its slots, and the external holder, in that order, each "
        "with its `code`, `name` and `depth` (0 printer, 1 unit or external holder, 2 "
        "slot); with `base_url`, the address a label's QR opens (`filament_label_base_url`, "
        "else `public_url`, else null). It makes a printer's place when it has none, and "
        "the place of each AMS unit, slot and external holder the printer's latest report "
        "lists, so it is a POST; `no_report` names the printers with no report that lists "
        "a unit or a holder (their slots appear once a spool has been loaded). "
        "409 `no_printer_kind` when no location kind is marked as a printer, "
        "422 for an unknown printer id."
    ),
)
async def printer_labels(
    filament: FilamentDep, store: SettingsStoreDep, body: m.PrinterLabelsRequest
) -> m.PrinterLabels:
    try:
        labels, no_report = await asyncio.to_thread(
            label_logic, filament.locations, body.printer_ids
        )
    except NoPrinterKindError as exc:
        raise ApiError(status.HTTP_409_CONFLICT, str(exc), code="no_printer_kind") from None
    except CatalogRefError as exc:
        raise _refused(exc) from None
    read = await asyncio.to_thread(store.read, {"filament_label_base_url", "public_url"})
    return m.PrinterLabels(base_url=label_base_url(read), labels=labels, no_report=no_report)


@router.get(
    "/label-templates",
    response_model=m.LabelTemplates,
    summary="The label templates",
    description=(
        "Each template a label can take (#7157): its `id`, `name`, label `width_mm` and "
        "`height_mm`, and for a sheet of labels its `sheet` (`page` `a4` or `letter`, "
        "`cols`, `rows`, `top_mm`, `left_mm`, `col_gap_mm`, `row_gap_mm`). `custom` takes "
        "any size within its bounds, one label a page. The setting `filament_label_template` "
        "holds the one chosen."
    ),
)
async def list_label_templates() -> m.LabelTemplates:
    return m.LabelTemplates(
        templates=list(label_templates.TEMPLATES),
        custom=m.CustomLabelBounds(
            min_width_mm=label_templates.MIN_WIDTH_MM,
            min_height_mm=label_templates.MIN_HEIGHT_MM,
            max_width_mm=label_templates.MAX_LENGTH_MM,
            max_height_mm=label_templates.MAX_LENGTH_MM,
        ),
    )


@router.post(
    "/labels",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "The labels PDF."}},
    summary="The labels PDF",
    description=(
        "A PDF of the labels of `spool_ids`, then `location_ids` (1 to 100 in all, in the "
        "order sent; #7157), as Bambuddy's `POST /inventory/labels` gives. Each label "
        "carries what the Labels page draws: a spool's number, material, colour name and "
        "brand with the QR code `scadbuddy:spool:<id>`; a place's code and name with the QR "
        "code of `<label address>/x/L-<n>`. `template` (else the setting "
        "`filament_label_template`) sets the size; `layout` `roll` is one label a page and "
        "`sheet` packs them on Letter paper, while a sheet template always prints on its "
        "own grid. It reads ScadBuddy's own rows only and writes nothing. 404 for an "
        "unknown spool or place, 409 `no_label_base` for a place label with no label "
        "address and no public URL, 422 for a bad body or a place address too long for "
        "its QR code."
    ),
)
async def render_labels_pdf(
    filament: FilamentDep, store: SettingsStoreDep, body: m.LabelsPdfRequest
) -> Response:
    read = await asyncio.to_thread(
        store.read, {"filament_label_template", "filament_label_base_url", "public_url"}
    )
    choice = body.template or read.get("filament_label_template") or LabelTemplateChoice()
    try:
        labels = await asyncio.to_thread(
            pdf_labels,
            filament.spools,
            filament.locations,
            body.spool_ids,
            body.location_ids,
            label_base_url(read),
        )
    except LabelNotFoundError as exc:
        raise ApiError(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except NoLabelBaseError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "A place label needs an address: set the label address in Settings, or the public URL.",
            code="no_label_base",
        ) from None
    except LabelPayloadTooLongError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    pdf = await asyncio.to_thread(label_pdf.render_pdf, labels, choice.resolved(), body.layout)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="labels.pdf"',
            "Cache-Control": "no-store",
        },
    )


@router.patch(
    "/locations/{location_id}",
    response_model=m.Location,
    summary="Change a location",
    description=(
        "Only the fields sent change; `null` clears a nullable one. `archived: true` takes "
        "the place off the tree and frees its name, `false` puts it back. 404 for no such "
        "place, 409 for a name, tray or AMS unit taken, or a `parent_id` that would make "
        "the place its own ancestor; 422 for a bad field."
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
        "The unarchived humidity and temperature sensors of the unarchived places, by name; "
        "of one place with `location_id`. A sensor on an archived place is left out. Each "
        "carries its last stored value and time, `reachable` "
        "(false when its last read had no number), `alerting` (the last value is "
        "outside `alert_below` / `alert_above`) and `last_notification`: the newest "
        "notification it queued when it started or stopped alerting with `notify` on "
        "(`kind`, the reading, `state`, `channel` `bambuddy`, how many Bambuddy channels "
        "took it, and `error` when it failed), or null."
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
        "`alert_above`) and `notify` (send a notification through Bambuddy's channels "
        "when it starts and when it stops alerting). An `ams` sensor is made by ScadBuddy from the "
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


#: The import's 503 when the route did not see the print worker's read end: it may still
#: be pulling, so a person checks before importing again (Decision 183).
IMPORT_UNFINISHED = (
    "the print worker did not finish the import in time; it may still be running, so check"
    " the spools before importing again"
)


@router.post(
    "/bambuddy/import",
    response_model=PullReport,
    summary="Import Bambuddy's spools",
    description=(
        "Read Bambuddy's spools, archived ones too, and pull them into ScadBuddy's own, once "
        "(the spool "
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
        "A Bambuddy spool Bambuddy archived before any pull linked it becomes an archived "
        "spool (Bambuddy's archive time, what is left, its usage rows), counted in `created` "
        "and in `archived_created`; Bambuddy archiving a linked spool changes nothing. "
        "The print worker reads Bambuddy and ScadBuddy pulls what it read, so the import "
        "waits for it (at most 4.5 minutes); 503 when the worker did not finish it in time "
        "(it may still be running: check the spools before importing again); 502 when "
        "ScadBuddy refused what the worker read (importing again gets the same answer); 409 "
        "when the Bambuddy URL changed during the import. "
        "The ledger events name the caller. Answers the counts and ids made, linked, changed, "
        "still needing a type and archived, the BB spool ids it could not pull, the count "
        "(and ids) of BB rows it could not read, and the duplicates it skipped. 409 when no "
        "Bambuddy is configured, and 409 with `code` `instance_changed` (reading nothing) "
        "while the links belong to another Bambuddy (`GET /filament/sync/status` "
        "`instance_changed`) until `POST /filament/sync/reset-links`; Bambuddy's own errors "
        "as they come."
    ),
)
async def import_bambuddy(
    state: StateDep, filament: FilamentDep, store: SettingsStoreDep, caller: CallerDep
) -> PullReport:
    """The print worker reads Bambuddy (``FilamentBbPull``, #6772) and the CRUD routes pull
    what it read; this route calls no Bambuddy. Bambuddy's refusal of the spool listing is
    answered as it comes, as when the API read it. The caller is kept under the run's id
    for the routes to name (`remember_actor`, Decision 181), never sent to the worker."""
    settings = await asyncio.to_thread(store.load)
    if not settings.bambuddy_url:
        raise not_configured("no Bambuddy URL is configured; set one in Settings")
    sync = (await asyncio.to_thread(store.read, {"filament_sync"}))["filament_sync"]
    # A person's import pulls the spools whatever `spools` says; the rest follow the setting.
    pulls = dataclasses.replace(Pulls.of(sync), spools=True)
    configured = instance_for(settings.bambuddy_url)
    if await asyncio.to_thread(links_elsewhere, filament, configured):
        # #6845 R-6845-5: the links belong to another Bambuddy; a pull would write its
        # values onto them.
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "the Bambuddy links belong to another Bambuddy; reset them first",
            code="instance_changed",
        )
    start = PullStart(
        instance=configured,
        pull_id=new_pull_id(),
        pulls=pulls,
        strict=True,
    )
    await asyncio.to_thread(remember_actor, filament, start.pull_id, actor(caller))
    nudger = state.filament_sync
    try:
        outcome = await execute_pull(nudger.client_getter(), nudger.task_queue, start)
    except Exception as error:
        # The run did not answer (timed out, Temporal out of reach): its read may still be
        # sending bodies, so the person stays kept for them; a later `remember_actor` drops the
        # row once it is `ACTOR_KEPT` old.
        logger.warning("the Bambuddy import did not finish", extra={"error": type(error).__name__})
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, IMPORT_UNFINISHED) from error
    if outcome.outcome == "failed" and outcome.error is None:
        # The read raised or timed out (Temporal's timeout does not stop it): kept as above.
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, IMPORT_UNFINISHED)
    # The read answered, so no body of this run is still to come (Decision 183).
    try:
        await asyncio.to_thread(forget_actor, filament, start.pull_id)
    except Exception as error:
        # A row left is deleted by a later import once it is `ACTOR_KEPT` old.
        logger.warning(
            "could not drop an import's kept actor", extra={"error": type(error).__name__}
        )
    if outcome.error is not None:
        raise outcome.error.error()
    if outcome.report is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, IMPORT_UNFINISHED)
    return outcome.report


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


@router.get(
    "/sync/status",
    response_model=SyncStatus,
    summary="Bambuddy sync status",
    description=(
        "How the ScadBuddy-to-Bambuddy sync stands. `directions` is the stored "
        "`filament_sync`: one of `off`, `sb_to_bb`, `bb_to_sb` or `two_way` for each of the "
        "six data types `spools`, `weights`, `tags`, `presets`, `suppliers` and "
        "`locations`; the sync writes Bambuddy only for a type set to `sb_to_bb` or "
        "`two_way`, and never for `tags`, which reach Bambuddy through the spool refresh's "
        "tag drain. `instance` is the Bambuddy the links belong to, and `instance_changed` "
        "whether that is not the configured Bambuddy: the sync is then stopped until "
        "`POST /filament/sync/reset-links` resets the links. `last_pass_at` is the "
        "last pass, `last_pass_started_at` when it started (by the worker's clock; a result "
        "from an older worker stores the database's `now()` instead), "
        "`last_error` its error label (null when it worked), and `last_ok_at` the "
        "last pass with no error, a skipped one too. `counts` is the last pass's: `written`, "
        "`failed`, `deferred`, `conflicts` and `unmapped_k`. `errors` holds one row per item "
        "whose last write failed (Bambuddy refused it or did not answer) or was held back "
        "(`name_taken`: another Bambuddy row has its name, so ScadBuddy never sent it): its "
        "`kind` (`spool`, `supplier`, `location`, `insert_type` or `filament`), its ScadBuddy "
        "id (`sb_id`), its `label` (a spool's `#N`, a spool type's code, any other row's "
        "name; null when the item is gone), the error label, the `attempts` and when (`at`), "
        "newest first; a later write that works drops the row. `skipped` says why no pass "
        "runs now: "
        "`no_bambuddy` (no Bambuddy is configured) or `nothing_pushes` (no data type but "
        "`tags` is set to push); else null. Reads ScadBuddy only."
    ),
)
async def get_filament_sync_status(filament: FilamentDep, store: SettingsStoreDep) -> SyncStatus:
    read = await asyncio.to_thread(store.read, {"bambuddy_url", "filament_sync"})
    sync = read["filament_sync"]
    url = read["bambuddy_url"]
    return await asyncio.to_thread(
        sync_status, filament, sync, skip_reason(url, sync), _configured(url)
    )


@router.get(
    "/sync/passes",
    response_model=SyncPassPage,
    summary="Bambuddy sync pass history",
    description=(
        "The ScadBuddy-to-Bambuddy sync's passes (#6905), newest first by when each ended, "
        "failed ones too; `GET /filament/sync/status` holds the last one. Each has its `id`, "
        "the Bambuddy `instance` it served, its `trigger` (`timer`: the interval ran out; "
        "`change`: a write's nudge; `full`: a full pass asked for, such as Sync now; `more`: "
        "the pass before it left writes; null when the worker did not say), `started_at` (by "
        "the worker's clock) and `ended_at` (the server's), the counts `written`, `failed`, "
        "`deferred`, `conflicts` and `unmapped_k`, `skipped` (`no_bambuddy` or "
        "`nothing_pushes`), `instance_changed` (the links or the pass's instance were another "
        "Bambuddy's, so it wrote nothing) and `error` (a label, null when it worked; "
        "`crud_unavailable:<failure>` or `crud_refused:<status>` when a call to ScadBuddy's "
        "own API failed, `internal` for any other failure outside Bambuddy). A page holds up "
        f"to `limit` passes (at most {PAGE_MAX}); `next_cursor` is opaque, and passing it "
        "back as `cursor` gives the next page with no row lost or repeated. 422 for a cursor "
        "no page gave. Rows older than the setting `filament_sync_pass_ttl_days` (30 by "
        "default) are deleted. Reads ScadBuddy only."
    ),
)
async def list_filament_sync_passes(
    filament: FilamentDep,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = 20,
) -> SyncPassPage:
    try:
        return await asyncio.to_thread(filament.sync_passes.page, cursor=cursor, limit=limit)
    except BadPassCursorError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None


def _configured(url: str | None) -> str | None:
    """The configured Bambuddy's instance, ``None`` with none configured."""
    return instance_for(url) if url else None


async def _listing(settings: StoredSettings) -> tuple[Listing, bool]:
    """The configured Bambuddy's spools, archived ones too, parsed one by one, for the
    proof that it is the links' own Bambuddy (#6845 R-6845-4), and whether it could not
    be read. ``None`` with no Bambuddy configured, or when the read failed: then the
    preview offers the delete only and a reset that expects the links kept is refused
    (Decision 198)."""
    if not settings.bambuddy_url:
        return None, False
    try:
        async with client_for(settings) as client:
            rows = await client.spool_rows(include_archived=True)
    except ApiError as failed:
        logger.warning(
            "the configured Bambuddy's spools could not be read for the links' proof",
            extra={"status": failed.status},
        )
        return None, True
    return parse_rows(rows), False


@router.get(
    "/sync/reset-links",
    response_model=LinksResetPreview,
    summary="What a reset of the Bambuddy links would delete",
    description=(
        "The dry run of `POST /filament/sync/reset-links` (#6773): `instance` is the "
        "Bambuddy the links belong to (null when none is recorded), the value "
        "`confirm_instance` must name; `configured_instance` is the configured Bambuddy's "
        "(null with none). `counts` is what a reset would delete now: the spool links "
        "(`spools`), the supplier, location, insert type and spool type links "
        "(`suppliers`, `locations`, `insert_types`, `filaments`: each row's Bambuddy id, "
        "hashes and seen copy), the open create `intents`, the `errors` rows and the "
        "Bambuddy spools the pull `ignored`. `same_bambuddy` (#6845) is true when the "
        "configured Bambuddy is the one the links were made against: it holds every linked "
        "spool, none older than ScadBuddy last saw it, at least one exactly as then, and at "
        "least half the links have a time on both sides. A reset with `expect_kept` true "
        "then deletes nothing and keeps every link; when it is false a reset deletes the "
        "counts, and the next passes link again only what the pull matches (a spool by its "
        "RFID tag, a supplier, location or insert type by its name), so every untagged "
        "spool is made again on the side its direction sends it to. Reads ScadBuddy and "
        "the configured Bambuddy's spools (`GET`). When Bambuddy cannot be read the answer "
        "is still 200, with `same_bambuddy` false and `bambuddy_unreadable` true. "
        "`changed_since_pull` is true when `same_bambuddy` is false although the "
        "configured Bambuddy holds every linked spool and none is older than its link, and "
        "at least half the links have a time on both sides (Decision 199): every linked "
        "spool ScadBuddy could compare (a time on both sides) was edited in "
        "Bambuddy since the last pull, so none matches exactly and ScadBuddy cannot prove "
        "it is the same Bambuddy; only the delete is "
        "then offered. The "
        "counts are approximate: they are read without a lock, so a pass that runs "
        "meanwhile can change them. The answer of the `POST` gives the counts it deleted."
    ),
)
async def preview_filament_sync_reset(
    filament: FilamentDep, store: SettingsStoreDep
) -> LinksResetPreview:
    settings = await asyncio.to_thread(store.load)
    listing, unreadable = await _listing(settings)
    return await asyncio.to_thread(
        partial(
            reset_preview,
            filament,
            _configured(settings.bambuddy_url),
            listing,
            unreadable=unreadable,
        )
    )


@router.post(
    "/sync/reset-links",
    response_model=LinksResetDone,
    summary="Reset the Bambuddy links after the Bambuddy URL changed",
    description=(
        "`expect_kept` is the dry run's `same_bambuddy` (#6845). When it is true, it first "
        "reads the configured Bambuddy's spools (`GET`) and checks again that it is the "
        "one the links were made against; then it deletes nothing: the links, intents and "
        "error rows stay, they now belong to the configured Bambuddy, and the answer has "
        "`kept` true and zero counts. When that read fails or no longer proves it, the "
        "answer is 409 `proof_changed` and nothing is written: read the dry run again. "
        "When `expect_kept` is false (the default), the person confirmed the delete: it "
        "reads no Bambuddy and deletes, in one transaction, what "
        "the sync keeps of the Bambuddy the links belong to (#6773): every spool link, the "
        "supplier, location, insert type and spool type links (their Bambuddy ids, hashes "
        "and seen copies), the open create intents, the error rows, the Bambuddy spools "
        "the pull ignores, and the recorded instance. No ScadBuddy spool, type, supplier or "
        "location is deleted, and none of their own fields changes. Either way it answers "
        "the instance, `kept` and the counts deleted, then nudges a full pass. After a "
        "delete the configured Bambuddy is linked afresh: the pull links its spools by "
        "RFID tag and its suppliers and locations by name, and the rest are made again "
        "(an untagged spool is duplicated on the side its direction sends it to). "
        "`confirm_instance` must be the links' instance (`GET "
        "/filament/sync/reset-links` `instance`), so a stale page cannot reset another "
        "Bambuddy's links. 409 with `code` `confirm_mismatch` when it is not, "
        "`same_instance` when the links belong to the configured Bambuddy (nothing to "
        "reset), `nothing_to_reset` when no instance is recorded, and `proof_changed` as "
        "above."
    ),
)
async def reset_filament_sync_links(
    filament: FilamentDep, store: SettingsStoreDep, state: StateDep, body: LinksResetRequest
) -> LinksResetDone:
    settings = await asyncio.to_thread(store.load)
    listing = (await _listing(settings))[0] if body.expect_kept else None
    try:
        done = await asyncio.to_thread(
            partial(
                reset_links,
                filament,
                body.confirm_instance,
                _configured(settings.bambuddy_url),
                listing,
                expect_kept=body.expect_kept,
            )
        )
    except ResetRefusedError as refused:
        detail = {
            "nothing_to_reset": "no Bambuddy instance is recorded, so no link belongs to one",
            "same_instance": "the links belong to the configured Bambuddy; nothing to reset",
            "confirm_mismatch": "confirm_instance is not the instance the links belong to",
            "proof_changed": (
                "the configured Bambuddy could not be read, or is no longer proven the one"
                " the links were made against; nothing was changed, so read the dry run again"
            ),
        }[refused.code]
        raise ApiError(status.HTTP_409_CONFLICT, detail, code=refused.code) from None
    state.filament_sync.nudge_soon(full=True)
    return done


@router.post(
    "/sync/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SyncRun,
    summary="Sync with Bambuddy now",
    description=(
        "Asks for a full sync pass now: the print worker reads Bambuddy, then writes it only "
        "for the data types `filament_sync` sets to push (`sb_to_bb` or `two_way`); a type "
        "set to `off` or `bb_to_sb` is never written, and nor is `tags` (the spool "
        'refresh\'s tag drain sends tags). Answers 202 `{"started": true, "requested_at": ...}` '
        "once the pass is asked for (`requested_at` is the server's time; the pass for this "
        "request has `last_pass_started_at` at or after it); it runs in the background, and "
        "`GET /filament/sync/status` shows how it went. 409 with `code` `no_bambuddy` when "
        "no Bambuddy is configured, and `nothing_pushes` when no data type but `tags` is set "
        "to push. "
        "503 when the pass could not be asked for (the settings read or the workflow service "
        "failed, or the two took over 5 seconds); `detail` ends in an error label "
        "(`internal` for any failure outside Bambuddy), and nothing started."
    ),
)
async def run_filament_sync(state: StateDep) -> SyncRun:
    # Before the nudge, so a pass it starts has begun at or after this (#6836).
    requested_at = datetime.now(UTC)
    try:
        reason = await state.filament_sync.nudge(full=True, raise_errors=True)
    except Exception as exc:
        # R-T10-2: no 202 for a pass that was not asked for. The label, never the message.
        label = failure_label(exc)
        logger.warning("could not start the filament sync", extra={"error": label})
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"could not ask the sync workflow for a pass: {label}",
        ) from None
    if reason == "no_bambuddy":
        raise ApiError(status.HTTP_409_CONFLICT, "no Bambuddy is configured", code=reason)
    if reason == "nothing_pushes":
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "no data type the sync pushes is set to push to Bambuddy (filament_sync; tags"
            " go with the refresh)",
            code=reason,
        )
    return SyncRun(requested_at=requested_at)


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
