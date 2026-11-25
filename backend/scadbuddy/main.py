from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Final

from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import iter_route_contexts
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError
from temporalio.worker import Worker

import scadbuddy.api
from scadbuddy import __version__
from scadbuddy.activity.component import ACTIVITY
from scadbuddy.activity.index import (
    ACTIVITY_PRUNE_MARGIN,
    ACTIVITY_REPAIR_WINDOW_DEFAULT,
    ACTIVITY_TTL_DEFAULT,
)
from scadbuddy.agent_data.rotation import kek_at
from scadbuddy.agent_data.rotation import rotate as rotate_agent_secrets
from scadbuddy.api import (
    crud_agent_events,
    crud_agent_packages,
    crud_agent_transcripts,
    crud_agent_turns,
    crud_print,
    entity_attachments,
    health,
    internal_blobs,
    internal_library,
    libraries,
    library_print,
    media,
    mesh_models,
    metrics,
    models,
    order_attachments,
    outputs,
    print_attachments,
    tasks,
    telemetry,
)
from scadbuddy.api.accepting import install_accepting_handler, note_preference
from scadbuddy.api.compression import Compression
from scadbuddy.api.cross_site import CrossSiteGate
from scadbuddy.api.deps import STATE_ATTR, AppState, build_state
from scadbuddy.api.limits import (
    ATTACHMENT_UPLOAD_PATH,
    BODY_LIMITS,
    ENTITY_ATTACHMENT_UPLOAD_PATH,
    INVOICE_FILE_UPLOAD_PATH,
    LIBRARY_UPLOAD_PATH,
    MEDIA_UPLOAD_PATH,
    MESH_UPLOAD_PATH,
    ORDER_ATTACHMENT_UPLOAD_PATH,
    BodySizeGate,
    RouteLimit,
)
from scadbuddy.api.printing import end_gone_sequences
from scadbuddy.api.realtime import EventCursor
from scadbuddy.api.runtime import apply_runtime, follow_changes
from scadbuddy.api.static import SPAStaticFiles
from scadbuddy.attachments.component import PRINT_ATTACHMENTS
from scadbuddy.auth.authorship_rule import AuthorshipRule
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.revoke import RetiredState, retired_state, revoke_retired
from scadbuddy.auth.start_check import check_enforce
from scadbuddy.bambuddy.attachments import sweep_gone_archives
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.errors import NOT_CONFIGURED_PROBLEM
from scadbuddy.bambuddy.follow import FollowActivities
from scadbuddy.bambuddy.lost_slices import sweep_lost_slices
from scadbuddy.bambuddy.output_reader import ContentBlobs, LocalOutputs
from scadbuddy.bambuddy.preview import PREVIEW_GRACE, sweep_preview_slices
from scadbuddy.bambuddy.print_source import MAX_DOWNLOAD_BYTES
from scadbuddy.bambuddy.runs import PrintRunStore, reap_run_holds
from scadbuddy.batches.component import PRINT_BATCHES
from scadbuddy.core.authorship import AgentAuthorship
from scadbuddy.core.logging import configure_logging
from scadbuddy.core.loop_watch import LoopWatch
from scadbuddy.core.metrics import HttpMetrics
from scadbuddy.core.paths import BUILTIN_DIR, MODEL_META_NAME
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.problems import ApiError, install_problem_handlers
from scadbuddy.core.settings import Settings
from scadbuddy.core.tracing import configure_tracing
from scadbuddy.crud import idempotency
from scadbuddy.crud import listener as crud_listener
from scadbuddy.entities.component import ENTITIES
from scadbuddy.entities.sweep import sweep_orphan_entities
from scadbuddy.filament.bb_pull_apply import LocalBbPull, execute_pull
from scadbuddy.filament.bb_pull_run import FilamentPullActivities
from scadbuddy.filament.bb_sync_nudge import instance_for
from scadbuddy.filament.bb_sync_results import InProcessFilamentSync, adopt_instance
from scadbuddy.filament.bb_sync_run import FilamentSyncActivities, settings_loader
from scadbuddy.filament.component import FILAMENT
from scadbuddy.filament.inventory import PoolSbSpoolReads
from scadbuddy.filament.preset_mirror import mirror_after_pull
from scadbuddy.filament.print_uses import PoolPrintUses
from scadbuddy.filament.sb_presets import PresetImport, import_from_bambuddy
from scadbuddy.filament.sensor_notify import SensorNotifyActivities
from scadbuddy.filament.sensors import PoolSensorNotifications
from scadbuddy.flow_triggers.component import FLOW_TRIGGERS
from scadbuddy.flow_triggers.events import listen_for_triggers
from scadbuddy.flow_triggers.fire import TriggerFires, reconcile_trigger_schedules
from scadbuddy.flows.component import FLOWS
from scadbuddy.flows.sweep import sweep_flow_runs, sweep_orphan_grants
from scadbuddy.git_remote.component import GIT_REMOTE
from scadbuddy.git_remote.push import PushError
from scadbuddy.library.assets import referenced_asset_ids, stopped
from scadbuddy.library.backfill import attach_backfills, attach_job_backfills, follow_backfills
from scadbuddy.library.history import GitError
from scadbuddy.library.library_seed import seed_libraries, seeded_checkouts
from scadbuddy.library.output_byte_copy import copy_output_bytes
from scadbuddy.library.output_bytes import OutputBytes, usable
from scadbuddy.library.output_copy import copy_outputs
from scadbuddy.library.output_rows import OutputRows
from scadbuddy.library.outputs import reap_orphan_holds
from scadbuddy.library.previews import sweep_work_dirs
from scadbuddy.library.settings_store import (
    NATIVE_LIBRARY_NOT_READY,
    PUBLIC_URL,
    RECOVER_BAMBUDDY_LIBRARY_SQL,
    StoreNotReadyError,
    load_render_store_settings,
)
from scadbuddy.library.template_copy import copy_templates
from scadbuddy.operations.blob_claims import sweep_upload_blobs
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.component import OPERATIONS, OperationCommands
from scadbuddy.operations.kinds import OperationKind, Queue
from scadbuddy.operations.store import OperationStore
from scadbuddy.orders.archives import mirror_attempt, sweep_media
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.lifecycle import reap_quote_holds as reap_orphan_quote_holds
from scadbuddy.orders.storefront.prepare import prepare_activities, warm_unpriced
from scadbuddy.orders.storefront.staged_files import staged_file_activities, sweep_staged
from scadbuddy.orders.storefront.visitors import VisitorStore
from scadbuddy.payments.component import PAYMENTS
from scadbuddy.payments.deposits import sync_deposits
from scadbuddy.print_library.component import PRINT_LIBRARY
from scadbuddy.print_library.copy import library_copy_activities
from scadbuddy.print_library.sweep import sweep_library, sweep_print_copies
from scadbuddy.print_watch.component import AUTO_IMPORTS
from scadbuddy.print_watch.lan import LanImportActivities
from scadbuddy.print_watch.lan_store import StoreLanRecorder
from scadbuddy.print_watch.nudge import Nudger
from scadbuddy.print_watch.sweep import auto_import
from scadbuddy.printers.bambuddy_sync import (
    apply_virtual_printers,
    boot_adoption_due,
    read_virtual_printers,
    sync_printers,
)
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.print_activities import LanActivities, LanDeps
from scadbuddy.printers.watch import WatchActivities
from scadbuddy.rack.component import RACK_USAGE
from scadbuddy.render.agent_migrations import migrate_agent
from scadbuddy.render.previews import TIMEOUT_FACTOR as PREVIEW_TIMEOUT_FACTOR
from scadbuddy.render.previews import PreviewUnrunError
from scadbuddy.render.runner import probe_openscad_version
from scadbuddy.slicing.component import SLICING
from scadbuddy.slicing.start import stop_lost_native_slices, sweep_native_slices
from scadbuddy.spools.activities import (
    capture_backstop_activity,
    filament_pull_activity,
    filament_rfid_activity,
    spool_activities,
)
from scadbuddy.store import sweep_blobs
from scadbuddy.store.assets import RemoteAssets
from scadbuddy.store.backends import UnrecordedSweeper
from scadbuddy.store.bambuddy import RenderSettingsSource
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.content import ContentStore, sweep_content
from scadbuddy.store.download_cache import DOWNLOADS_DIRNAME
from scadbuddy.store.factory import blob_spool, build_store
from scadbuddy.store.geometry import GeometryIndex
from scadbuddy.store.index import STAGE_LOCK_SLOTS, BlobIndex, StageLocks
from scadbuddy.store.local import LocalContentBackend
from scadbuddy.store.migrate import migrate_activity
from scadbuddy.worker import (
    MAKE_CURRENT_BACKOFF,
    MAKE_CURRENT_ERROR_AFTER,
    MAKE_CURRENT_ERROR_EVERY,
    MAKE_CURRENT_EVERY,
    ROUTED_POLL,
    ROUTED_WAIT,
    deployment_writes,
    make_current_until_polled,
    run_inprocess_worker,
    worker_deps_from_state,
)
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.attempt_media import STEP_ACTIVITY as ATTEMPT_MEDIA_STEP
from scadbuddy.workflows.attempt_media import AttemptMediaStep, VersionedAttemptMedia
from scadbuddy.workflows.client import (
    LIBRARY_DEPLOYMENT_NAME,
    bambuddy_worker,
    connect,
    follow_worker,
    library_deployment,
    reconcile_lost_operations,
    reconcile_lost_runs,
    routes_to,
    sandboxed_runner,
)
from scadbuddy.workflows.flow_triggers import VersionedFlowTriggerFire
from scadbuddy.workflows.housekeeping import (
    ACTIVITY_SWEEP,
    ATTACHMENTS_SWEEP,
    ATTEMPT_MEDIA_SWEEP,
    AUTH_SESSIONS_SWEEP,
    AUTO_IMPORT_SWEEP,
    BACKFILL_SWEEP,
    BANK_DEPOSITS_SWEEP,
    ENTITY_ORPHANS_SWEEP,
    FLOWS_SWEEP,
    GIT_REMOTE_SWEEP,
    GIT_SWEEP,
    HEARTBEAT_TIMEOUT,
    LIBRARY_SWEEP,
    LOST_NATIVE_SLICES_SWEEP,
    LOST_SLICES_SWEEP,
    MAINTENANCE_IMPORT_SWEEP,
    NATIVE_COPIES_SWEEP,
    NATIVE_COPIES_TIMEOUT,
    NATIVE_SLICES_SWEEP,
    OUTPUT_BYTES_SWEEP,
    OUTPUT_COPY_SWEEP,
    PAYMENT_CAPTURES_SWEEP,
    PAYMENT_CLOSES_SWEEP,
    PREVIEWS_SWEEP,
    PRINTER_SYNC_SWEEP,
    PRINTER_WATCHES_SWEEP,
    QUOTE_HOLDS_SWEEP,
    REAP_SWEEP,
    RUN_HOLDS_SWEEP,
    RUN_HOLDS_TIMEOUT,
    STOREFRONT_DRAFTS_SWEEP,
    STOREFRONT_MAIL_SWEEP,
    SWEEPS,
    TEMPLATE_COPY_SWEEP,
    TRANSFER_PUTS_SWEEP,
    WARM_LISTINGS_SWEEP,
    ensure_schedules,
    library_worker,
)
from scadbuddy.workflows.library_copy import VersionedLibraryCopy
from scadbuddy.workflows.operation import VersionedOperationWorkflow
from scadbuddy.workflows.operation_activities import operation_activities
from scadbuddy.workflows.prepare_listing import VersionedPrepareListing
from scadbuddy.workflows.previews import (
    DUE_ACTIVITY,
    REFRESH_ACTIVITY,
    UNRUN_FAILURE,
    VersionedPreviewBackfill,
    ensure_preview_schedule,
)
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from scadbuddy.workflows.printer_watch import ensure_watches
from scadbuddy.workflows.sensor_notify import ensure_sensor_notify_schedule
from scadbuddy.workflows.slicing import VersionedSliceWorkflow
from scadbuddy.workflows.slicing_activities import slice_activities
from scadbuddy.workflows.spool_refresh import VersionedSpoolRefresh, ensure_spool_schedule
from scadbuddy.workflows.staged_file import VersionedStagedFile
from scadbuddy.workflows.store_migration import VersionedStoreMigration

API_PREFIX = "/api/v1"

#: Never traced (spec §6): set in code so no deployment can drop it.
#: Regexes searched against the full ``scheme://host/path`` URL, so each is anchored on
#: both ends: an unanchored ``/metrics`` would also drop ``/api/v1/models/metrics-x``.
EXCLUDED_URLS: Final = ",".join(
    f"^[a-z]+://[^/]+{path}$" for path in ("/healthz", "/metrics", "/telemetry/v1/traces")
)

#: How long a batch print nobody touches is kept (#2718 review): long past any print
#: it started, which Bambuddy's queue and the print runs keep on their own.
BATCH_TTL = timedelta(days=7)
#: A batch with a file still `pending` is what follows that run (#3798): kept longer.
PENDING_BATCH_TTL = timedelta(days=30)

logger = logging.getLogger(__name__)

DESCRIPTION = "Self-hosted OpenSCAD customizer for Bambuddy."


#: The `scadbuddy.api` modules whose router sits at the root rather than under
#: :data:`API_PREFIX`.
ROOT_ROUTE_MODULES = frozenset({"health", "metrics", "telemetry"})


def _api_router() -> APIRouter:
    """Every other module in `scadbuddy.api` that defines a ``router``, included under
    :data:`API_PREFIX`: a new route module is mounted without an edit here. The order
    is by name and does not matter, because no two routes match the same request
    (`tests/api/test_routes.py`)."""
    router = APIRouter()
    for info in sorted(pkgutil.iter_modules(scadbuddy.api.__path__), key=lambda i: i.name):
        if info.name in ROOT_ROUTE_MODULES:
            continue
        module = importlib.import_module(f"scadbuddy.api.{info.name}")
        module_router = getattr(module, "router", None)
        if isinstance(module_router, APIRouter):
            router.include_router(module_router)
    return router


def _name_in_openapi(app: FastAPI, *extra: type[BaseModel]) -> None:
    """Add models no route declares to the OpenAPI `components`.

    A route that parses its own body, as `POST /models` does for its three content
    types, can only describe it through `openapi_extra`, which FastAPI copies in
    verbatim: the model would be inlined and never named, and the generated client
    would have no type for it. Named here, `openapi_extra` points at it by `$ref`.
    A name already in the spec is refused: two models under one name would leave one
    shape for both, and a client would build the wrong body (#5317).
    """
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = generate()
        named = schema.setdefault("components", {}).setdefault("schemas", {})
        for model in extra:
            if model.__name__ in named:
                raise ValueError(f"the OpenAPI spec already names a {model.__name__}")
            named[model.__name__] = model.model_json_schema(
                ref_template="#/components/schemas/{model}"
            )
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


async def _close_quietly(state: AppState) -> None:
    """Close the render service and the bus after a failed start, logging (not
    raising) what fails, so the start's own error is the one that propagates."""
    closes: list[Callable[[], Awaitable[object]]] = [
        state.events.aclose,
        state.render.aclose,
        partial(asyncio.to_thread, state.projection.close),
    ]
    store = getattr(state, "store", None)  # unset when the start failed before it
    if store is not None:
        closes.append(store.aclose)
    for close in closes:
        try:
            await close()
        except Exception:
            logger.exception("could not release what a failed start opened")


async def sweep_assets(state: AppState, stop: threading.Event | None = None) -> list[str]:
    """Remove the uploads nothing references or has used for the grace (#296).

    The references are read first -- every job in the projection, then every
    saved preset, order item (#4249), output and template (`referenced_asset_ids`) -- and
    any failure to read them raises, or skips the pass, before anything is removed. What
    is referenced after that is kept by its last use, which the sweep re-checks under the
    store's lock per asset.
    ``stop``, once set, ends it before its next asset (#2140), and before and after
    reading the references, which no per-asset check can interrupt (#2554).
    The records and the sweep run in threads; the outputs' references are read on the
    loop, since an output's 3MF can come from the blob store (#7256).
    """
    if stopped(stop):
        return []
    records = await asyncio.to_thread(_asset_records, state)
    if records is None:
        return []
    referenced = await referenced_asset_ids(state.paths, records, state.outputs)
    if stopped(stop):
        return []
    removed = await asyncio.to_thread(
        state.assets.sweep, referenced, grace=state.config.asset_sweep_grace, stop=stop
    )
    state.metrics.assets_swept.inc(len(removed))
    if removed:
        logger.info("removed unused uploads", extra={"count": len(removed)})
    return removed


def _asset_records(state: AppState) -> list[Mapping[str, Any]] | None:
    """The JSON records from Postgres that can name an upload; None when the orders'
    cannot be read, which skips the pass. Blocking."""
    jobs = state.render.store.list_jobs()
    # Whole inputs, not only their `params`: a template UI's own state can name an
    # upload too (#725). A job's `params` beside them, for a row whose inputs are `{}`.
    records: list[Mapping[str, Any]] = [job.inputs for job in jobs]
    records += [job.params for job in jobs]
    records += state.presets.saved_inputs()
    try:
        # Every order item's inputs (#4249, orders spec §3.3). Fails closed: without
        # them the sweep would take an ordered upload, so a store that cannot answer
        # (or no orders component at all) skips this pass, as `stopped` does.
        records += state.components.get(ORDERS).store.item_inputs()
    except Exception:
        logger.exception("skipping the asset sweep: the orders' inputs could not be read")
        return None
    return records


def _sweep_checkouts(state: AppState) -> list[str]:
    """The thread half of :func:`sweep_library_checkouts`."""
    # Every id any revision of any model.json -- live or deleted model, mine or a
    # built-in -- ever held: ONE `git log -p`. A restore
    # puts a revision's pins back, so each of them is still a pin. Glob pathspecs, so
    # `*` stops at `/`: a model's own model.json, a built-in's one level deeper, and
    # no file of that name inside a model's folder.
    named = state.history.object_ids_in(
        f":(glob)*/{MODEL_META_NAME}",
        f":(glob){BUILTIN_DIR}/*/{MODEL_META_NAME}",
    )
    # The image's seed (#169) is kept pinned or not: the boot would copy it back.
    seed_dir = state.settings.resolve_seed_libraries_dir()
    seeded = set(seeded_checkouts(seed_dir)) if seed_dir is not None else set()

    def keep(name: str, commit: str) -> bool:
        return (
            commit in named
            or (name, commit) in seeded
            or bool(state.checkouts.leased(state.paths.libraries / name / commit))
            # The live pins as a removal counts them: uncommitted edits, and an
            # entry with no commit or an unreadable model.json keeps every checkout.
            or bool(state.catalogue.library_users(name, commit))
        )

    return state.libraries.sweep_checkouts(keep)


async def sweep_library_checkouts(state: AppState) -> list[str]:
    """Remove the library checkouts that nothing pins (#271): no live model, and no
    revision of any model in the history -- so restoring any revision never needs a
    checkout this removed.

    Under the checkout gate alone, as a removal: no pin or render in this process
    runs meanwhile. Another replica sharing ``/data`` is kept apart by the age
    guard in :meth:`LibraryStore.sweep_checkouts`. Without a repository there is
    no history to read, so nothing is swept.
    """
    if not state.history.available:
        return []
    async with state.checkouts.removing():
        removed = await asyncio.to_thread(_sweep_checkouts, state)
    if removed:
        logger.info("removed unpinned library checkouts", extra={"checkouts": removed})
    return removed


def _remote_assets(state: AppState) -> RemoteAssets | None:
    return state.store.remote_assets


async def drop_swept_assets(state: AppState, removed: list[str], *, cutoff: datetime) -> None:
    """The store's copies of what `sweep_assets` just removed from the volume; only
    those not stored again since ``cutoff``, the sweep's start."""
    remote = _remote_assets(state)
    if remote is not None and removed:
        await remote.drop(removed, cutoff=cutoff)


async def _sweep_assets_logged(state: AppState, stop: threading.Event | None = None) -> None:
    # Best effort: a store or volume error skips this sweep (removing nothing it could
    # not prove unused) and the next one retries. Only the Schedule's sweep runs it, so
    # a failure is logged and fails its activity, which reports it.
    try:
        remote = _remote_assets(state)
        cutoff = await remote.clock() if remote is not None else None
        removed = await sweep_assets(state, stop)
        if remote is None or cutoff is None:
            return
        if stop is not None and stop.is_set():
            # Stopped (#2140): only mark what it removed, which no later sweep could
            # find otherwise -- `reconcile` retries marked copies, never a diff against
            # the volume. The next sweep's reconcile removes them. This runs inside the
            # shutdown's bounded join: one that times out cancels it, and a copy left
            # unmarked then is never retried, so it stays in the store until something
            # else removes it (#2554).
            if removed:
                await remote.mark_swept(removed, cutoff=cutoff)
            return
        await drop_swept_assets(state, removed, cutoff=cutoff)
        # What an earlier drop failed to remove, and what the store lost (a copy an
        # `ensure` dropped, a race with a delete): both converge here, per sweep.
        await remote.reconcile(state.assets, cutoff=cutoff)
        await remote.backfill(state.assets)
    except Exception:
        logger.exception("could not sweep unused uploads")
        raise


async def _sweep_duplicate_staging_logged(state: AppState, *, reraise: bool = False) -> None:
    """The boot skips a volume error; the Schedule's sweep (``reraise``) logs any error,
    then fails its activity (review #1095b 4)."""
    try:
        await asyncio.to_thread(state.catalogue.sweep_duplicate_staging)
    except Exception as error:
        if not reraise and not isinstance(error, OSError):
            raise
        logger.exception("could not sweep duplicate staging folders")
        if reraise:
            raise


async def _sweep_unrecorded(content: ContentStore, grace: float) -> int:
    """#701: remove what uploads to ``content``'s backend left with no row (their process
    died first), once older than ``grace``; 0 for a backend that is no
    `UnrecordedSweeper`. `claim`: an object a live save uploaded and has not indexed yet
    stays (#7477); one a dead save left goes once past the grace."""
    backend = content.backend
    if not isinstance(backend, UnrecordedSweeper):
        return 0
    swept = await backend.sweep_unrecorded(
        cutoff=datetime.now(UTC) - timedelta(seconds=grace),
        recorded=partial(content.index.shares_backend_id, backend.backend),
        claim=content.claim_object,
    )
    return swept if isinstance(swept, int) else len(swept)


async def _sweep_blobs_logged(state: AppState) -> None:
    """The Temporal path's blob store: the pieces no job references any more. Only
    the Schedule's sweep runs it, so a failure is logged and fails its activity."""
    unrecorded = 0
    try:
        if state.store.content is None:
            removed = await asyncio.to_thread(
                sweep_blobs, state.blobs, state.refs, grace=state.config.sweep_grace
            )
            if state.outputs.bytes is not None:
                # `local`'s output store (#7257, plan D7): a deleted output's blobs.
                outputs_swept = await sweep_content(
                    state.outputs.bytes.content, state.refs, grace=state.config.sweep_grace
                )
                if outputs_swept:
                    logger.info(
                        "removed unreferenced output blobs", extra={"count": len(outputs_swept)}
                    )
                # And what a save that died before its commit uploaded there (#7477).
                unrecorded = await _sweep_unrecorded(
                    state.outputs.bytes.content, state.config.sweep_grace
                )
        else:
            failed = False
            try:
                removed = await sweep_content(
                    state.store.content, state.refs, grace=state.config.sweep_grace
                )
                # A `LocalContentBackend` is swept for unrecorded objects only as the
                # outputs' own store, above (#7477); no other store over one is (none
                # is built in prod; #7756 review).
                if not isinstance(state.store.content.backend, LocalContentBackend):
                    unrecorded = await _sweep_unrecorded(
                        state.store.content, state.config.sweep_grace
                    )
            except BaseException:
                # BaseException on purpose: a cancelled sweep must stay cancelled, not be
                # replaced by the trim's own failure below.
                failed = True
                raise
            finally:
                # Both steps above need the remote store (a stale blob's release is a
                # round trip too): one that is down must not stop the local cache being
                # trimmed, or it grows for as long as the remote stays down (#2115).
                if isinstance(state.store.blobs, CachedBlobStore):
                    try:
                        await asyncio.to_thread(state.store.blobs.evict)
                    except Exception:
                        # Under a Bambuddy failure, logged on its own and that failure
                        # goes on (#2560): raised here it would replace it, left only
                        # as __context__. Alone, it is the sweep's failure.
                        if not failed:
                            raise
                        logger.exception("could not trim the worker cache")
    except Exception:
        logger.exception("could not sweep unreferenced blobs")
        raise
    if unrecorded:
        logger.info("removed unrecorded uploads", extra={"count": unrecorded})
    if removed:
        logger.info("removed unreferenced blobs", extra={"count": len(removed)})
        # A swept piece is no geometry's source any more (`store/geometry.py`).
        try:
            await asyncio.to_thread(GeometryIndex(state.refs.pool).forget_pieces, removed)
        except Exception:
            logger.exception("could not forget swept pieces' geometry")
            raise


async def _backfill_store_logged(
    state: AppState, *, uploads: bool, fonts: bool = True, stop: threading.Event | None = None
) -> None:
    """The boot's mirror of what predates the store, in the background and best effort:
    an unreachable Bambuddy never holds up or fails the start. Uploads only when the
    housekeeping Schedule's sweep does not backfill them: `SCADBUDDY_ASSET_SWEEP_INTERVAL`
    0, or the Schedule paused (`_run_library_worker`, review #1095 2). Fonts on every
    boot: nothing else mirrors a family installed before. ``stop`` ends it after the item
    in hand (`_join_background`)."""
    try:
        if uploads and state.store.remote_assets is not None:
            mirrored = await state.store.remote_assets.backfill(state.assets, stop=stop)
            logger.info("mirrored uploads", extra={"count": mirrored})
        if fonts and state.store.fonts is not None:
            count = await state.store.fonts.backfill(stop=stop)
            logger.info("mirrored fonts", extra={"count": count})
    except Exception:
        logger.exception("could not mirror what predates the blob store; the next boot retries")


async def _index_outputs_logged(state: AppState, *, stop: threading.Event) -> None:
    """The start's pass over the saved outputs (#4777, `OutputIndex.sync`), best effort:
    a failure is logged, and the next start runs it again."""
    index = state.outputs.index
    if index is None:
        return
    try:
        added, dropped = await asyncio.to_thread(index.sync, state.outputs, stop)
        logger.info("indexed the saved outputs", extra={"added": added, "dropped": dropped})
    except Exception:
        logger.exception("could not index the saved outputs; the next start retries")


async def _copy_templates_once(state: AppState, stop: threading.Event) -> None:
    """One run of the copy of the templates' files to rows (#7317, `copy_templates`),
    logged. It takes the copy's advisory lock, so one replica copies at a time; a run
    that does not get it does nothing. A failure is logged, then raised."""
    refresh = state.catalogue.refresh
    if refresh is None:
        return
    try:
        result = await asyncio.to_thread(
            copy_templates, state.catalogue, refresh.rows, refresh, state.projection.pool, stop
        )
    except Exception:
        logger.exception("could not copy the templates to rows; the next sweep or start retries")
        raise
    if result is not None:
        logger.info(
            "copied the templates to rows",
            extra={
                "added": result.added,
                "refreshed": result.refreshed,
                "dropped": result.dropped,
                "unreadable": len(result.unreadable),
                "incomplete": result.incomplete,
                "stopped": stop.is_set(),
            },
        )


async def _copy_outputs_once(state: AppState, stop: threading.Event) -> None:
    """One run of the copy of the saved outputs' files to rows (#7255, `copy_outputs`),
    logged. It takes the copy's advisory lock, so one replica copies at a time; a run
    that does not get it does nothing. A failure is logged, then raised."""
    rows = state.outputs.rows
    if rows is None:
        return
    try:
        result = await asyncio.to_thread(
            copy_outputs, state.outputs, rows, state.projection.conninfo, stop
        )
    except Exception:
        logger.exception(
            "could not copy the saved outputs to rows; the next sweep or start retries"
        )
        raise
    if result is not None:
        logger.info(
            "copied the saved outputs to rows",
            extra={
                "copied": result.copied,
                "refreshed": result.refreshed,
                "dropped": result.dropped,
                "not_copied": len(result.not_copied),
                "damaged": len(result.damaged),
                "incomplete": result.incomplete,
                "stopped": stop.is_set(),
            },
        )


async def _copy_templates_logged(state: AppState, *, stop: threading.Event) -> None:
    """The start's copy (#7317), once and best effort: a failure is logged only. The
    housekeeping sweep `TEMPLATE_COPY_SWEEP` runs it again."""
    with suppress(Exception):
        await _copy_templates_once(state, stop)


async def _copy_outputs_logged(state: AppState, *, stop: threading.Event) -> None:
    """The start's copy (#7255), once and best effort: a failure is logged only. The
    housekeeping sweep `OUTPUT_COPY_SWEEP` runs it again, for the writes of pods on an
    older build."""
    with suppress(Exception):
        await _copy_outputs_once(state, stop)


async def _copy_output_bytes_once(state: AppState, stop: threading.Event) -> None:
    """One run of the byte copy of the saved outputs to the blob store (#7256,
    `copy_output_bytes`), logged. It takes the copy's advisory lock, so one replica copies
    at a time; a run that does not get it does nothing. On `local` it copies to the
    outputs' own content store (#7257, plan D7); on `bambuddy` it records a refused run.
    A failure is logged, then raised."""
    rows = state.outputs.rows
    if rows is None:
        return
    # On `local` the outputs' own content store (plan D7), else the shared one.
    content = (
        state.outputs.bytes.content if state.outputs.bytes is not None else state.store.content
    )
    try:
        result = await copy_output_bytes(
            state.outputs, rows, content, state.projection.conninfo, stop
        )
    except Exception:
        logger.exception(
            "could not copy the outputs' bytes to the blob store; the next sweep or start retries"
        )
        raise
    if result is not None:
        logger.info(
            "copied the outputs' bytes to the blob store",
            extra={
                "copied": result.copied,
                "verified": result.verified,
                "skipped": result.skipped,
                "failed": len(result.failed),
                "damaged": len(result.damaged),
                "refused": result.refused,
                "incomplete": result.incomplete,
                "stopped": stop.is_set(),
            },
        )
        if result.refused is None and not result.incomplete and not stop.is_set():
            await _try_cut_over(rows)


async def _try_cut_over(rows: OutputRows) -> bool:
    """Write the cut-over mark (#7257, plan D2) when the copies are done, best effort:
    whether it exists now. Only the call that wrote the mark logs it (another replica may
    write it meanwhile); a failure is logged only, and the next byte run or start tries
    again."""
    try:
        if await asyncio.to_thread(rows.cut_over):
            return True
        if not await asyncio.to_thread(rows.try_cut_over):
            return await asyncio.to_thread(rows.cut_over)
        at = await asyncio.to_thread(rows.cut_over_at)
    except Exception:
        logger.exception("could not write the cut-over mark; the next byte run or start retries")
        return False
    logger.info("the outputs are cut over (#7257)", extra={"at": at})
    return True


async def _copy_output_bytes_logged(state: AppState, *, stop: threading.Event) -> None:
    """The start's byte copy (#7256), once and best effort: a failure is logged only. The
    housekeeping sweep `OUTPUT_BYTES_SWEEP` runs it again, for the files of pods on an
    older build."""
    with suppress(Exception):
        await _copy_output_bytes_once(state, stop)


async def _copy_outputs_and_bytes_logged(state: AppState, *, stop: threading.Event) -> None:
    """The start's copies: the rows (#7255), then the bytes (#7256), which walk the rows
    the first copy wrote. The bytes are copied only when the last row copy run finished:
    a run that is ``incomplete`` (an outputs volume not there) or did not end leaves the
    byte copy to the sweep `OUTPUT_BYTES_SWEEP`.

    It tries the cut-over mark first (#7257): when the copies were done before this start,
    the mark is written now, and neither copy runs."""
    rows = state.outputs.rows
    if rows is not None and await _try_cut_over(rows):
        return
    await _copy_outputs_logged(state, stop=stop)
    if stop.is_set():
        return
    if rows is None:
        return
    try:
        last = await asyncio.to_thread(rows.last_run)
    except Exception:
        logger.exception("could not read the last row copy; the start does not copy the bytes")
        return
    if last is None or last["finished_at"] is None or last["incomplete"]:
        logger.info("the row copy did not finish; the start does not copy the outputs' bytes")
        return
    await _copy_output_bytes_logged(state, stop=stop)


#: How often a long sweep heartbeats: well inside its `HEARTBEAT_TIMEOUT`.
HEARTBEAT_EVERY = HEARTBEAT_TIMEOUT.total_seconds() / 4


#: How long a sweep the worker's stop cancels may still run (#1708): cancelling its task
#: would leave its thread running on the stores the lifespan closes next.
SWEEP_SHUTDOWN_JOIN = 10.0


#: How long the shutdown waits for the boot's backfills to stop (#2128): each runs its
#: work in threads, which a cancel would leave running on the pool and the stores the
#: lifespan closes next.
BACKGROUND_SHUTDOWN_JOIN = 10.0


async def _join_background(stop: threading.Event, *tasks: asyncio.Task[Any] | None) -> None:
    """Stop the boot's backfills (#2128): set ``stop``, which each checks between items,
    and wait up to `BACKGROUND_SHUTDOWN_JOIN` for them to end by themselves, as
    `follow_backfills`' remover waits for its attaches (#1759). One still running past
    that is cancelled and logged; its thread may then outlive the pool."""
    stop.set()
    running = {task for task in tasks if task is not None}
    if not running:
        return
    _, late = await asyncio.wait(running, timeout=BACKGROUND_SHUTDOWN_JOIN)
    for task in late:
        logger.warning(
            "a boot backfill was still running at shutdown; cancelled it",
            extra={"task": task.get_name()},
        )
        task.cancel()
    for task in running:
        with suppress(asyncio.CancelledError):
            await task


async def _heartbeating[T](work: Coroutine[Any, Any, T], stop: threading.Event | None = None) -> T:
    """Run ``work`` in the current activity, heartbeating until it ends (review #1095
    2): a worker lost mid-sweep is then noticed within the heartbeat timeout. One the
    worker's stop cancels first gets `SWEEP_SHUTDOWN_JOIN` to end by itself (#1708),
    so its thread is not left running while the lifespan closes its stores; ``stop``
    is set first, asking a sweep that checks it to end early (#2140)."""
    running = asyncio.create_task(work)
    try:
        while not (await asyncio.wait({running}, timeout=HEARTBEAT_EVERY))[0]:
            activity.heartbeat()
    except asyncio.CancelledError:
        if activity.is_worker_shutdown():
            if stop is not None:
                stop.set()
            await _join_at_shutdown(running)
        raise
    finally:
        running.cancel()
    return await running


async def _join_at_shutdown(running: asyncio.Task[Any]) -> None:
    """Wait up to `SWEEP_SHUTDOWN_JOIN` for a sweep the stop cancelled to end, under
    that cancel. Its own outcome is left to its logging; the activity is cancelled."""
    done, _ = await asyncio.wait({running}, timeout=SWEEP_SHUTDOWN_JOIN)
    if not done:
        logger.warning(
            "a library activity was still running when the worker stopped; cancelled it",
            extra={"activity": activity.info().activity_type},
        )
    elif not running.cancelled():
        running.exception()  # retrieved: the sweep logged it


def _attempt_media_activity(state: AppState) -> Callable[..., Any]:
    """The step of an attempt's ``AttemptMedia`` workflow (#7233, spec 2026-11-05 §4.6):
    it heartbeats while a timelapse downloads."""

    @activity.defn(name=ATTEMPT_MEDIA_STEP)
    async def attempt_media_step(fulfilment_id: str) -> AttemptMediaStep:
        orders = state.components.get(ORDERS)
        return await _heartbeating(
            mirror_attempt(
                orders.lifecycle,
                orders.integrations,
                state.store.blobs,
                ClaimStore(state.paths.claims),
                fulfilment_id,
                limit=state.settings.media_upload_max_bytes,
            )
        )

    return attempt_media_step


def _housekeeping_activities(state: AppState) -> list[Callable[..., Any]]:
    """The Schedule's sweeps (#1054), in `SWEEPS` order, each the loop's best-effort
    call: it logs a failure, then fails the activity (review #1095 1), so the run
    reports it; the other sweeps still run and the next tick tries again."""

    @activity.defn(name=SWEEPS[0])
    async def prune_jobs() -> None:
        # Its settle can describe many runs on a slow Temporal (#1707): heartbeat.
        await _heartbeating(_prune_jobs_logged(state))

    @activity.defn(name=SWEEPS[1])
    async def sweep_assets() -> None:
        # Its own stop, per run (#2140): one a worker's shutdown sets ends this run only,
        # never the sweeps of the worker `_run_library_worker` starts after it. A sweep
        # added here that runs a thread passes its own the same way.
        stop = threading.Event()
        await _heartbeating(_sweep_assets_logged(state, stop), stop)

    @activity.defn(name=SWEEPS[2])
    async def sweep_blobs() -> None:
        await _heartbeating(_sweep_blobs_logged(state))

    @activity.defn(name=SWEEPS[3])
    async def sweep_staging() -> None:
        # A crashed duplicate's staging otherwise waits for the next boot (#397).
        await _heartbeating(_sweep_duplicate_staging_logged(state, reraise=True))

    @activity.defn(name=SWEEPS[4])
    async def sweep_claims() -> None:
        await _heartbeating(_sweep_claims_and_batches_logged(state))

    @activity.defn(name=BACKFILL_SWEEP)
    async def sweep_backfills() -> None:
        # #902: `follow_backfills` attaches on the job's event; this finds what no
        # process heard (the API was down, the listener reconnecting).
        await _heartbeating(_attach_backfills_logged(state))

    @activity.defn(name=REAP_SWEEP)
    async def reap_output_holds() -> None:
        # A scan of every output's directory can be slow on a network volume: heartbeat.
        await _heartbeating(_reap_output_holds_logged(state))

    @activity.defn(name=OUTPUT_COPY_SWEEP)
    async def copy_outputs_to_rows() -> None:
        # #7255: its own stop, as `sweep_assets`': the worker's shutdown ends the walk
        # between folders. Any cancel sets it too, as a timeout's: the thread then ends and
        # frees the advisory lock.
        stop = threading.Event()
        try:
            await _heartbeating(_copy_outputs_once(state, stop), stop)
        except asyncio.CancelledError:
            stop.set()
            raise

    @activity.defn(name=OUTPUT_BYTES_SWEEP)
    async def copy_output_bytes_to_blobs() -> None:
        # #7256: its own stop, as the row copy's: the worker's shutdown or any cancel ends
        # the run between outputs and frees its advisory lock.
        stop = threading.Event()
        try:
            await _heartbeating(_copy_output_bytes_once(state, stop), stop)
        except asyncio.CancelledError:
            stop.set()
            raise

    @activity.defn(name=FLOWS_SWEEP)
    async def sweep_flow_runs() -> None:
        await _heartbeating(_sweep_flow_runs_logged(state))

    @activity.defn(name=PREVIEWS_SWEEP)
    async def sweep_preview_slices() -> None:
        await _heartbeating(_sweep_preview_slices_logged(state))

    @activity.defn(name=LOST_SLICES_SWEEP)
    async def sweep_lost_slice_starts() -> None:
        await _heartbeating(_sweep_lost_slices_logged(state))

    @activity.defn(name=GIT_SWEEP)
    async def maintain_models_git() -> None:
        await _heartbeating(_maintain_models_git_logged(state))

    @activity.defn(name=ACTIVITY_SWEEP)
    async def sweep_activity() -> None:
        await _heartbeating(_sweep_activity_logged(state))

    @activity.defn(name=TEMPLATE_COPY_SWEEP)
    async def copy_templates_to_rows() -> None:
        # #7317: its own stop, as the output copy's: the worker's shutdown ends the run
        # between batches. Any cancel sets it too, as a timeout's: the thread then ends
        # and frees the advisory lock.
        stop = threading.Event()
        try:
            await _heartbeating(_copy_templates_once(state, stop), stop)
        except asyncio.CancelledError:
            stop.set()
            raise

    @activity.defn(name=ATTACHMENTS_SWEEP)
    async def sweep_print_attachments() -> None:
        await _heartbeating(_sweep_print_attachments_logged(state))

    @activity.defn(name=ENTITY_ORPHANS_SWEEP)
    async def sweep_entity_orphans() -> None:
        await _heartbeating(_sweep_entity_orphans_logged(state))

    @activity.defn(name=LIBRARY_SWEEP)
    async def sweep_print_library() -> None:
        await _heartbeating(_sweep_print_library_logged(state))

    @activity.defn(name=NATIVE_SLICES_SWEEP)
    async def sweep_native_slice_rows() -> None:
        await _heartbeating(_sweep_native_slices_logged(state))

    @activity.defn(name=TRANSFER_PUTS_SWEEP)
    async def prune_transfer_puts() -> None:
        await _heartbeating(_prune_transfer_puts_logged(state))

    @activity.defn(name=LOST_NATIVE_SLICES_SWEEP)
    async def stop_lost_native_slice_rows() -> None:
        await _heartbeating(_stop_lost_native_slices_logged(state))

    @activity.defn(name=PRINTER_WATCHES_SWEEP)
    async def ensure_printer_watches() -> None:
        await _heartbeating(_ensure_printer_watches_logged(state))

    @activity.defn(name=QUOTE_HOLDS_SWEEP)
    async def reap_quote_holds() -> None:
        await _heartbeating(_reap_quote_holds_logged(state))

    @activity.defn(name=PRINTER_SYNC_SWEEP)
    async def sync_bambuddy_printers() -> None:
        await _heartbeating(_sync_bambuddy_printers_logged(state))

    @activity.defn(name=NATIVE_COPIES_SWEEP)
    async def sweep_native_copies() -> None:
        await _heartbeating(_sweep_native_copies_logged(state))

    @activity.defn(name=GIT_REMOTE_SWEEP)
    async def push_git_remote() -> None:
        await _heartbeating(_push_git_remote_logged(state))

    @activity.defn(name=AUTH_SESSIONS_SWEEP)
    async def sweep_auth_sessions() -> None:
        await _heartbeating(_sweep_auth_sessions_logged(state))

    @activity.defn(name=PAYMENT_CLOSES_SWEEP)
    async def retry_payment_closes() -> None:
        await _heartbeating(_retry_payment_closes_logged(state))

    @activity.defn(name=PAYMENT_CAPTURES_SWEEP)
    async def settle_payment_captures() -> None:
        await _heartbeating(_settle_payment_captures_logged(state))

    @activity.defn(name=BANK_DEPOSITS_SWEEP)
    async def sync_bank_deposits() -> None:
        await _heartbeating(_sync_bank_deposits_logged(state))

    @activity.defn(name=MAINTENANCE_IMPORT_SWEEP)
    async def import_maintenance() -> None:
        await _heartbeating(_import_maintenance_logged(state))

    @activity.defn(name=STOREFRONT_DRAFTS_SWEEP)
    async def sweep_storefront_drafts() -> None:
        await _heartbeating(_sweep_storefront_drafts_logged(state))

    @activity.defn(name=STOREFRONT_MAIL_SWEEP)
    async def send_storefront_mail() -> None:
        await _heartbeating(_send_storefront_mail_logged(state))

    @activity.defn(name=AUTO_IMPORT_SWEEP)
    async def auto_import_prints() -> None:
        await _heartbeating(_auto_import_prints_logged(state))

    @activity.defn(name=RUN_HOLDS_SWEEP)
    async def reap_print_run_holds() -> None:
        await _heartbeating(_reap_run_holds_logged(state))

    @activity.defn(name=ATTEMPT_MEDIA_SWEEP)
    async def import_attempt_media() -> None:
        await _heartbeating(_import_attempt_media_logged(state))

    @activity.defn(name=WARM_LISTINGS_SWEEP)
    async def warm_listings() -> None:
        await _heartbeating(_warm_listings_logged(state))

    return [
        prune_jobs,
        sweep_assets,
        sweep_blobs,
        sweep_staging,
        sweep_claims,
        sweep_backfills,
        reap_output_holds,
        copy_outputs_to_rows,
        copy_output_bytes_to_blobs,
        sweep_flow_runs,
        sweep_preview_slices,
        sweep_lost_slice_starts,
        maintain_models_git,
        sweep_activity,
        copy_templates_to_rows,
        sweep_print_attachments,
        sweep_entity_orphans,
        sweep_print_library,
        sweep_native_slice_rows,
        prune_transfer_puts,
        stop_lost_native_slice_rows,
        ensure_printer_watches,
        reap_quote_holds,
        sync_bambuddy_printers,
        sweep_native_copies,
        push_git_remote,
        sweep_auth_sessions,
        retry_payment_closes,
        settle_payment_captures,
        sync_bank_deposits,
        import_maintenance,
        sweep_storefront_drafts,
        send_storefront_mail,
        auto_import_prints,
        reap_print_run_holds,
        import_attempt_media,
        warm_listings,
    ]


async def _sweep_native_slices_logged(state: AppState) -> None:
    """The native slicer's settled slices (plan 2026-10-16 Task 12): the rows past
    `PREVIEW_GRACE` with their holds. The blobs go on `sweep_content`'s next pass."""
    slicing = state.components.get(SLICING)
    try:
        removed = await sweep_native_slices(slicing.store, finished_older_than=PREVIEW_GRACE)
    except Exception:
        logger.exception("could not sweep the native slices")
        raise
    if removed:
        logger.info("removed settled native slices", extra={"count": len(removed)})


async def _stop_lost_native_slices_logged(state: AppState) -> None:
    """The running native slices no execution writes, failed (Task 12 review M1)."""
    slicing = state.components.get(SLICING)
    try:
        stopped = await stop_lost_native_slices(slicing.temporal(), slicing.store)
    except Exception:
        logger.exception("could not stop the lost native slices")
        raise
    if stopped:
        logger.info("stopped lost native slices", extra={"count": len(stopped)})


#: How long the slicer worker's PUT records are kept: a slice records its PUT within
#: minutes, and a retry reuses the same pending key.
TRANSFER_PUTS_TTL = timedelta(days=1)


async def _prune_transfer_puts_logged(state: AppState) -> None:
    try:
        await asyncio.to_thread(state.components.get(SLICING).puts.prune, TRANSFER_PUTS_TTL)
    except Exception:
        logger.exception("could not prune the transfer PUTs")
        raise


async def _ensure_printer_watches_logged(state: AppState) -> None:
    """#3823: a watch for every LAN-linked printer, on the print queue."""
    try:
        started = await ensure_watches(
            state.components.get(PRINTERS).store,
            state.print_runs.client,
            state.print_runs.task_queue,
        )
    except Exception:
        logger.exception("could not start the printer watches")
        raise
    if started:
        logger.debug("printer watches running", extra={"count": len(started)})


async def _sync_bambuddy_printers_logged(state: AppState) -> None:
    """#4439, #4747: the registry follows Bambuddy's printers and virtual printers, and
    an empty LAN link is filled from Bambuddy. Nothing to do without a Bambuddy."""
    registry = state.components.get(PRINTERS)

    async def sweep(client: BambuddyClient) -> None:
        try:
            result = await sync_printers(registry, client)
        except Exception:
            logger.exception("could not sync the printers from Bambuddy")
            raise
        news = result.created or result.linked or result.lan_linked or result.conflicts
        if news or result.target_missing:
            # A VP whose target never gets a row repeats on every sweep: alone, it is
            # DEBUG, not an INFO line per sweep (#4861).
            logger.log(
                logging.INFO if news else logging.DEBUG,
                "synced the printers from Bambuddy",
                extra={
                    # Not "created": a LogRecord attribute, which `extra` may not
                    # overwrite (it raised KeyError and lost the line, #4858).
                    "printers_created": result.created,
                    "linked": result.linked,
                    "lan_linked": result.lan_linked,
                    "conflicts": result.conflicts,
                    "target_missing": result.target_missing,
                },
            )

    await _with_bambuddy(state, sweep)


#: How long the boot's virtual-printer pass may wait for Bambuddy's two lists.
VP_BOOT_TIMEOUT = 60.0


async def _adopt_bb_instance_logged(state: AppState) -> None:
    """The filament links #6261's pull made before the sync's state row belong to the
    configured Bambuddy (`adopt_instance`, #6265 Decision 12). Nothing without a
    Bambuddy. A failure is logged, never raised: the boot goes on, and the first pass
    or pull that links anything records the instance itself."""
    try:
        read = await asyncio.to_thread(state.settings_store.read, {"bambuddy_url"})
        if read["bambuddy_url"]:
            await asyncio.to_thread(
                adopt_instance, state.components.get(FILAMENT), instance_for(read["bambuddy_url"])
            )
    except Exception:
        logger.exception("could not record the Bambuddy the filament links belong to")


async def _adopt_virtual_rows_logged(state: AppState) -> None:
    """#5236: the boot's virtual-printer pass. A row made by hand before the sync existed,
    with a Bambuddy virtual printer's serial, stayed ``physical`` while the full sync was
    left to the sweeps' Schedule, which prod turns off. So, once per boot and only when
    `boot_adoption_due`, the sync's virtual-printer part runs: two Bambuddy lists, no
    access code, no probe. Nothing without a Bambuddy. Bambuddy refusing, unreachable or
    past `VP_BOOT_TIMEOUT` is one WARNING from this pass (the client logs its own failed
    request besides); anything else, the registry's own ApiErrors too (#5259), one logged
    traceback; never raised."""
    try:
        registry = state.components.get(PRINTERS)
        rows = await asyncio.to_thread(registry.store.list)
        if not boot_adoption_due(rows):
            return
        settings = await asyncio.to_thread(state.settings_store.load)
        # Only Bambuddy's calls are in here: an ApiError from the registry's writes below
        # is a local fault, never "Bambuddy unreachable" (#5259).
        try:
            async with asyncio.timeout(VP_BOOT_TIMEOUT), client_for(settings) as client:
                listing = await read_virtual_printers(client)
        except ApiError as error:
            if error.type == NOT_CONFIGURED_PROBLEM:
                return
            logger.warning(
                "could not sync the virtual printers from Bambuddy at start",
                extra={"status": error.status},
            )
            return
        except TimeoutError:
            logger.warning(
                "could not sync the virtual printers from Bambuddy at start",
                extra={"timeout": VP_BOOT_TIMEOUT},
            )
            return
        await apply_virtual_printers(registry, listing)
    except Exception:
        logger.exception("could not sync the virtual printers from Bambuddy at start")


async def _sweep_print_library_logged(state: AppState) -> None:
    """#3827: the native library's old trash and orphan blobs; nothing on ``bambuddy``."""
    try:
        native = await state.components.get(PRINT_LIBRARY).serving_native()
        if native is None:
            return
        done = await sweep_library(native, now=datetime.now(UTC))
    except Exception:
        logger.exception("could not sweep the print library")
        raise
    if done.purged or done.orphans:
        logger.info(
            "swept the print library",
            extra={"purged": done.purged, "orphans": done.orphans},
        )


async def _sweep_native_copies_logged(state: AppState) -> None:
    """#4337: a native run's per-print copies and slices nothing keeps, to the trash;
    no copies on ``bambuddy``. The LAN runs whose sequence is gone are recorded stopped
    on every backend first (#6871)."""
    try:
        # A LAN run whose sequence is gone uses nothing (`end_gone_sequences`), and its
        # controls no longer stand first on the Prints page (#6871): checked on every
        # backend, since the strip only reads. Without Temporal every LAN run with prints
        # still to start is kept as in use.
        try:
            await end_gone_sequences(state.print_runs, NATIVE_COPIES_TIMEOUT / 2)
        except Exception:
            logger.warning(
                "could not check the LAN runs' sequences; their files are kept", exc_info=True
            )
        native = await state.components.get(PRINT_LIBRARY).serving_native()
        if native is None:
            return
        await sweep_print_copies(
            native,
            state.uploads,
            state.print_runs.store.subjects_in_use,
            state.components.get(SLICING).store.slicing,
            now=datetime.now(UTC),
        )
    except Exception:
        logger.exception("could not sweep the native per-print copies")
        raise


async def _sweep_print_attachments_logged(state: AppState) -> None:
    """#3047: the attachments of archives deleted in Bambuddy."""
    store = state.components.get(PRINT_ATTACHMENTS)

    async def sweep(client: BambuddyClient) -> None:
        try:
            gone = await sweep_gone_archives(client, store)
        except Exception:
            logger.exception("could not sweep the attachments of deleted archives")
            raise
        if gone:
            logger.info("forgot the attachments of deleted archives", extra={"count": gone})

    await _with_bambuddy(state, sweep)


async def _sweep_entity_orphans_logged(state: AppState) -> None:
    """#6950 (spec D4, phase 1): the entities whose owner's row is gone, with their files
    and links."""
    entities = state.components.get(ENTITIES)
    try:
        removed = await asyncio.to_thread(
            sweep_orphan_entities, entities.store, entities.attachments
        )
    except Exception:
        logger.exception("could not sweep the entities whose owner is gone")
        raise
    if removed:
        logger.info("removed entities whose owner is gone", extra={"count": removed})


async def _sweep_activity_logged(state: AppState) -> None:
    """Spec 2026-10-09 §6: repair what the sources' own writes missed, then prune. Repair
    first, so a placeholder it can fill is filled before its time is up; prune runs
    whatever repair did. The window stays an hour inside the TTL, or a pruned row could
    be restored from its source on the next run: prune reads the row's own finished_at,
    the moment it was written, and repair the source's, a little earlier."""
    failed = False
    try:
        settings = await asyncio.to_thread(state.settings_store.load)
    except Exception:
        # The stored TTL is unknown, and the default may be shorter than the operator's:
        # pruning with it would delete rows they chose to keep. Skip the run, which fails.
        logger.warning("could not load the activity settings; skipping the sweep", exc_info=True)
        raise
    ttl = settings.activity_ttl_seconds or ACTIVITY_TTL_DEFAULT
    window = min(
        settings.activity_repair_window_seconds or ACTIVITY_REPAIR_WINDOW_DEFAULT,
        ttl - ACTIVITY_PRUNE_MARGIN,
    )
    index = state.components.get(ACTIVITY).index
    try:
        await index.repair(window)
    except Exception:
        logger.exception("could not repair activity")
        failed = True
    try:
        await index.prune(ttl, window)
    except Exception:
        logger.exception("could not prune activity")
        failed = True
    # #6646 Decision 49: the sensors' old notification rows, in this activity so the
    # workflow's history is unchanged.
    try:
        pruned = await asyncio.to_thread(state.components.get(FILAMENT).sensors.prune_notifications)
    except Exception:
        logger.exception("could not prune the sensor notifications")
        failed = True
    else:
        if pruned:
            logger.info("pruned sensor notifications", extra={"count": pruned})
    # #6905: the filament sync's old pass rows, at most `PRUNE_BATCH` a run, by the setting
    # read above (a failed load prunes nothing), in this activity for the same reason.
    try:
        pruned = await asyncio.to_thread(
            state.components.get(FILAMENT).sync_passes.prune,
            settings.filament_sync_pass_ttl_days,
        )
    except Exception:
        logger.exception("could not prune the filament sync passes")
        failed = True
    else:
        if pruned:
            logger.info("pruned filament sync passes", extra={"count": pruned})
    if failed:
        raise RuntimeError("the activity sweep failed")


async def _prune_jobs_logged(state: AppState) -> None:
    try:
        await state.render.prune()
    except Exception:
        logger.exception("could not prune settled render jobs")
        raise


async def _sweep_claims_and_batches_logged(state: AppState) -> None:
    """The claims sweep, which also forgets batch prints left untouched for `BATCH_TTL`:
    both are a delete on the prune cadence (`PRUNE_SWEEPS`), so the batch prune rides
    this activity rather than adding a sweep a replica on an older build could not run
    (#2718 review). Its failure is logged as the batch prune's own."""
    # Each its own: one failing never starves the other, as the flow sweeps.
    failed: Exception | None = None
    try:
        await asyncio.to_thread(ClaimStore(state.paths.claims).sweep)
    except Exception as err:
        logger.exception("could not sweep operation claims")
        failed = err
    try:
        # #7373: a library upload's claim is a blob; one no release removed goes here.
        if state.store.content is not None:
            await sweep_upload_blobs(state.store.content)
    except Exception as err:
        logger.exception("could not sweep library uploads' blobs")
        failed = failed or err
    try:
        # #2718 review: a batch print closed without Cancel or Done stays otherwise.
        pruned = await state.components.get(PRINT_BATCHES).prune(
            BATCH_TTL, pending_older_than=PENDING_BATCH_TTL
        )
        if pruned.count:
            logger.info("forgot batch prints left untouched", extra={"count": pruned.count})
        if pruned.pending:
            # #3798: each still followed a file's run, so a new batch could send it again.
            logger.warning(
                "forgot batch prints with a file still being checked",
                extra={"batches": pruned.pending},
            )
    except Exception as err:
        logger.exception("could not prune batch prints")
        failed = failed or err
    if failed is not None:
        raise failed


async def _sweep_flow_runs_logged(state: AppState) -> None:
    flows = state.components.get(FLOWS)
    if flows.client is None:
        logger.debug("no flow-run sweep: flows are unavailable without the secret key file")
        return
    # Each its own: one failing never starves the other (review of #2643).
    failed: Exception | None = None
    try:
        ended = await sweep_flow_runs(flows.store, flows.client)
        if ended:
            logger.info("ended flow runs whose execution closed", extra={"count": len(ended)})
    except Exception as err:
        logger.exception("could not sweep flow runs")
        failed = err
    try:
        orphans = await sweep_orphan_grants(flows.store, flows.client)
        if orphans:
            logger.info("deleted grants whose run never started", extra={"count": len(orphans)})
    except Exception as err:
        logger.exception("could not sweep the grants of runs that never started")
        failed = failed or err
    if failed is not None:
        raise failed


async def _sweep_preview_slices_logged(state: AppState) -> None:
    """#2169: the dialog's background slices no run queued. On `SWEEPS`' Schedule only,
    so the sweeps' switch turns it off."""

    async def sweep(client: BambuddyClient) -> None:
        try:
            removed = await sweep_preview_slices(client, state.uploads)
        except Exception:
            logger.exception("could not sweep the print dialog's background slices")
            raise
        if removed:
            logger.info("removed unprinted background slices", extra={"count": removed})

    await _with_bambuddy(state, sweep)


async def _sweep_lost_slices_logged(state: AppState) -> None:
    """#2715: what print runs' lost slice starts left. On the prune Schedule too: the
    job that names the file is kept only 30 minutes."""

    async def sweep(client: BambuddyClient) -> None:
        try:
            lost = await sweep_lost_slices(client, state.uploads)
        except Exception:
            logger.exception("could not sweep the files of lost slice starts")
            raise
        if lost:
            logger.info("removed the files of lost slice starts", extra={"count": lost})

    await _with_bambuddy(state, sweep)


async def _with_bambuddy(
    state: AppState, sweep: Callable[[BambuddyClient], Awaitable[None]]
) -> None:
    """Run ``sweep`` with a Bambuddy client; nothing to do when Bambuddy is not set up.
    What ``sweep`` raises it has logged, and is raised as it is."""
    settings = await asyncio.to_thread(state.settings_store.load)
    # Held, not raised in place: a sweep's own ApiError must miss the handler below,
    # which would log it again as Bambuddy unreachable, or drop it as NOT_CONFIGURED.
    failed: Exception | None = None
    try:
        async with client_for(settings) as client:
            try:
                await sweep(client)
            except Exception as err:
                failed = err
    except ApiError as error:
        if error.type == NOT_CONFIGURED_PROBLEM:
            logger.debug("no slice sweep: Bambuddy is not set up")
            return
        logger.exception("could not reach Bambuddy to sweep slices")
        raise
    if failed is not None:
        raise failed


async def _maintain_models_git_logged(state: AppState) -> None:
    try:
        packed = await asyncio.to_thread(state.history.maintain)
    except Exception:
        logger.exception("could not pack the models repository")
        raise
    if packed:
        logger.info("packed the models repository")


def _rotate_service_credentials(state: AppState) -> None:
    """#4975 phase 4 (spec 2026-11-01 §7.2): each caller's next credential when its
    newest has under seven days left, and the long-dead ones forgotten. Without the KEK
    none can be sealed, so none are issued. First, a sealed credential of a caller that
    reads none any more (the projects and print workers', #5298) is revoked, KEK or not."""
    auth = state.components.get(AUTH)
    try:
        retired = auth.credentials.revoke_retired_legacy()
    except Exception:
        # Never in the way of the rotation: the next start or sweep tries again.
        logger.warning("could not revoke the retired callers' sealed credentials", exc_info=True)
    else:
        if retired:
            logger.info(
                "revoked the retired callers' sealed credentials", extra={"credentials": retired}
            )
    kek = auth.kek
    if kek is None:
        return
    minted = auth.credentials.rotate_due(kek)
    if minted:
        logger.info("issued service credentials", extra={"credentials": len(minted)})
    auth.credentials.delete_expired()


def _migrate_agent_at_start(state: AppState) -> None:
    """The agent's migrations (`render/agent_migrations.py`, #5299 D6), after the
    backend's: the agent neither migrates nor waits for its ledger (#5299 step 12). Neither
    set reads the other's tables, so the order is the start's, not the schema's. A failure
    stops the start."""
    with state.settings_store.pool.connection() as conn:
        applied = migrate_agent(conn)
    if applied:
        logger.info("applied the agent's database migrations", extra={"versions": applied})


def _grant_and_rotate_at_start(state: AppState) -> None:
    """At start, after the migrations: a retired caller's role nothing (#5298 Tasks 11, 22
    and 35, #5299 step 12), what each retired role still has logged (`auth/revoke.py`
    `retired_state`, the retirement order's checks), and the credentials due. A failure is
    logged, never fatal."""
    try:
        with state.settings_store.pool.connection() as conn:
            revoked: list[str] = []
            states: list[RetiredState] = []
            try:
                revoked = revoke_retired(conn)
            except Exception:
                # A revoke that fails must not cost the start its credentials' rotation.
                logger.exception("could not revoke the retired callers' roles")
            try:
                states = retired_state(conn)
            except Exception:
                logger.exception("could not read what the retired callers' roles still have")
        if revoked:
            logger.info("revoked the retired callers' roles", extra={"roles": revoked})
        for left in states:
            logger.info(
                "a retired caller's role still exists",
                extra={
                    "role": left.role,
                    "granted_relations": left.granted,
                    "owned_relations": left.owned,
                    "sessions": left.sessions,
                },
            )
        _rotate_service_credentials(state)
    except Exception:
        logger.exception("could not issue the service credentials")


def _rotate_agent_secrets(state: AppState) -> None:
    """#5299 D5: with the previous KEK mounted, the agent's Claude credentials, payload
    keys, Web Push keys, notification channel tokens, plugin header secrets and sign-ins,
    and the workspace's principal file keys sealed under it are sealed again under the
    current one (`agent_data/rotation.py`), as the agent did at its own start. Logged,
    never fatal."""
    current = kek_at(state.settings.secret_key_file)
    previous = kek_at(state.settings.secret_key_previous_file)
    if state.settings.secret_key_previous_file is not None and previous is None:
        logger.warning(
            "the previous secret key cannot be read; the agent's secrets are not re-wrapped"
        )
    if current is None or previous is None:
        return
    try:
        with state.settings_store.pool.connection() as conn:
            rotate_agent_secrets(conn, previous, current)
    except Exception:
        logger.exception("could not re-wrap the agent's secrets under the current key")


def _prune_crud_requests(state: AppState) -> None:
    with state.settings_store.pool.connection() as conn:
        idempotency.prune(conn)


async def _sweep_auth_sessions_logged(state: AppState) -> None:
    """#4975: the signed-in sessions no request can use again, and (in the activity, so
    the workflow's history is unchanged) the service credentials' rotation (phase 4), the
    spent delegation codes and long-dead access tokens (phase 6), and the CRUD routes'
    idempotency replies past their 24 h (#5298, plan D5)."""
    auth = state.components.get(AUTH)
    try:
        deleted = await asyncio.to_thread(auth.sessions.delete_expired)
    except Exception:
        logger.exception("could not delete the expired sessions")
        raise
    try:
        await asyncio.to_thread(_rotate_service_credentials, state)
    except Exception:
        logger.exception("could not rotate the service credentials")
        raise
    try:
        await asyncio.to_thread(auth.delete_spent_delegations)
        await asyncio.to_thread(auth.tokens.delete_expired)
    except Exception:
        logger.exception("could not delete the spent delegation codes or old access tokens")
        raise
    try:
        await asyncio.to_thread(_prune_crud_requests, state)
    except Exception:
        logger.exception("could not prune the CRUD routes' idempotency ledger")
        raise
    if deleted:
        logger.info("deleted expired sessions", extra={"sessions": deleted})


async def _push_git_remote_logged(state: AppState) -> None:
    """#4974: what a push after a commit left behind, when a git remote is set."""
    try:
        pushed = await asyncio.to_thread(state.components.get(GIT_REMOTE).push_all)
    except PushError:
        # Each failure is recorded for Settings, scrubbed of the credential.
        logger.warning("could not push to the git remote")
        raise
    except Exception:
        logger.exception("could not push to the git remote")
        raise
    if pushed:
        logger.info("pushed to the git remote", extra={"repositories": pushed})


async def _retry_payment_closes_logged(state: AppState) -> None:
    """#5700: close again each provider's page whose close failed, once its wait is over."""
    payments = state.components.get(PAYMENTS)
    try:
        closed = await payments.service.retry_closes()
    except Exception:
        logger.exception("could not retry the payment pages' closes")
        raise
    if closed:
        logger.info("closed payment pages", extra={"count": closed})


async def _settle_payment_captures_logged(state: AppState) -> None:
    """#4959: settle each pending capture whose outcome was never told."""
    payments = state.components.get(PAYMENTS)
    try:
        settled = await payments.service.settle_captures()
    except Exception:
        logger.exception("could not settle the pending payment captures")
        raise
    if settled:
        logger.info("settled pending payment captures", extra={"count": settled})


async def _sync_bank_deposits_logged(state: AppState) -> None:
    """#4961: read the linked bank account's new deposits and suggest their orders."""
    payments = state.components.get(PAYMENTS)
    try:
        suggested = await sync_deposits(payments.bank, state.events)
    except Exception:
        logger.exception("could not sync the bank deposits")
        raise
    if suggested:
        logger.info("suggested deposit matches", extra={"count": suggested})


#: How long a visitor's `storefront_renders` rows are kept: the render limits count a
#: day at most (#5282).
STOREFRONT_RENDERS_TTL = timedelta(days=1)


def _sweep_storefront(
    state: AppState, *, now: datetime | None = None
) -> tuple[list[str], int, int]:
    days = state.settings_store.snapshot().runtime.storefront_draft_ttl_days
    now = datetime.now(UTC) if now is None else now
    before = now - timedelta(days=days)
    drafts = state.components.get(ORDERS).lifecycle.sweep_drafts(before)
    with state.projection.pool.connection() as conn:
        visitors, renders = VisitorStore.sweep(
            conn, visitors_before=before, renders_before=now - STOREFRONT_RENDERS_TTL
        )
    return drafts, visitors, renders


async def _sweep_storefront_drafts_logged(state: AppState) -> None:
    """#5282: the storefront carts idle past ``storefront_draft_ttl_days``, then the
    visitors idle as long with no order, the visitors' renders past a day, and the
    storefront's staged files' backstop (#6133, `staged_files.sweep_staged`)."""
    try:
        drafts, visitors, renders = await asyncio.to_thread(_sweep_storefront, state)
        # The backstop only: each staged file's watcher owns its clean-up.
        staged = await sweep_staged(state)
    except Exception:
        logger.exception("could not sweep the idle storefront carts")
        raise
    if drafts or visitors or renders or staged:
        logger.info(
            "swept idle storefront carts",
            extra={
                "drafts": len(drafts),
                "visitors": visitors,
                "renders": renders,
                "staged": staged,
            },
        )


async def _send_storefront_mail_logged(state: AppState) -> None:
    """#5283: the storefront mail its after-commit send left queued."""
    mail = state.components.get(ORDERS).mail
    try:
        sent = await asyncio.to_thread(mail.send_queued)
    except Exception:
        logger.exception("could not send the queued storefront mail")
        raise
    if sent:
        logger.info("sent queued storefront mail", extra={"count": sent})


async def _auto_import_prints_logged(state: AppState) -> None:
    """#4699: the print watcher's pass. With ``print_auto_import`` off it reads nothing
    from Bambuddy and no printer, and forgets when it was switched on. The switch is
    read with the floor, in one transaction (`AutoImportStore.floor`), not from
    ``settings``: a ``PUT /settings`` that commits between the two reads never loses its
    floor (#6011)."""
    store = state.components.get(AUTO_IMPORTS)
    settings = await asyncio.to_thread(state.settings_store.load)
    try:
        since = await store.floor()
    except Exception:
        logger.exception("could not read when the print watcher was switched on")
        raise
    if since is None:
        return

    async def sweep(client: BambuddyClient) -> None:
        try:
            result = await auto_import(
                client,
                settings,
                since,
                links=state.print_links,
                uploads=state.uploads,
                store=store,
                slugs=state.catalogue.slugs,
                exists=state.catalogue.exists,
            )
        except Exception:
            logger.exception("could not import the prints ScadBuddy did not start")
            raise
        if result.linked or result.inbox:
            logger.info(
                "imported prints ScadBuddy did not start",
                extra={"linked": result.linked, "inbox": result.inbox},
            )

    await _with_bambuddy(state, sweep)


async def _reap_quote_holds_logged(state: AppState) -> None:
    """#4250 Ruling 3: the slice quotes' holders §3.4 says should be gone."""
    orders = state.components.get(ORDERS)
    try:
        dropped = await asyncio.to_thread(reap_orphan_quote_holds, orders.refs, orders.store)
    except Exception:
        logger.exception("could not reap the quotes' sliced-file holds")
        raise
    if dropped:
        logger.info("reaped quote holds", extra={"count": dropped})


async def _import_maintenance_logged(state: AppState) -> None:
    """#5774 (spec 2026-11-17-machine-rate-actuals §3.7): each linked printer's
    maintenance log read from Bambuddy. A printer Bambuddy does not answer for keeps its
    stored events and is counted, not raised: the next tick reads it again."""
    try:
        read, failed = await state.components.get(ORDERS).machine_rates.read_logs()
    except Exception:
        logger.exception("could not import the printers' maintenance logs")
        raise
    if read or failed:
        logger.log(
            logging.WARNING if failed else logging.DEBUG,
            "imported the printers' maintenance logs",
            extra={"read": read, "failed": failed},
        )


async def _import_attempt_media_logged(state: AppState) -> None:
    """#7234: one ``AttemptMedia`` step for each order attempt still missing a medium
    (`orders.archives.sweep_media`, spec 2026-11-05 §4.6), counted in
    ``scadbuddy_attempt_media_sweep_starts`` (#7491)."""
    orders = state.components.get(ORDERS)
    try:
        started = await sweep_media(
            orders.lifecycle,
            orders.integrations,
            orders.media_starter,
            limit=state.settings.media_upload_max_bytes,
        )
    except Exception:
        logger.exception("could not sweep the order attempts' media")
        raise
    state.metrics.attempt_media_sweep_starts.inc(started)
    if started:
        logger.info("started attempt media imports", extra={"count": started})


async def _warm_listings_logged(state: AppState) -> None:
    """#7665: the default price run of each published listing whose default choices have
    no price (`orders.storefront.prepare.warm_unpriced`)."""
    try:
        started = await warm_unpriced(state, state.components.get(ORDERS))
    except Exception:
        logger.exception("could not sweep the published listings' prices")
        raise
    if started:
        logger.info("started default price runs", extra={"count": started})


async def _reap_run_holds_logged(state: AppState) -> None:
    """#6122: the LAN runs' holds on their sliced files that a run's end left. First
    (#6871) the LAN runs whose sequence is gone are recorded stopped, which drops their
    holds too and lets the next queued run head its printer's card, every
    `PRUNE_INTERVAL` whatever the asset sweep's interval."""
    try:
        await end_gone_sequences(state.print_runs, RUN_HOLDS_TIMEOUT / 2)
    except Exception:
        logger.warning("could not check the LAN runs' sequences", exc_info=True)
    try:
        dropped = await asyncio.to_thread(reap_run_holds, state.refs)
    except Exception:
        logger.exception("could not reap the print runs' sliced-file holds")
        raise
    if dropped:
        logger.info("reaped print run holds", extra={"count": dropped})


async def _reap_output_holds_logged(state: AppState) -> None:
    try:
        await asyncio.to_thread(reap_orphan_holds, state.refs, state.outputs)
    except Exception:
        logger.exception("could not reap orphaned output holds")
        raise


async def _attach_backfills_logged(
    state: AppState, *, reraise: bool = True, stop: threading.Event | None = None
) -> None:
    """#902's backstop pass. The Schedule's sweep (``reraise``) fails its activity on an
    error; the boot's pass only logs it, and is ended by ``stop`` (`_join_background`)."""
    try:
        await asyncio.to_thread(
            attach_backfills,
            state.outputs,
            state.refs,
            state.render.store.read,
            state.events,
            stop,
        )
    except Exception:
        logger.exception("could not attach the finished output re-renders")
        if reraise:
            raise


def _preview_activities(state: AppState) -> list[Callable[..., Any]]:
    """The preview backfill's steps (#1054): what the boot's pass over every model did,
    on its Schedule. With previews off (a run left open from before), both do nothing."""

    @activity.defn(name=DUE_ACTIVITY)
    async def previews_due() -> list[str]:
        previews = state.previews
        if previews is None:
            return []
        try:
            return await asyncio.to_thread(previews.due)
        except Exception:
            logger.exception("could not list the models to render their previews")
            raise

    @activity.defn(name=REFRESH_ACTIVITY)
    async def preview_refresh(slug: str) -> bool:
        previews = state.previews
        if previews is None:
            return False
        try:
            return await _heartbeating(previews.refresh(slug, raise_unrun=True))
        except PreviewUnrunError as error:
            raise ApplicationError(str(error), type=UNRUN_FAILURE) from error
        except Exception:
            logger.exception("could not refresh a model's preview", extra={"slug": slug})
            raise

    return [previews_due, preview_refresh]


async def _prepare_catalogue(state: AppState) -> None:
    """The boot's passes over the catalogue, run before the render queue starts."""
    seed_dir = state.settings.resolve_seed_models_dir()
    if seed_dir is not None:
        await asyncio.to_thread(state.catalogue.sync_builtins, seed_dir)
    # After the sync, so the built-ins exist: a model the old seed copied in
    # becomes a duplicate of its built-in (#158). Contains its own failures.
    await asyncio.to_thread(state.catalogue.link_seeded)
    # A create or duplicate that died between claiming its slug and writing it left
    # an empty directory; it becomes a tombstone for the sweep below. Logs and skips
    # whatever it cannot read or move, so it never stops the boot.
    await asyncio.to_thread(state.catalogue.sweep_stranded_claims)
    # A delete that died between its rename and its rmtree left a tombstone.
    # Best effort, as it is after a delete: leftovers must not stop the boot.
    try:
        await asyncio.to_thread(state.catalogue.sweep_tombstones)
    except OSError:
        logger.exception("could not sweep tombstones")
    # A duplicate the process died in the middle of left its staging copy. Nothing
    # is duplicating yet: no request has been served.
    await _sweep_duplicate_staging_logged(state)
    # Before the orphan sweep, which forgets the saved presets of templates that are
    # gone: the database, and its migrations, as the projection opens it.
    await asyncio.to_thread(state.presets.open)
    # Derived files a failed or raced delete left keyed to a slug that is gone.
    # It logs and skips whatever it cannot read, so it never stops the boot.
    await asyncio.to_thread(state.catalogue.sweep_orphans)
    # And their previews, which are rows in the database rather than files.
    try:
        await asyncio.to_thread(state.catalogue.sweep_orphan_previews)
    except Exception:
        logger.exception("could not sweep orphaned previews")
    # A default render the process died in left its scratch directory, which the
    # orphan sweep never reads: no slug names it. Only one older than any render may
    # run goes, since another replica may be rendering into it. Whether or not
    # previews are on: one may be left from when they were.
    try:
        await asyncio.to_thread(
            sweep_work_dirs, state.paths, state.config.render_timeout * PREVIEW_TIMEOUT_FACTOR
        )
    except OSError:
        logger.exception("could not sweep preview scratch directories")
    # A library clone the process died in the middle of. Nothing is cloning yet:
    # no request has been served.
    try:
        await asyncio.to_thread(state.libraries.sweep_staging)
    except OSError:
        logger.exception("could not sweep library staging clones")
    # The curated libraries baked into the image (#169), so a fresh volume renders
    # a BOSL2 model offline. Before the queue starts: the first render finds them.
    seed_libraries_dir = state.settings.resolve_seed_libraries_dir()
    if seed_libraries_dir is not None:
        try:
            await asyncio.to_thread(seed_libraries, state.paths, seed_libraries_dir)
        except OSError:
            logger.exception("could not seed library checkouts from the image")
    # It logs and keeps what it cannot remove; one that cannot read the history removes nothing.
    try:
        await sweep_library_checkouts(state)
    except (OSError, GitError):
        logger.exception("could not sweep library checkouts")


async def _start_render(state: AppState) -> None:
    """Open (and migrate) the projection, prepare the catalogue, fail what a legacy
    queue left running, connect the in-process worker's client, prune, and start the
    service (which settles the pending rows no workflow will run, then prunes). A
    failure leaves the projection to the lifespan's guard, which closes everything a
    failed start opened, each once."""
    projection, service, settings = state.projection, state.render, state.settings
    await asyncio.to_thread(projection.open)
    # Only now is the `templates` table there (#7317, plan decision 2): the boot
    # commit `ensure_repo` made before it writes no rows; the start copy fills them.
    state.history.template_rows = state.catalogue.refresh
    # The readers too (#7317 phase 2): they read the rows once a copy run has finished.
    if state.catalogue.refresh is not None:
        state.catalogue.rows = state.catalogue.refresh.rows
    await _prepare_catalogue(state)
    # What a pre-Temporal release was running, nothing
    # will finish (#546).
    failed = await asyncio.to_thread(projection.fail_legacy_running)
    if failed:
        logger.warning(
            "failed the renders a pre-Temporal release left running",
            extra={"job_ids": [job.id for job in failed]},
        )
    if settings.temporal_worker_inprocess:
        # Eager: a worker cannot run on the API's lazy client (dev and tests).
        state.temporal = await connect(settings.temporal_address, settings.temporal_namespace)
    await service.prune()
    await service.start()


def _worker_exited(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
    """An in-process worker that ends before shutdown asked it to leaves renders
    pending: say so when it happens, not at shutdown."""
    if stop.is_set() or task.cancelled():
        return
    logger.error("the in-process render worker exited early", exc_info=task.exception())


async def _stop_worker(state: AppState, worker: asyncio.Task[None], deps: WorkerDeps) -> None:
    """Let the in-process worker drain for up to one activity, then cancel it."""
    try:
        await asyncio.wait_for(worker, state.config.activity_timeout)
    except TimeoutError:
        logger.warning("the in-process render worker did not drain in time; cancelled it")
    except Exception:
        logger.exception("the in-process render worker failed")
    if deps.thumbnail_executor is not None:
        deps.thumbnail_executor.shutdown(wait=False, cancel_futures=True)


#: How long the print worker waits before connecting again to a Temporal that is down.
PRINT_WORKER_RECONNECT = 5.0
#: How often the print worker task looks for runs whose execution is gone.
LOST_RUN_INTERVAL = 300.0


async def _connect_until(state: AppState, stop: asyncio.Event, name: str) -> Client | None:
    """The app's client, or one connected eagerly (a worker cannot run on the lazy
    client), retrying while Temporal is down; None once ``stop`` is set. A connect to a
    Temporal that never answers retries for a long time, so it is raced against
    ``stop``: the app's shutdown never waits it out."""
    settings = state.settings
    if state.temporal is not None:
        return state.temporal
    while not stop.is_set():
        connecting = asyncio.create_task(
            connect(settings.temporal_address, settings.temporal_namespace)
        )
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({connecting, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
        if not connecting.done():
            connecting.cancel()
            return None
        error = connecting.exception()
        if error is None:
            return connecting.result()
        logger.warning("the %s worker cannot reach Temporal yet; retrying", name, exc_info=error)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), PRINT_WORKER_RECONNECT)
    return None


async def _run_print_worker(state: AppState, stop: asyncio.Event) -> None:
    """Serve the ``bambuddy`` queue in this process until ``stop`` (#1052): only with
    SCADBUDDY_TEMPORAL_WORKER_INPROCESS or SCADBUDDY_TEMPORAL_PRINT_WORKER_INPROCESS;
    otherwise the ``scadbuddy-print`` worker serves it (#1060). Unversioned, on this
    process's stores and volume. It connects eagerly (a worker cannot run on the lazy
    client), retrying while Temporal is down, so the API still boots without it; print
    routes answer 503 meanwhile."""
    client = await _connect_until(state, stop, "print")
    if client is None:
        return
    settings = state.settings
    deps = PrintDeps(
        settings_store=state.settings_store,
        outputs=LocalOutputs(state.outputs, state.catalogue),
        prints=state.outputs.prints,
        uploads=state.uploads,
        store=state.print_runs.store,
        observer=state.print_progress,
        rack=state.components.get(RACK_USAGE),
        links=state.print_links,
        # This process's own native library when it serves (#3827).
        library=await state.components.get(PRINT_LIBRARY).serving_native(),
        native=state.components.get(SLICING).starter,
        printers=state.components.get(PRINTERS).store,
        reports=state.components.get(PRINTERS).reports,
        sb_spools=PoolSbSpoolReads.over(state.settings_store.pool),
    )
    ops = state.components.get(OPERATIONS)
    registry = state.components.get(PRINTERS)
    watches = WatchActivities(
        registry.store,
        registry.reports,
        state.events,
        rack=state.components.get(RACK_USAGE),
        settle_failed=state.metrics.rack_watch_settle_failed,
        # A LAN job start starts a print watcher pass on the library queue (#4699).
        nudge=Nudger(
            state.settings_store.load,
            registry.store.get,
            settings.temporal_task_queue_library,
            settings.temporal_task_queue_bambuddy,
        ).nudge,
        write_failed=state.metrics.printer_watch_write_failed,
    )
    content = state.store.content
    lan = LanDeps(
        printers=registry.store,
        settings_store=state.settings_store,
        library=deps.library,
        # A native slice's blob, from this process's own store (plan 2026-10-16 Task 9).
        blobs=ContentBlobs(content) if content is not None else None,
        rack=state.components.get(RACK_USAGE),
        # A run's next plate copy waits on the printer's report (#4541).
        reports=registry.reports,
        uses=PoolPrintUses(state.settings_store.pool),
        # A run queued on a busy printer says its place (#6871).
        runs=state.print_runs.store,
    )
    auto_imports = state.components.get(AUTO_IMPORTS)
    activities: list[Callable[..., Any]] = [
        *PrintActivities(deps).all(),
        *LanActivities(lan).all(),
        *operation_activities(ops.store, state.settings_store, _kinds_on(ops, "bambuddy")),
        # A LAN-only printer's job (#4699 phase 4), recorded through this process's own
        # store, as the CRUD route records the print worker's.
        LanImportActivities(
            registry.store,
            state.settings_store.load,
            state.components.get(PRINT_LIBRARY).serving_native,
            StoreLanRecorder(auto_imports.lan, state.catalogue.slugs, state.catalogue.exists),
        ).lan_auto_import,
        # The filament sync's pass (#6265) on this process's own stores, as the CRUD
        # routes run it for the print worker; it waits out a busy sync as that worker's
        # client waits out the routes' 503 `sync_busy` (#6910 review).
        FilamentSyncActivities(
            InProcessFilamentSync(state.components.get(FILAMENT), state.settings_store),
            settings_loader(state.settings_store.load),
        ).sync_pass,
        # The Bambuddy pull's reads (#6772) on this process's own stores, as the CRUD
        # routes apply them for the print worker.
        FilamentPullActivities(
            LocalBbPull(state.components.get(FILAMENT), state.settings_store.load),
            settings_loader(state.settings_store.load),
        ).pull_read,
        # The sensors' alert notifications (#6646) on this process's own pool, as the
        # CRUD routes claim and end them for the print worker.
        SensorNotifyActivities(
            PoolSensorNotifications(state.settings_store.pool),
            settings_loader(state.settings_store.load),
        ).deliver,
    ]
    while not stop.is_set():
        # A worker that fails is said at once and started again: until then every
        # print run waits on a queue nothing polls.
        queue = settings.temporal_task_queue_bambuddy
        workers = [
            bambuddy_worker(client, queue, activities),
            follow_worker(
                client,
                queue,
                FollowActivities(
                    state.print_follower, running=state.metrics.print_follows_running
                ).follow_print,
                printer_watch=watches.printer_watch,
            ),
        ]
        if not await _serve_until(workers, stop):
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), PRINT_WORKER_RECONNECT)


def serves_print_queue(settings: Settings) -> bool:
    """Whether the API process serves the ``bambuddy`` queue itself (#1060)."""
    return settings.temporal_worker_inprocess or settings.temporal_print_worker_inprocess


async def _print_upkeep(state: AppState, stop: asyncio.Event) -> None:
    """What the API does for print runs whoever serves their queue (#1060): end the runs
    and operations whose execution closed without ending them, until ``stop``. Postgres
    and a client only."""
    client = await _connect_until(state, stop, "print upkeep")
    if client is None:
        return
    ops = state.components.get(OPERATIONS)
    where = {name: kind.where for name, kind in ops.kinds.items()}
    await _end_lost_runs_until(client, state.print_runs.store, ops.store, stop, where)


#: The ``library`` worker's workflows beside ``Housekeeping`` (`library_worker` adds it),
#: each with its own versioning behavior (#4090): every one is AUTO_UPGRADE, and each
#: class says why. The deployment has no default, so a workflow added here without one
#: stops the worker from being built.
LIBRARY_WORKFLOWS: tuple[type, ...] = (
    VersionedPreviewBackfill,
    VersionedOperationWorkflow,
    VersionedFlowTriggerFire,
    VersionedStoreMigration,
    VersionedLibraryCopy,
    VersionedSliceWorkflow,
    VersionedSpoolRefresh,
    VersionedStagedFile,
    VersionedPrepareListing,
    VersionedAttemptMedia,
)


#: How long the library Schedules wait for this build to route the queue before they
#: are set up anyway (Temporal refusing the build, say), and how often it is asked: as
#: long as a versioned worker waits to report ready (`worker.ROUTED_WAIT`).
LIBRARY_ROUTED_WAIT = ROUTED_WAIT
LIBRARY_ROUTED_POLL = ROUTED_POLL


async def _make_library_current(
    client: Client, build_id: str, queue: str, routed: asyncio.Event
) -> None:
    """Make this API build the ``scadbuddy-library`` deployment's current version, as
    the render worker does its own (`worker._serve_versioned`, #4090): a new replica
    then takes every new task of the queue, and an old one only finishes the activity
    attempts it already holds. It gives up once a newer build is current (#2538). Then
    it waits, up to `LIBRARY_ROUTED_WAIT`, for the queue itself to route to this build
    (`routes_to`), and sets ``routed`` whatever came of it. The wait narrows the window
    in which a sweep only this build has reaches an old replica; it does not close it,
    as a non-root partition of the queue can lag its root (#4255). Such a sweep fails
    and runs again at its next tick. An error other than Temporal's is logged, not
    raised: the worker keeps serving."""
    set_current, read_current = deployment_writes(client, build_id, LIBRARY_DEPLOYMENT_NAME)
    loop = asyncio.get_running_loop()
    try:
        await make_current_until_polled(
            set_current,
            build_id=build_id,
            backoff=MAKE_CURRENT_BACKOFF,
            every=MAKE_CURRENT_EVERY,
            error_after=MAKE_CURRENT_ERROR_AFTER,
            error_every=MAKE_CURRENT_ERROR_EVERY,
            current=read_current,
        )
        until = loop.time() + LIBRARY_ROUTED_WAIT
        while loop.time() < until:
            with suppress(RPCError):
                if await routes_to(
                    client, queue, deployment_name=LIBRARY_DEPLOYMENT_NAME, build_id=build_id
                ):
                    return
            await asyncio.sleep(LIBRARY_ROUTED_POLL)
        # An error: the Schedules are set up now anyway, and a sweep this build added
        # that the old build is handed fails, unseen, until its next tick.
        logger.error(
            "the library queue does not route to this build yet", extra={"build_id": build_id}
        )
    except Exception:
        logger.exception(
            "could not make this build current for the library queue",
            extra={"build_id": build_id},
        )
    finally:
        routed.set()


async def _import_presets(state: AppState) -> PresetImport:
    """The spool refresh's presets import (#7244), from the configured Bambuddy."""
    loaded = await asyncio.to_thread(state.settings_store.load)
    return await import_from_bambuddy(state.settings_store.pool, state.store.blobs, loaded)


async def _built_off_loop[T](build: Callable[[], T]) -> T:
    """``build()`` in a thread (#7359). A Temporal ``Worker`` runs each workflow once in
    the sandbox to check it, which imports the workflow's modules: on an API pod that
    had just started that held the loop 2 s. The check's workflow instance asks for the
    running loop, so the thread runs one of its own for the build; nothing the build
    makes stays bound to it."""

    async def built() -> T:
        return build()

    return await asyncio.to_thread(asyncio.run, built())


async def _run_library_worker(state: AppState, stop: asyncio.Event) -> None:
    """Serve the ``library`` queue until ``stop`` (#1054): the housekeeping Schedule's
    sweeps and the preview backfill need the data volume this process holds. Once
    connected it sets the Schedules up; Temporal down at boot only delays that.

    The worker is this build's version of ``scadbuddy-library`` (#4090), in every mode
    (`SCADBUDDY_TEMPORAL_WORKER_INPROCESS` too), and makes its build current once, at
    the first connect, not at each restart of the worker. It never drains on stop: its
    workflows are all AUTO_UPGRADE, so the current build takes over what an old replica
    leaves (README, "Deploying")."""
    client = await _connect_until(state, stop, "library")
    if client is None:
        return
    # #6265 Decision 17: once Temporal answers, a full filament sync pass, for what
    # changed while no replica nudged.
    boot_nudge = asyncio.create_task(state.filament_sync.nudge(full=True))
    settings = state.settings
    queue = settings.temporal_task_queue_library
    build_id = settings.revision

    routed = asyncio.Event()

    async def set_up() -> None:
        # After this build routes the queue (#4090): each set-up triggers a run with this
        # build's sweeps, which a previous build still routed to would fail as
        # unregistered.
        with suppress(TimeoutError):
            await asyncio.wait_for(routed.wait(), LIBRARY_ROUTED_WAIT)
        paused = await _set_up_housekeeping(
            client,
            queue,
            state.config.asset_sweep_interval,
            state.previews.timeout if state.previews is not None else None,
            stop,
            spool_refresh=settings.spool_overlay_refresh_seconds,
            print_queue=settings.temporal_task_queue_bambuddy,
        )
        if paused and state.store.content is not None:
            # Its sweep would backfill the uploads (review #1095 2); paused, the start does.
            await _backfill_store_logged(state, uploads=True, fonts=False)

    schedules = asyncio.create_task(set_up())
    # Once per process, beside the restart loop below, as the render worker does at its
    # start: a worker restarted late in a rollout must not take the deployment back from
    # a newer build (`make_current_until_polled` writes on its first attempt).
    current = asyncio.create_task(_make_library_current(client, build_id, queue, routed))
    ops = state.components.get(OPERATIONS)
    trigger_activities, stop_triggers = _flow_triggers(state, client, queue, stop)
    activities = [
        *_housekeeping_activities(state),
        *_preview_activities(state),
        # The retired spool-provider activities, for runs opened before
        # spool-providers-gone (#6262 Task 12).
        *spool_activities(),
        # Bambuddy's spools pulled into ScadBuddy's own (#6261), on the spools' Schedule:
        # read by the print worker, which sends the rows to the CRUD routes (#6772).
        # Then, while `filament_sync.presets` pulls, Bambuddy's filament presets into
        # ScadBuddy's own, read from Bambuddy (#7244).
        filament_pull_activity(
            state.components.get(FILAMENT),
            lambda: state.settings_store.load(),
            lambda: state.settings_store.read({"filament_sync"})["filament_sync"],
            partial(execute_pull, client, settings.temporal_task_queue_bambuddy),
            partial(_import_presets, state),
            lambda: mirror_after_pull(
                state.settings_store.pool, state.store.blobs, state.settings_store.load
            ),
        ),
        # The printers' trays reconciled with the ledger, the watch's backstop (#6263).
        capture_backstop_activity(state.components.get(PRINTERS)),
        # The tag bindings ScadBuddy changed, told to Bambuddy (#6264).
        filament_rfid_activity(
            state.components.get(PRINTERS),
            state.components.get(FILAMENT),
            lambda: state.settings_store.load(),
            lambda: state.settings_store.read({"filament_sync"})["filament_sync"],
        ),
        *operation_activities(ops.store, state.settings_store, _kinds_on(ops, "library")),
        *trigger_activities,
        migrate_activity(lambda: state.settings_store.pool, state.settings),
        *library_copy_activities(
            lambda: state.settings_store.pool,
            lambda: state.settings_store.load(),
            state.components.get(PRINT_LIBRARY),
            lambda: state.uploads,
        ),
        *slice_activities(
            state.components.get(SLICING),
            state.components.get(PRINT_LIBRARY),
            lambda: state.settings_store,
            lambda: state.uploads,
        ),  # Each staged customer file's watcher (#6133).
        *staged_file_activities(state),
        # Each order attempt's media import (#7233).
        _attempt_media_activity(state),
        # A listing's prepared colour combinations (#6237, #6299 review).
        *prepare_activities(state, lambda: state.components.get(ORDERS)),
    ]
    try:
        while not stop.is_set():
            worker = await _built_off_loop(
                partial(
                    library_worker,
                    client,
                    queue,
                    activities,
                    workflows=LIBRARY_WORKFLOWS,
                    workflow_runner=sandboxed_runner(),
                    graceful_shutdown_timeout=LIBRARY_GRACEFUL_SHUTDOWN,
                    deployment_config=library_deployment(build_id),
                )
            )
            if not await _serve_until([worker], stop, name="library"):
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), PRINT_WORKER_RECONNECT)
    finally:
        current.cancel()
        schedules.cancel()
        boot_nudge.cancel()
        await stop_triggers()


def _flow_triggers(
    state: AppState, client: Client, queue: str, stop: asyncio.Event
) -> tuple[list[Callable[..., Any]], Callable[[], Coroutine[Any, Any, None]]]:
    """Flow triggers (#2484) on the library worker: their fires run here, beside the data
    volume; their Schedules are put right now and every `RECONCILE_INTERVAL`; their events
    are heard by every replica. The fire activities, and what stops the rest."""
    triggers = state.components.get(FLOW_TRIGGERS)
    schedules = asyncio.create_task(
        reconcile_trigger_schedules(client, queue, triggers.store, stop)
    )
    unlisten = listen_for_triggers(state.events, client, queue, triggers.store)

    async def stop_all() -> None:
        schedules.cancel()
        await unlisten()

    return TriggerFires(triggers.store, lambda: triggers.flows, client, queue).all(), stop_all


def _kinds_on(ops: OperationCommands, queue: Queue) -> dict[str, OperationKind]:
    """The kinds one worker serves (§4.3): only those whose effect it holds."""
    return {name: kind for name, kind in ops.kinds.items() if kind.queue == queue}


async def _set_up_housekeeping(
    client: Client,
    queue: str,
    interval: float,
    previews: float | None,
    stop: asyncio.Event,
    *,
    spool_refresh: float,
    print_queue: str | None = None,
) -> bool:
    """The housekeeping Schedules, the spool refresh (Bambuddy's spools pulled, its
    presets while ``filament_sync.presets`` pulls, and the filament capture backstop,
    every ``spool_refresh`` seconds; 0: none, #3818, #6262, #6263, #7244), the sensor
    notifications' pass on ``print_queue`` (the ``bambuddy`` queue, every minute, #6646)
    and the
    preview backfill's (``previews``: the scheduler's render bound; None, previews off:
    none), retried until Temporal takes them: a frontend can answer the connect before
    it can create one. Each set-up triggers its Schedule, so the preview one comes last:
    a retry after the housekeeping ones failed does not run the backfill twice (a refresh
    or a notification pass run twice is only a pass). True when the sweeps' one is
    paused."""
    while not stop.is_set():
        try:
            paused = await ensure_schedules(client, queue, interval)
            await ensure_spool_schedule(client, queue, spool_refresh)
            if print_queue is not None:
                await ensure_sensor_notify_schedule(client, print_queue)
            await ensure_preview_schedule(client, queue, previews)
            return paused
        except Exception:
            logger.warning("could not set up the library Schedules; retrying", exc_info=True)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), PRINT_WORKER_RECONNECT)
    return False


async def _serve_until(
    workers: Sequence[Worker],
    stop: asyncio.Event,
    alongside: Coroutine[Any, Any, None] | None = None,
    *,
    name: str = "print",
) -> bool:
    """Run ``workers`` (and ``alongside``) until ``stop``: True. A worker that ends
    first, failed or not, is said at once (review #1061: a poller that dies while running
    would otherwise leave the queue unpolled until the pod restarts), and the others
    are shut down so all start again together: False."""
    running = [asyncio.create_task(worker.run()) for worker in workers]
    beside = asyncio.create_task(alongside) if alongside is not None else None
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({*running, stopping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if beside is not None:
            beside.cancel()
        stopping.cancel()
    ended = [task for task in running if task.done()]
    for task in ended:
        error = task.exception()
        logger.error(
            "the %s worker failed; starting it again",
            name,
            exc_info=error if error is not None else RuntimeError("the worker stopped"),
        )
    for worker, task in zip(workers, running, strict=True):
        if not task.done():
            await worker.shutdown()
            with suppress(Exception):
                await task
    return not ended


async def _end_lost_runs_until(
    client: Client,
    store: PrintRunStore,
    operations: OperationStore,
    stop: asyncio.Event,
    where: Mapping[str, str],
) -> None:
    """Every ``LOST_RUN_INTERVAL`` until ``stop``, end the print runs (review #1061) and
    the operations (review #1063) whose execution closed without ending them: one
    terminated in the Temporal UI. ``where``: each operation kind's what-to-check."""
    while not stop.is_set():
        try:
            ended = await reconcile_lost_runs(client, store)
            if ended:
                logger.warning("ended print runs whose execution was gone", extra={"count": ended})
        except Exception:
            logger.exception("could not check print runs for lost executions")
        try:
            ended = await reconcile_lost_operations(client, operations, where=where)
            if ended:
                logger.warning("ended operations whose execution was gone", extra={"count": ended})
        except Exception:
            logger.exception("could not check operations for lost executions")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), LOST_RUN_INTERVAL)


async def _stop_queue_worker(
    task: asyncio.Task[None] | None, name: str, timeout: float | None = None
) -> None:
    """Wait for the ``name`` queue's worker task (print, library) to stop."""
    if task is None:
        return
    try:
        await asyncio.wait_for(task, PRINT_WORKER_STOP_TIMEOUT if timeout is None else timeout)
    except TimeoutError:
        logger.warning("the %s worker did not stop in time; cancelled it", name)
    except Exception:
        logger.exception("the %s worker failed", name)


#: The worker's own graceful shutdown (30 s) and a margin.
PRINT_WORKER_STOP_TIMEOUT = 40.0
#: What the library worker's stop gives a running sweep before cancelling it.
LIBRARY_GRACEFUL_SHUTDOWN = timedelta(seconds=30)
#: Its graceful shutdown, then a cancelled sweep's join (#1708), and a margin.
LIBRARY_WORKER_STOP_TIMEOUT = LIBRARY_GRACEFUL_SHUTDOWN.total_seconds() + SWEEP_SHUTDOWN_JOIN + 10.0


def _build_route_contexts(app: FastAPI) -> None:
    """Build what FastAPI builds on the first walk of the routes: each route's
    dependencies, signatures read with their annotations evaluated (#7359). The server
    span's name (`FastAPIInstrumentor`) comes from a walk of every route, so the first
    traced request made it, on the loop: 1.1 to 9.7 s on a new API pod, measured from
    `core/loop_watch.py`'s stacks on prod, 2026-11-24. FastAPI keeps it until the routes
    change, and nothing adds one after `create_app`. Blocking: run it off the loop."""
    for _ in iter_route_contexts(app.routes):
        pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state: AppState = getattr(app.state, STATE_ATTR)
    # First: without its database ScadBuddy has no settings, so it does not start.
    # It also brings the schema up to date, before the projection opens.
    await asyncio.to_thread(state.settings_store.open)
    # Then the agent's (#5299 D6), which the agent waits for.
    try:
        await asyncio.to_thread(_migrate_agent_at_start, state)
    except BaseException:
        await asyncio.to_thread(state.settings_store.close)
        raise
    # #4975 phase 4: the service credentials, before any caller reads its own.
    await asyncio.to_thread(_grant_and_rotate_at_start, state)
    # #5299 D5: a KEK rotation re-wraps the agent's credentials and payload keys here.
    await asyncio.to_thread(_rotate_agent_secrets, state)
    # #4975 phase 8 (spec 2026-11-01 §9.1): `enforce` without a usable login stops here.
    if state.settings.auth_mode == "enforce":
        auth = state.components.get(AUTH)
        try:
            public_url = await asyncio.to_thread(lambda: state.settings_store.load().public_url)
            await check_enforce(auth, public_url)
        except BaseException:
            await asyncio.to_thread(auth.close)
            await asyncio.to_thread(state.settings_store.close)
            raise
    # Then what the UI saved (#322), before anything below is sized or started from it.
    snapshot = await asyncio.to_thread(state.settings_store.snapshot)
    apply_runtime(state, snapshot.runtime, booting=True)
    # The blob store (#426): its index and the render settings live in the settings
    # pool, open only from here on.
    pool = state.settings_store.pool
    # #7477 review I2: a save's stage hold takes a connection of a small pool of its own,
    # never the shared one, and waits for a slot of it before its first upload; a
    # removal's exclusive lock takes that pool's reserved connection (#7756 review).
    stage_locks = StageLocks(state.settings_store.stage_lock_pool, STAGE_LOCK_SLOTS)
    source = RenderSettingsSource(pool, state.settings)
    try:
        current = await asyncio.to_thread(load_render_store_settings, pool, state.settings)
        source.seed(current)
        state.store = build_store(
            backend=current.store_backend,
            current=current,
            config=state.config,
            paths=state.paths,
            pool=pool,
            index=BlobIndex(pool, stage_locks=stage_locks),
            source=source,
            history=state.history,
            fonts=state.fonts,
            metrics=state.metrics,
            # #2788: on the API's /data, so every replica shares it, unless set
            # elsewhere: a node-local one (#7316) may be marked this container's alone.
            downloads_dir=state.config.download_cache_dir or state.paths.cache / DOWNLOADS_DIRNAME,
            downloads_private=state.config.download_cache_dir is not None
            and state.config.download_cache_private,
        )
        # Plan 2026-10-17 Ruling 3: from any source, the environment's seed included
        # (a save never checks a seeded value), `native` needs a content store.
        if snapshot.stored.library_backend == "native" and state.store.content is None:
            await state.store.aclose()
            raise StoreNotReadyError(
                f"{NATIVE_LIBRARY_NOT_READY}. To start on"
                f" Bambuddy's library, run {RECOVER_BAMBUDDY_LIBRARY_SQL} in ScadBuddy's"
                " database, in the schema its SCADBUDDY_DATABASE_URL uses (or, when no"
                " library_backend is stored, set SCADBUDDY_LIBRARY_BACKEND=bambuddy)."
            )
    except BaseException:
        # A refused start (an unready store) closes the pool it opened, then propagates.
        await asyncio.to_thread(state.settings_store.close)
        raise
    state.blobs = state.store.blobs
    state.render.snapshots = state.store.snapshots
    # #7256: the outputs' bytes are read from the blob store first, never on `bambuddy`.
    # #7257 plan D7: `local` has no content store, so outputs get one of their own under
    # `data/output-blobs/`, with the shared index and spool. It has no caps, as the `local`
    # store has none.
    #: `local`'s output store, which the bundle does not close.
    output_content: ContentStore | None = None
    if state.store.backend == "local":
        output_content = ContentStore(
            LocalContentBackend(state.paths.output_blobs),
            BlobIndex(pool, stage_locks=stage_locks),
            spool=blob_spool(state.config, state.paths),
        )
        state.outputs.bytes = OutputBytes(output_content)
    elif usable(state.store.content):
        assert state.store.content is not None
        state.outputs.bytes = OutputBytes(state.store.content)
    # From the store's construction to the `try` below, whose `finally` owns shutdown:
    # a failure anywhere here (a refused OpenSCAD probe, a render service or bus that
    # will not start) releases what is open, the store and its pool included, rather
    # than leaking it for this failed boot.
    try:
        # Spec 2026-10-16 §4.3: once the store is ready, the rows a migration copied to
        # its backend are that backend's.
        switched = await asyncio.to_thread(BlobIndex(pool).switch_migrated, current.store_backend)
        if switched:
            logger.info(
                "switched migrated blobs",
                extra={"backend": current.store_backend, "rows": switched},
            )
        state.paths.ensure(output_blobs=output_content is not None)
        # Before the built-in sync: an existing models directory becomes revision 1,
        # so what a newer image changes in a built-in is a commit on top of it rather
        # than an unversioned overwrite.
        await asyncio.to_thread(state.history.ensure_repo)
        # Before anything shells out to openscad or fc-list: it is what points
        # fontconfig at the fonts on the data volume.
        state.fonts.prepare()
        state.openscad_version = await probe_openscad_version(state.config)
        await _start_render(state)
        # After the projection, which migrated the database: the bus writes the event
        # log, and `start` refuses (EventLogMissingError) if it is not there yet. What
        # was published before now (the built-in sync's commits) waited.
        if isinstance(state.events, PgNotifyEventBus):
            await state.events.start()
    except BaseException:
        await _close_quietly(state)
        await asyncio.to_thread(state.settings_store.close)
        raise

    # Everything from here holds the render service's resources (the Postgres pool,
    # its pruner), so it runs inside the `try` whose `finally` releases them: a
    # failure while starting up closes them as a shutdown does, rather than leaking.
    library: asyncio.Task[None] | None = None
    stop_library = asyncio.Event()
    backfill: asyncio.Task[None] | None = None
    # A change saved on any replica, this one's included, applies its live fields here.
    unfollow = follow_changes(state)
    unfollow_backfills: Callable[[], Coroutine[Any, Any, None]] | None = None
    attach_now: asyncio.Task[None] | None = None
    adopt_now: asyncio.Task[None] | None = None
    index_now: asyncio.Task[None] | None = None
    copy_templates_now: asyncio.Task[None] | None = None
    copy_now: asyncio.Task[None] | None = None
    stop_background = threading.Event()
    components = AsyncExitStack()
    worker: tuple[asyncio.Task[None], WorkerDeps] | None = None
    stop = asyncio.Event()
    stop_printing = asyncio.Event()
    printing: asyncio.Task[None] | None = None
    upkeep: asyncio.Task[None] | None = None
    try:
        # #7245: the API's own loop and default executor, as each worker watches its own
        # (#3788). Its request handlers, its library worker and its in-process workers
        # share them.
        await components.enter_async_context(LoopWatch(state.metrics))
        # Every component's `run` (`core/components.py`), now that the database and
        # the bus are up. One that fails exits those already running and fails the
        # boot; closed below, before the queue it may be using.
        await components.enter_async_context(state.components.running())
        # #6265 Decision 12: before the first nudge, so before any write.
        await _adopt_bb_instance_logged(state)
        if state.temporal is not None:
            deps = worker_deps_from_state(state)
            task = asyncio.create_task(
                run_inprocess_worker(state.settings, deps, state.temporal, stop)
            )
            task.add_done_callback(partial(_worker_exited, stop))
            worker = (task, deps)
        # #902: a finished re-render is attached on its event, by every replica;
        # the housekeeping Schedule's `BACKFILL_SWEEP` catches one none heard.
        unfollow_backfills = follow_backfills(
            state.events,
            partial(
                attach_job_backfills,
                state.outputs,
                state.refs,
                state.render.store.read,
                events=state.events,
            ),
        )
        # And once now, whatever the Schedule: a re-render that settled while no replica
        # was listening (the sweeps' interval 0, or the Schedule paused).
        attach_now = asyncio.create_task(
            _attach_backfills_logged(state, reraise=False, stop=stop_background),
            name="attach backfills",
        )
        # #4777: every saved output with no row in the index gets one, and a row whose
        # output is gone goes; in the background, as an output with no row is only
        # missing from the cross-model list meanwhile.
        index_now = asyncio.create_task(
            _index_outputs_logged(state, stop=stop_background), name="index outputs"
        )
        # #7317: every template's files copied to its row, and the rows reconciled with
        # the folders, after `_start_render` made the table and synced the built-ins. The
        # housekeeping sweep `TEMPLATE_COPY_SWEEP` runs it again.
        copy_templates_now = asyncio.create_task(
            _copy_templates_logged(state, stop=stop_background), name="copy templates"
        )
        # #7255: every saved output's files copied to its row, and the rows reconciled
        # with the files; until a copy run finishes, a missing row falls back to them.
        # The housekeeping sweep `OUTPUT_COPY_SWEEP` runs it again, for older pods' writes.
        # #7256: then their bytes to the blob store (`OUTPUT_BYTES_SWEEP` again).
        copy_now = asyncio.create_task(
            _copy_outputs_and_bytes_logged(state, stop=stop_background), name="copy outputs"
        )
        # #5236: a hand-made row a Bambuddy virtual printer's serial names becomes that
        # VP's, with the sweeps off too.
        adopt_now = asyncio.create_task(
            _adopt_virtual_rows_logged(state), name="adopt virtual printers"
        )
        # Print runs (#1052): the `scadbuddy-print` worker serves the `bambuddy` queue,
        # or this process does when told to (#1060); the upkeep is this process's.
        if serves_print_queue(state.settings):
            printing = asyncio.create_task(_run_print_worker(state, stop_printing))
        upkeep = asyncio.create_task(_print_upkeep(state, stop_printing))
        # Housekeeping (#1054): a Schedule on the `library` queue this process serves,
        # run once at once (the boot's sweep of the uploads, converging with the store;
        # review #1095 1: the boot no longer walks them itself first), then every interval.
        library = asyncio.create_task(_run_library_worker(state, stop_library))
        if state.store.content is not None:
            backfill = asyncio.create_task(
                _backfill_store_logged(
                    state,
                    uploads=state.config.asset_sweep_interval == 0,
                    stop=stop_background,
                ),
                name="store backfill",
            )
        # The per-change requests. The pass over every model is the preview
        # backfill's Schedule (#1054), triggered once the library worker connects.
        if state.previews is not None:
            state.previews.start()
        # #7359: every route's dependencies, before either port takes a request.
        await asyncio.to_thread(_build_route_contexts, app)
        # #5298 (plan D3): the workers' CRUD port, once the app can answer.
        await crud_listener.start(app, state.settings.crud_port)
        logger.info(
            "scadbuddy started",
            extra={
                # The deploy provenance stamped into the image (what /healthz
                # reports), not the package version, which is not bumped per deploy.
                "version": state.settings.version,
                "revision": state.settings.revision,
                "data_dir": str(state.paths.root),
                "openscad_version": state.openscad_version,
            },
        )
        yield
    finally:
        # First: no worker's CRUD call starts on stores closing below.
        await crud_listener.stop(app)  # never raises: the rest of the shutdown runs
        unfollow()
        if state.previews is not None:
            await state.previews.aclose()
        stop_library.set()
        await _stop_queue_worker(library, "library", LIBRARY_WORKER_STOP_TIMEOUT)
        if unfollow_backfills is not None:
            await unfollow_backfills()
        await _join_background(
            stop_background,
            backfill,
            attach_now,
            adopt_now,
            index_now,
            copy_templates_now,
            copy_now,
        )
        stop_printing.set()
        await _stop_queue_worker(printing, "print")
        await _stop_queue_worker(upkeep, "print upkeep")
        # Each close runs even when one before it raises, so a component (or a pool)
        # failing on its way out cannot leak the pools and the bus's listener (#653).
        # The stack unwinds last-pushed first: pushed here in reverse of the order run.
        async with AsyncExitStack() as closing:
            closing.push_async_callback(asyncio.to_thread, state.settings_store.close)
            closing.push_async_callback(state.store.aclose)
            if output_content is not None:
                closing.push_async_callback(output_content.aclose)
            closing.push_async_callback(state.events.aclose)
            closing.push_async_callback(asyncio.to_thread, state.presets.close)
            closing.push_async_callback(asyncio.to_thread, state.projection.close)
            closing.push_async_callback(state.render.aclose)
            closing.push_async_callback(components.aclose)
            if worker is not None:
                # Pushed last, so it runs first: the worker stops before the queue and
                # the pools it uses close, and its failing still lets them close (#3125).
                stop.set()
                closing.push_async_callback(_stop_worker, state, *worker)


def create_app(settings_override: Settings | None = None) -> FastAPI:
    app_settings = settings_override or Settings()
    configure_logging(app_settings.log_level)
    configure_tracing(
        "scadbuddy-api",
        version=app_settings.version,
        revision=app_settings.revision,
        inprocess_worker=app_settings.temporal_worker_inprocess,
    )

    app = FastAPI(
        title="ScadBuddy",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        # FastAPI (0.142) would otherwise add exporters of its own to
        # OTEL_EXPORTER_OTLP_ENDPOINT: spans past the scrub, which wraps only ours
        # (`core/tracing.py`), and every log line. Only `core/tracing.py` exports.
        telemetry={"auto_configure": False},
        # #2794: whether a request follows a 202 itself, for `run_operation`.
        # #4975: what SCADBUDDY_AUTH_MODE=enforce would refuse, counted in observe.
        dependencies=[Depends(note_preference), Depends(auth_check)],
    )
    state = build_state(app_settings)
    setattr(app.state, STATE_ATTR, state)
    install_problem_handlers(app)
    install_accepting_handler(app)
    libraries.install_library_handlers(app)
    models.install_model_handlers(app)
    # A commit made for the agent is authored as the agent (#252), and only the agent's
    # and the projects worker's credentials name an author (#4975 phase 5).
    app.add_middleware(
        AgentAuthorship,
        rule=AuthorshipRule(
            mode=lambda: state.settings.auth_mode,
            run_key=lambda: state.components.get(AUTH).run_token_key(),
            pool=lambda: state.components.get(AUTH).pool,
        ),
    )
    # What each read saw of the event bus, so the UI need not read twice (#1039).
    app.add_middleware(EventCursor, events=lambda: state.events)
    # Outside everything that reads a body, so an oversized one is refused on its
    # headers rather than buffered.
    app.add_middleware(
        BodySizeGate,
        limits=BODY_LIMITS,
        routes=[
            RouteLimit(
                "POST",
                MEDIA_UPLOAD_PATH,
                # The value in effect, which Settings can change while running (#322).
                lambda: state.settings.media_upload_max_bytes,
                "a media upload",
                "the upload limit in Settings, seeded by SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES",
            ),
            RouteLimit(
                "POST",
                ATTACHMENT_UPLOAD_PATH,
                lambda: state.settings.media_upload_max_bytes,
                "a print's photo or video",
                "the upload limit in Settings, seeded by SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES",
            ),
            RouteLimit(
                "POST",
                ORDER_ATTACHMENT_UPLOAD_PATH,
                lambda: state.settings.media_upload_max_bytes,
                "an order's attachment",
                "the upload limit in Settings, seeded by SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES",
            ),
            RouteLimit(
                "POST",
                ENTITY_ATTACHMENT_UPLOAD_PATH,
                lambda: state.settings.media_upload_max_bytes,
                "an entity's attachment",
                "the upload limit in Settings, seeded by SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES",
            ),
            RouteLimit(
                "POST",
                INVOICE_FILE_UPLOAD_PATH,
                lambda: state.settings.media_upload_max_bytes,
                "an invoice's file",
                "the upload limit in Settings, seeded by SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES",
            ),
            RouteLimit(
                "POST",
                MESH_UPLOAD_PATH,
                MAX_DOWNLOAD_BYTES + 64 * 1024,  # the file plus the form's framing
                "a mesh upload",
                "the library download cap (bambuddy/print_source.py MAX_DOWNLOAD_BYTES)",
            ),
            RouteLimit(
                "POST",
                LIBRARY_UPLOAD_PATH,
                MAX_DOWNLOAD_BYTES + 64 * 1024,  # the file plus the form's framing
                "a library upload",
                "the library download cap (bambuddy/print_source.py MAX_DOWNLOAD_BYTES)",
            ),
            # The browser trace relay's 256 KiB (spec 2026-10-01 §5.2).
            telemetry.RELAY_ROUTE_LIMIT,
            # A task's answer is small (#7364).
            tasks.ANSWER_ROUTE_LIMIT,
            # The slicer worker's upload (plan 2026-10-16-native-slicer-rust-worker).
            internal_blobs.PUT_ROUTE_LIMIT,
            # A native run's per-print copy from the print worker (#3818 tier A Task 4).
            internal_library.PUT_ROUTE_LIMIT,
            # The Bambuddy pull's bodies from the print worker (#6772, #6861).
            crud_print.PULL_ROUTE_LIMIT,
            # The agent store's transcript, turn and event-log writes, at their declared
            # bounds, which are above the 8 MiB text default (#6863).
            crud_agent_transcripts.ROUTE_LIMIT,
            crud_agent_turns.ROUTE_LIMIT,
            crud_agent_events.ROUTE_LIMIT,
            # A plugin package's archive or npm tarball, mirrored by the agent (#6935).
            crud_agent_packages.MIRROR_ROUTE_LIMIT,
        ],
    )
    # Who the request is from (#4975), inside the cross-site gate (spec 2026-11-01 §8.1).
    # In SCADBUDDY_AUTH_MODE=off it refuses nothing.
    app.add_middleware(
        AuthGate,
        sessions=lambda: state.components.get(AUTH).sessions,
        credentials=lambda: state.components.get(AUTH).credentials,
        tokens=lambda: state.components.get(AUTH).tokens,
    )
    # A write from a page on another origin is refused before its body is read (#962).
    app.add_middleware(
        CrossSiteGate,
        public_url=lambda: state.settings_store.get(PUBLIC_URL),
        allowed_origins=lambda: state.settings.allowed_origin_list,
    )
    # Outside every route and gate, so their answers go out gzipped too (#1033).
    app.add_middleware(Compression)
    # Outermost of all (added last): the gate answers a 413 itself without calling
    # inward, so a counter inside it would never see the requests most worth
    # counting. It reads no body, so wrapping the gate costs the gate nothing.
    app.add_middleware(HttpMetrics, metrics=state.metrics)

    app.include_router(health.router)
    app.include_router(metrics.router)
    # The browser trace relay: at the root like the two above, before the SPA's mount.
    app.include_router(telemetry.router)
    # The prefix is given here, not to the router: with none, FastAPI (0.165) builds every
    # route's context to check for an empty path, and drops them.
    app.include_router(_api_router(), prefix=API_PREFIX)
    _name_in_openapi(
        app,
        models.PastedSource,
        media.MediaUpload,
        mesh_models.MeshUpload,
        library_print.LibraryFileUploadForm,
        print_attachments.PrintAttachmentUpload,
        order_attachments.OrderAttachmentUpload,
        order_attachments.RevisionAttachmentUpload,
        entity_attachments.EntityAttachmentUpload,
        outputs.NeedsBackfillProblem,
    )

    # Last, so every API route above wins the match; unknown paths fall back to index.html.
    frontend = app_settings.resolve_frontend_dir()
    if frontend is not None:
        app.mount("/", SPAStaticFiles(frontend), name="frontend")
    else:
        logger.info("no frontend bundle found; serving the API only")
    # Outermost, so the server span covers every middleware, the body gate included.
    FastAPIInstrumentor.instrument_app(
        app, excluded_urls=EXCLUDED_URLS, exclude_spans=["receive", "send"]
    )
    return app
