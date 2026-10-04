from __future__ import annotations

import asyncio
import logging
import math
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Path, status
from psycopg import Connection
from starlette.requests import HTTPConnection
from temporalio.client import Client

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.follow import Follower
from scadbuddy.bambuddy.print_links import PrintLinkStore
from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver, progress_for
from scadbuddy.bambuddy.runs import PrintRunStore, TransactionalEvents
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.components import Components, discover_components
from scadbuddy.core.config import INSTALL_CONCURRENCY, Config
from scadbuddy.core.events import (
    Event,
    EventBus,
    UpstreamAvailable,
    VersionCommitted,
    emit,
)
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import EventLogRetention, PgNotifyEventBus
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import COMMIT_ID_PATTERN, ModelHistory
from scadbuddy.library.libraries import CheckoutFetcher, CheckoutGate, LibraryStore
from scadbuddy.library.media_store import PostgresMediaStore
from scadbuddy.library.outputs import OUTPUT_ID_PATTERN, OutputMeta, OutputStore
from scadbuddy.library.presets import PresetStore
from scadbuddy.library.previews import PreviewStore
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH, MODEL_ID_PATTERN
from scadbuddy.library.url_import import IMPORT_TIMEOUT, RESOLVER_THREADS
from scadbuddy.render.previews import (
    TIMEOUT_FACTOR,
    PreviewScheduler,
)
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.render.submit import RenderService
from scadbuddy.store import BlobRefs, BlobStore
from scadbuddy.store.factory import StoreBundle
from scadbuddy.workflows.client import connect_lazily

logger = logging.getLogger(__name__)

STATE_ATTR = "scadbuddy"
VERSION_TIMEOUT = 10.0
JOB_ID_PATTERN = r"^[0-9a-f]{32}$"
RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
OPERATION_ID_PATTERN = r"^[0-9a-f]{32}$"


#: URL fetches at once per replica (#178): `POST /models/import` and, since #844,
#: `POST /models/{slug}/assets/fetch` share it. An in-process cap, because what it
#: protects -- the resolver's threads -- is per process too, so N replicas fetch up to
#: N x this. As many as the resolver has threads. Library installs share those
#: threads; a fetch that finds none free is the same retryable 503.
IMPORT_CONCURRENCY = RESOLVER_THREADS
#: `POST /models/{slug}/dependencies` reports worked out at once per replica (#253,
#: review of #740). Each reads the model's files and every model.json in a worker
#: thread; uncapped, a burst of them holds the default executor every other
#: `to_thread` route shares. A report past it waits on the loop, not in a thread.
DEPENDENCY_CHECK_CONCURRENCY = 2


class ImportPermits:
    """The import fetch budget (#178): at most `limit` fetches at once, each
    remembered by when it started, so a refusal can say when the oldest must end
    (#631). Used on the event loop only; a fetch finds it full or takes a permit with
    no await in between."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        #: When each held permit was taken, by a token of its own.
        self._taken: dict[object, float] = {}
        #: Set when a permit is given back, then replaced.
        self._freed = asyncio.Event()

    def full(self) -> bool:
        return len(self._taken) >= self.limit

    async def wait(self) -> None:
        """Until a permit is free; take it with `hold` with no await in between."""
        while self.full():
            await self._freed.wait()

    @contextmanager
    def hold(self) -> Iterator[None]:
        token = object()
        self._taken[token] = time.monotonic()
        try:
            yield
        finally:
            del self._taken[token]
            self._freed.set()
            self._freed = asyncio.Event()

    def retry_after(self) -> int:
        """Seconds until the oldest held fetch reaches `IMPORT_TIMEOUT` and must have
        given its permit back; at least 1."""
        if not self._taken:
            return 1
        left = min(self._taken.values()) + IMPORT_TIMEOUT - time.monotonic()
        return max(1, math.ceil(left))


@dataclass
class AppState:
    settings: Settings
    config: Config
    paths: DataPaths
    history: ModelHistory
    catalogue: Catalogue
    outputs: OutputStore
    #: An output's uploads to Bambuddy's file library (#455), on the projection's
    #: Postgres pool.
    uploads: BambuddyUploadStore
    #: Which Bambuddy archives an output's prints produced (#306), on the same pool.
    print_links: PrintLinkStore
    presets: PresetStore
    settings_store: SettingsStore
    fonts: FontService
    libraries: LibraryStore
    #: Uploads for `// file` parameters, with their caps (#296).
    assets: AssetStore
    #: Submits renders to Temporal and reads them back from the projection (#546).
    render: RenderService
    #: The `render_jobs` projection and the blob references.
    projection: JobProjection
    refs: BlobRefs
    #: Default-render previews: the thumbnail of a model with none and no output.
    #: None when they are off (SCADBUDDY_PREVIEW_RENDERS) or there is no database.
    previews: PreviewScheduler | None
    #: Where every state change is published (spec §7): `PgNotifyEventBus` on
    #: #241's database, or `InProcessEventBus` in a test that builds one itself.
    events: EventBus
    #: Publishes ``print.*`` from the progress reads the backend makes.
    print_progress: ProgressObserver
    #: What `FollowPrint`'s activity reads with (#1053), on the follow worker.
    print_follower: Follower
    #: The print dialog's runs (#470), on Temporal (#1052): the record, and where to
    #: start them.
    print_runs: PrintCommands
    metrics: Metrics
    #: Caps the openscad runs that do NOT go through a render — the editor's
    #: parse check and the schema derivation behind it. Its own budget, not the render
    #: one: the worker's cap is its activity slots, so there is no semaphore to share, and
    #: the pod's worst case is render_concurrency + check_concurrency + lsp_sessions.
    checks: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    #: At most INSTALL_CONCURRENCY library clones at once. Each runs in a worker
    #: thread for up to the git timeout; uncapped, a burst of installs would hold the
    #: default executor that every other `to_thread` route shares. More than one, so
    #: a long clone (NopSCADlib) doesn't hold up adding another library. Nothing
    #: orders two clones of the same library: each runs in full, and the one whose
    #: commit is already checked out gives way to it (`LibraryStore._clone`); the pin
    #: itself is written under the history's write lock. A queued install waits on
    #: the loop, not in a thread.
    installs: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(INSTALL_CONCURRENCY)
    )
    #: At most IMPORT_CONCURRENCY `POST /models/import` and `POST
    #: /models/{slug}/assets/fetch` fetches at once on this replica, together. Held for
    #: the fetch only -- an import's parse check takes `checks` like any create, an
    #: asset's sanitising runs after it -- and a fetch that finds it full is refused at
    #: once, not queued.
    imports: ImportPermits = field(default_factory=lambda: ImportPermits(IMPORT_CONCURRENCY))
    #: At most DEPENDENCY_CHECK_CONCURRENCY dependency reports at once.
    dependency_checks: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(DEPENDENCY_CHECK_CONCURRENCY)
    )
    #: Pins and renders share it; deleting a checkout takes it alone (#253). The
    #: in-process worker holds the same one.
    checkouts: CheckoutGate = field(default_factory=CheckoutGate)
    #: One permit per open editor's openscad-lsp process (``SCADBUDDY_LSP_SESSIONS``),
    #: held for as long as the editor stays open rather than for one piece of work —
    #: the third term in the pod's worst case above.
    language_servers: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    #: One permit per open realtime socket (``SCADBUDDY_REALTIME_SOCKETS``, #266).
    realtime_sockets: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    openscad_version: str | None = field(default=None)
    #: Read through :attr:`components`. An ``__init__`` field, so ``dataclasses.replace``
    #: carries the built registry over to the copy rather than dropping it.
    _components: Components | None = field(default=None, kw_only=True, repr=False)

    @property
    def components(self) -> Components:
        """Every feature service that is a component (`core/components.py`), built over
        this state by `build_state`: a new service goes there, not in a field here."""
        if self._components is None:
            raise RuntimeError("the components are not built yet: use build_state")
        return self._components

    @components.setter
    def components(self, value: Components) -> None:
        self._components = value

    #: The blob store (#426), built in the lifespan once the settings pool is open;
    #: nothing reads it earlier. `blobs` is its piece store, set with it.
    store: StoreBundle = field(init=False)
    blobs: BlobStore = field(init=False)
    #: The in-process worker's client (SCADBUDDY_TEMPORAL_WORKER_INPROCESS), which the
    #: lifespan connects eagerly: a worker cannot run on the API's lazy one.
    temporal: Client | None = field(default=None)


@dataclass(frozen=True)
class PrintCommands:
    """What the print routes need (#1052): our record, and the client and queue that
    ``PrintRun`` starts on. The client is the API's lazy one, so the API boots while
    Temporal is down; a print then answers 503 until it is back."""

    store: PrintRunStore
    client: Client
    task_queue: str
    search_attributes: bool = False


class _Immediate:
    """``publish_in`` for a bus that has no transaction of its own (the in-process bus
    of a test): publishes at once, before the caller's transaction commits, so a
    subscriber that re-reads the row may see it as it was (review #1061 5)."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    def publish_in(self, conn: Connection[Any], event: Event) -> None:
        emit(self.bus, event)


def transactional_events(events: EventBus) -> TransactionalEvents:
    return events if isinstance(events, PgNotifyEventBus) else _Immediate(events)


def announce_commits(events: EventBus, catalogue: Catalogue) -> Callable[[str, list[str]], None]:
    """The history's commit hook: ``version.committed`` for each template a commit
    touched, and ``upstream.available`` for every duplicate of one that still exists.

    Runs on the committing thread after the write lock is released, so reading the
    duplicates' ``model.json`` here cannot deadlock against the commit.
    """

    def on_commit(commit: str, touched: list[str]) -> None:
        for model_id in touched:
            emit(events, VersionCommitted(slug=model_id, commit=commit))
            try:
                if not catalogue.exists(model_id):
                    continue  # deleted: its duplicates' upstream is gone, not updated
                duplicates = catalogue.duplicates_of(model_id)
            except OSError:
                logger.exception("could not list duplicates", extra={"slug": model_id})
                continue
            for duplicate in duplicates:
                emit(events, UpstreamAvailable(slug=duplicate, upstream=model_id, commit=commit))

    return on_commit


def build_state(settings: Settings) -> AppState:
    """The core services, then every discovered component over them."""
    state = _build_core(settings)
    state.components = Components(state, discover_components())
    state.components.build_all()
    return state


def _build_core(settings: Settings) -> AppState:
    config = settings.to_config()
    paths = DataPaths(root=settings.data_dir)
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX, timeout=config.git_timeout)
    metrics = Metrics()
    metrics.build_info.labels(settings.version, settings.revision).set(1)
    # Nothing connects here: the projection's pool opens in the lifespan, and the
    # event bus's in `PgNotifyEventBus.start`. The previews share the projection's
    # pool (#454, #401).
    projection = JobProjection(settings.database_url, pool_size=settings.database_pool_size)
    pool = projection.pool
    # One LISTEN connection per process: the bus shares the projection's.
    events = PgNotifyEventBus(
        settings.database_url,
        listener=projection.pg_listener,
        metrics=metrics,
        retention=EventLogRetention(
            seconds=settings.event_log_retention_seconds,
            rows=settings.event_log_retention_rows,
        ),
    )
    # Job events commit with the job change that they describe.
    projection.events = events
    preview_store = PreviewStore(pool.connection)
    outputs = OutputStore(paths)
    uploads = BambuddyUploadStore(pool)
    checkouts = CheckoutGate()
    installs = asyncio.Semaphore(INSTALL_CONCURRENCY)
    libraries = LibraryStore(paths, max_bytes=config.library_max_bytes)
    assets = AssetStore(
        paths.assets,
        pool,
        max_total_bytes=config.asset_max_total_bytes,
        max_count=config.asset_max_count,
    )
    # The outputs feed the catalogue's fallback thumbnail (#179), and the previews
    # stand in behind them. Off, the catalogue serves no preview at all -- including
    # ones rendered while it was on, which stay stored until their model goes.
    # The media list (#274) shares the projection's pool, opened in the lifespan.
    # Nothing connects here either: the lifespan opens it.
    presets = PresetStore(
        paths, settings.database_url, pool_size=min(4, settings.database_pool_size)
    )
    catalogue = Catalogue(
        paths,
        history,
        outputs,
        preview_store,
        duplicate_staging_max_age=config.duplicate_staging_max_age,
        presets=presets,
        wrapper_prefix=WRAPPER_PREFIX,
        serve_previews=settings.preview_renders,
        media_store=PostgresMediaStore(pool),
    )
    history.on_commit = announce_commits(events, catalogue)
    # Lazy, so the API boots while Temporal is down: a render is then refused with
    # `temporal-unavailable` (#1053) until it is back.
    temporal = connect_lazily(settings.temporal_address, settings.temporal_namespace)
    render = RenderService(
        projection=projection,
        client=temporal,
        task_queue=settings.temporal_task_queue_render,
        config=config,
        paths=paths,
        metrics=metrics,
        search_attributes=settings.temporal_search_attributes,
    )
    previews = (
        build_previews(catalogue, outputs, render, config) if settings.preview_renders else None
    )
    # Nothing connects here either: the lifespan opens it first thing.
    settings_store = SettingsStore(settings, events=events)
    print_progress = ProgressObserver(events)
    print_links = PrintLinkStore(pool)

    async def read_progress(meta: OutputMeta) -> PrintProgress | None:
        # The follow links archives too (#306), so a print nobody watches is found.
        async with client_for(settings_store.load()) as client:
            return await progress_for(
                client,
                meta,
                uploads=uploads if pool is not None else None,
                # Load-bearing for the rack settle hook (#836): without ``links=`` a fast
                # print's archive is never linked, so its rack use goes uncounted, and
                # no test catches it (the P3 settle test builds its own reader).
                links=print_links if print_links.available else None,
            )

    return AppState(
        settings=settings,
        config=config,
        paths=paths,
        history=history,
        catalogue=catalogue,
        outputs=outputs,
        uploads=uploads,
        print_links=print_links,
        presets=presets,
        settings_store=settings_store,
        fonts=FontService(
            paths.root,
            api_key=config.google_fonts_api_key,
            catalogue_ttl=config.fonts_catalogue_ttl,
        ),
        libraries=libraries,
        assets=assets,
        render=render,
        previews=previews,
        metrics=metrics,
        events=events,
        print_progress=print_progress,
        print_follower=Follower(
            outputs=outputs,
            observer=print_progress,
            read=read_progress,
            events=events,
        ),
        print_runs=PrintCommands(
            store=PrintRunStore(pool, events=transactional_events(events)),
            client=temporal,
            task_queue=settings.temporal_task_queue_bambuddy,
            search_attributes=settings.temporal_search_attributes,
        ),
        checkouts=checkouts,
        installs=installs,
        checks=asyncio.Semaphore(config.check_concurrency),
        language_servers=asyncio.Semaphore(config.lsp_sessions),
        realtime_sockets=asyncio.Semaphore(config.realtime_sockets),
        projection=projection,
        refs=BlobRefs(pool),
    )


async def probe_openscad_version(config: Config) -> str | None:
    """``openscad --version`` writes to stderr, so both streams are merged."""
    if shutil.which(config.openscad) is None:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            config.openscad,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=VERSION_TIMEOUT)
    except (OSError, TimeoutError):
        logger.exception("could not read the openscad version")
        return None
    if process.returncode != 0:
        return None
    first = stdout.decode("utf-8", "replace").strip().splitlines()
    return first[0].strip() if first else None


def build_previews(
    catalogue: Catalogue,
    outputs: OutputStore,
    render: RenderService,
    config: Config,
) -> PreviewScheduler | None:
    """The preview scheduler, hooked to every change that can call for a new preview;
    ``None`` without a database, where there is nowhere to keep one. A preview is a
    render on Temporal like any other (`RenderService.render_preview`)."""
    if catalogue.previews is None:
        return None
    previews = PreviewScheduler(
        catalogue,
        catalogue.previews,
        render.render_preview,
        timeout=config.render_timeout * TIMEOUT_FACTOR,
    )
    # Everything that can change whether a model needs a preview, or which one.
    catalogue.on_change = previews.request
    outputs.on_change = previews.request
    return previews


def set_previews(state: AppState, enabled: bool) -> None:
    """Turn the default-render previews on or off before the boot starts them (#322:
    ``preview_renders`` saved in Settings applies at the next start)."""
    state.catalogue.serve_previews = enabled
    if enabled and state.previews is None:
        state.previews = build_previews(state.catalogue, state.outputs, state.render, state.config)
    elif not enabled and state.previews is not None:
        state.previews = None
        state.catalogue.on_change = None
        state.outputs.on_change = None


def get_state(connection: HTTPConnection) -> AppState:
    """For a request or a WebSocket alike: both are an ``HTTPConnection``."""
    state: AppState = getattr(connection.app.state, STATE_ATTR)
    return state


StateDep = Annotated[AppState, Depends(get_state)]


def get_config(state: StateDep) -> Config:
    return state.config


def get_paths(state: StateDep) -> DataPaths:
    return state.paths


def get_catalogue(state: StateDep) -> Catalogue:
    return state.catalogue


def get_history(state: StateDep) -> ModelHistory:
    return state.history


def get_outputs(state: StateDep) -> OutputStore:
    return state.outputs


def get_uploads(state: StateDep) -> BambuddyUploadStore:
    return state.uploads


def get_print_links(state: StateDep) -> PrintLinkStore:
    return state.print_links


def get_presets(state: StateDep) -> PresetStore:
    return state.presets


def get_settings_store(state: StateDep) -> SettingsStore:
    return state.settings_store


def get_fonts(state: StateDep) -> FontService:
    return state.fonts


def get_libraries(state: StateDep) -> LibraryStore:
    return state.libraries


def get_assets(state: StateDep) -> AssetStore:
    return state.assets


def get_render(state: StateDep) -> RenderService:
    return state.render


def get_events(state: StateDep) -> EventBus:
    return state.events


def get_print_progress(state: StateDep) -> ProgressObserver:
    return state.print_progress


#: Problem ``type`` for a route that needs the database when none is configured.
DATABASE_REQUIRED_PROBLEM = "https://scadbuddy.dev/problems/database-required"


def require_print_runs(state: StateDep) -> PrintCommands:
    """The print runs, or a 503 naming what is missing: runs live only in Postgres."""
    if not state.print_runs.store.available:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "print runs are stored in Postgres, and SCADBUDDY_DATABASE_URL is not set",
            type_=DATABASE_REQUIRED_PROBLEM,
        )
    return state.print_runs


def get_checks(state: StateDep) -> asyncio.Semaphore:
    return state.checks


def get_installs(state: StateDep) -> asyncio.Semaphore:
    return state.installs


def get_dependency_checks(state: StateDep) -> asyncio.Semaphore:
    return state.dependency_checks


def get_imports(state: StateDep) -> ImportPermits:
    return state.imports


def get_checkouts(state: StateDep) -> CheckoutGate:
    return state.checkouts


ConfigDep = Annotated[Config, Depends(get_config)]
PathsDep = Annotated[DataPaths, Depends(get_paths)]
CatalogueDep = Annotated[Catalogue, Depends(get_catalogue)]
HistoryDep = Annotated[ModelHistory, Depends(get_history)]
OutputsDep = Annotated[OutputStore, Depends(get_outputs)]
UploadsDep = Annotated[BambuddyUploadStore, Depends(get_uploads)]
PrintLinksDep = Annotated[PrintLinkStore, Depends(get_print_links)]
PresetsDep = Annotated[PresetStore, Depends(get_presets)]
SettingsStoreDep = Annotated[SettingsStore, Depends(get_settings_store)]
FontsDep = Annotated[FontService, Depends(get_fonts)]
LibrariesDep = Annotated[LibraryStore, Depends(get_libraries)]
AssetsDep = Annotated[AssetStore, Depends(get_assets)]
RenderDep = Annotated[RenderService, Depends(get_render)]
EventsDep = Annotated[EventBus, Depends(get_events)]
PrintProgressDep = Annotated[ProgressObserver, Depends(get_print_progress)]
PrintRunsDep = Annotated[PrintCommands, Depends(require_print_runs)]
ChecksDep = Annotated[asyncio.Semaphore, Depends(get_checks)]
InstallsDep = Annotated[asyncio.Semaphore, Depends(get_installs)]
DependencyChecksDep = Annotated[asyncio.Semaphore, Depends(get_dependency_checks)]
ImportsDep = Annotated[ImportPermits, Depends(get_imports)]
CheckoutsDep = Annotated[CheckoutGate, Depends(get_checkouts)]


def get_fetcher(state: StateDep, libraries: LibrariesDep) -> CheckoutFetcher:
    """Over the request's store, with the app's install permits and checkout gate."""
    return CheckoutFetcher(libraries, state.installs, state.checkouts)


FetcherDep = Annotated[CheckoutFetcher, Depends(get_fetcher)]

# A template id: a slug of mine, or `builtin:<slug>`.
SlugPath = Annotated[str, Path(pattern=MODEL_ID_PATTERN, max_length=MAX_MODEL_ID_LENGTH)]
JobIdPath = Annotated[str, Path(pattern=JOB_ID_PATTERN)]
OutputIdPath = Annotated[str, Path(pattern=OUTPUT_ID_PATTERN)]
RunIdPath = Annotated[str, Path(pattern=RUN_ID_PATTERN)]
OperationIdPath = Annotated[str, Path(pattern=OPERATION_ID_PATTERN)]
# Abbreviated ids are accepted the way git accepts them; the API always answers
# with the full 40 characters.
CommitPath = Annotated[str, Path(pattern=COMMIT_ID_PATTERN)]
