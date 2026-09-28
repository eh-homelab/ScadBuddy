from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Path, status
from starlette.requests import HTTPConnection

from scadbuddy.analyzers.decisions import DecisionStore, PostgresDecisionStore
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver, progress_for
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.bambuddy.watcher import PgPrintLog, PgWatchLock, PrintWatcher
from scadbuddy.core.components import Components, discover_components
from scadbuddy.core.config import Config
from scadbuddy.core.events import (
    EventBus,
    InProcessEventBus,
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
from scadbuddy.render.job_store import JobBackend, JobStore
from scadbuddy.render.jobs import RenderQueue
from scadbuddy.render.pg_store import PostgresJobStore
from scadbuddy.render.previews import TIMEOUT_FACTOR, PreviewScheduler, render_preview
from scadbuddy.render.solids import WRAPPER_PREFIX

logger = logging.getLogger(__name__)

STATE_ATTR = "scadbuddy"
VERSION_TIMEOUT = 10.0
JOB_ID_PATTERN = r"^[0-9a-f]{32}$"


INSTALL_CONCURRENCY = 2


@dataclass
class AppState:
    settings: Settings
    config: Config
    paths: DataPaths
    history: ModelHistory
    catalogue: Catalogue
    outputs: OutputStore
    #: An output's uploads to Bambuddy's file library (#455), on the render queue's
    #: Postgres pool. Without a database every use raises (#401).
    uploads: BambuddyUploadStore
    presets: PresetStore
    settings_store: SettingsStore
    fonts: FontService
    libraries: LibraryStore
    #: Uploads for `// file` parameters, with their caps (#296).
    assets: AssetStore
    queue: RenderQueue
    #: Default-render previews: the thumbnail of a model with none and no output.
    #: None when they are off (SCADBUDDY_PREVIEW_RENDERS) or there is no database.
    previews: PreviewScheduler | None
    #: Where every state change is published (spec §7): `PgNotifyEventBus` on
    #: #241's database when one is configured, `InProcessEventBus` otherwise.
    events: EventBus
    #: Publishes ``print.*`` from the progress reads the backend makes.
    print_progress: ProgressObserver
    #: Follows each started print until it settles (#268).
    print_watcher: PrintWatcher
    metrics: Metrics
    #: Print-analyzer decisions (#284), in Postgres only. ``None`` without a database
    #: (until #401 makes one required): the routes that persist answer 503.
    decisions: DecisionStore | None
    #: Caps the openscad runs that do NOT go through the render queue — the editor's
    #: parse check and the schema derivation behind it. Its own budget, not the render
    #: one: the queue's cap is N worker tasks, so there is no semaphore to share, and
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
    #: Pins and renders share it; deleting a checkout takes it alone (#253). The
    #: render queue holds the same one.
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
    # Nothing connects here: the job pool opens in `RenderQueue.open_store` and the
    # event bus's in `PgNotifyEventBus.start`, both from the lifespan. The previews
    # share the job pool, and there are none without a database (#454, #401).
    store: JobBackend
    events: EventBus
    preview_store: PreviewStore | None = None
    if settings.database_url:
        pg_store = PostgresJobStore(
            settings.database_url, paths, pool_size=settings.database_pool_size
        )
        # One LISTEN connection per process: the bus shares the render queue's.
        pg_events = PgNotifyEventBus(
            settings.database_url,
            listener=pg_store.pg_listener,
            metrics=metrics,
            retention=EventLogRetention(
                seconds=settings.event_log_retention_seconds,
                rows=settings.event_log_retention_rows,
            ),
        )
        # Job events commit with the job change that they describe.
        pg_store.events = pg_events
        store, events = pg_store, pg_events
        preview_store = PreviewStore(pg_store.connection)
    else:
        # No database: the UI keeps working, events reach this process only.
        store, events = JobStore(paths), InProcessEventBus()
    decisions: DecisionStore | None = (
        PostgresDecisionStore(settings.database_url) if settings.database_url else None
    )
    outputs = OutputStore(paths)
    uploads = BambuddyUploadStore(store.pool if isinstance(store, PostgresJobStore) else None)
    checkouts = CheckoutGate()
    installs = asyncio.Semaphore(INSTALL_CONCURRENCY)
    libraries = LibraryStore(paths, max_bytes=config.library_max_bytes)
    # The render queue's: a route builds its own over its `LibrariesDep`.
    fetcher = CheckoutFetcher(libraries, installs, checkouts)
    assets = AssetStore(
        paths.assets,
        max_total_bytes=config.asset_max_total_bytes,
        max_count=config.asset_max_count,
    )
    # The outputs feed the catalogue's fallback thumbnail (#179), and the previews
    # stand in behind them. Off, the catalogue serves no preview at all -- including
    # ones rendered while it was on, which stay stored until their model goes.
    # The media list (#274) shares the render queue's pool, opened in the lifespan.
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
        media_store=(
            PostgresMediaStore(store.pool) if isinstance(store, PostgresJobStore) else None
        ),
    )
    history.on_commit = announce_commits(events, catalogue)
    queue = RenderQueue(
        config,
        paths,
        store=store,
        history=history,
        metrics=metrics,
        events=events,
        checkouts=checkouts,
        fetcher=fetcher,
        assets=assets,
    )
    previews: PreviewScheduler | None = None
    if settings.preview_renders and preview_store is not None:
        previews = PreviewScheduler(
            catalogue,
            preview_store,
            queue,
            lambda slug: render_preview(
                slug,
                config=config,
                paths=paths,
                history=history,
                assets=assets,
                executor=queue.thumbnail_executor,
                checkouts=checkouts,
            ),
            timeout=config.render_timeout * TIMEOUT_FACTOR,
        )
        # Everything that can change whether a model needs a preview, or which one.
        catalogue.on_change = previews.request
        outputs.on_change = previews.request
    # Nothing connects here either: the lifespan opens it first thing.
    settings_store = SettingsStore(settings, events=events)
    print_progress = ProgressObserver(events)

    async def read_progress(meta: OutputMeta) -> PrintProgress | None:
        async with client_for(settings_store.load()) as client:
            return await progress_for(client, meta)

    return AppState(
        settings=settings,
        config=config,
        paths=paths,
        history=history,
        catalogue=catalogue,
        outputs=outputs,
        uploads=uploads,
        presets=presets,
        settings_store=settings_store,
        fonts=FontService(
            paths.root,
            api_key=config.google_fonts_api_key,
            catalogue_ttl=config.fonts_catalogue_ttl,
        ),
        libraries=libraries,
        assets=assets,
        queue=queue,
        previews=previews,
        metrics=metrics,
        decisions=decisions,
        events=events,
        print_progress=print_progress,
        print_watcher=PrintWatcher(
            outputs=outputs,
            observer=print_progress,
            read=read_progress,
            events=events,
            prints=PgPrintLog(settings.database_url) if settings.database_url else None,
            lock=PgWatchLock(settings.database_url) if settings.database_url else None,
        ),
        checkouts=checkouts,
        installs=installs,
        checks=asyncio.Semaphore(config.check_concurrency),
        language_servers=asyncio.Semaphore(config.lsp_sessions),
        realtime_sockets=asyncio.Semaphore(config.realtime_sockets),
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


def get_queue(state: StateDep) -> RenderQueue:
    return state.queue


def get_events(state: StateDep) -> EventBus:
    return state.events


def get_print_progress(state: StateDep) -> ProgressObserver:
    return state.print_progress


def get_print_watcher(state: StateDep) -> PrintWatcher:
    return state.print_watcher


#: Problem ``type`` for a route that needs the database when none is configured.
DATABASE_REQUIRED_PROBLEM = "https://scadbuddy.dev/problems/database-required"


def get_decisions(state: StateDep) -> DecisionStore | None:
    return state.decisions


def require_decisions(state: StateDep) -> DecisionStore:
    """The decision store, or a 503 naming what is missing. There is no file fallback."""
    if state.decisions is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "analyzer decisions are stored in Postgres, and SCADBUDDY_DATABASE_URL is not set",
            type_=DATABASE_REQUIRED_PROBLEM,
        )
    return state.decisions


def get_checks(state: StateDep) -> asyncio.Semaphore:
    return state.checks


def get_installs(state: StateDep) -> asyncio.Semaphore:
    return state.installs


def get_checkouts(state: StateDep) -> CheckoutGate:
    return state.checkouts


ConfigDep = Annotated[Config, Depends(get_config)]
PathsDep = Annotated[DataPaths, Depends(get_paths)]
CatalogueDep = Annotated[Catalogue, Depends(get_catalogue)]
HistoryDep = Annotated[ModelHistory, Depends(get_history)]
OutputsDep = Annotated[OutputStore, Depends(get_outputs)]
UploadsDep = Annotated[BambuddyUploadStore, Depends(get_uploads)]
PresetsDep = Annotated[PresetStore, Depends(get_presets)]
SettingsStoreDep = Annotated[SettingsStore, Depends(get_settings_store)]
FontsDep = Annotated[FontService, Depends(get_fonts)]
LibrariesDep = Annotated[LibraryStore, Depends(get_libraries)]
AssetsDep = Annotated[AssetStore, Depends(get_assets)]
QueueDep = Annotated[RenderQueue, Depends(get_queue)]
EventsDep = Annotated[EventBus, Depends(get_events)]
PrintProgressDep = Annotated[ProgressObserver, Depends(get_print_progress)]
PrintWatcherDep = Annotated[PrintWatcher, Depends(get_print_watcher)]
OptionalDecisionsDep = Annotated[DecisionStore | None, Depends(get_decisions)]
DecisionsDep = Annotated[DecisionStore, Depends(require_decisions)]
ChecksDep = Annotated[asyncio.Semaphore, Depends(get_checks)]
InstallsDep = Annotated[asyncio.Semaphore, Depends(get_installs)]
CheckoutsDep = Annotated[CheckoutGate, Depends(get_checkouts)]


def get_fetcher(state: StateDep, libraries: LibrariesDep) -> CheckoutFetcher:
    """Over the request's store, with the app's install permits and checkout gate."""
    return CheckoutFetcher(libraries, state.installs, state.checkouts)


FetcherDep = Annotated[CheckoutFetcher, Depends(get_fetcher)]

# A template id: a slug of mine, or `builtin:<slug>`.
SlugPath = Annotated[str, Path(pattern=MODEL_ID_PATTERN, max_length=MAX_MODEL_ID_LENGTH)]
JobIdPath = Annotated[str, Path(pattern=JOB_ID_PATTERN)]
OutputIdPath = Annotated[str, Path(pattern=OUTPUT_ID_PATTERN)]
# Abbreviated ids are accepted the way git accepts them; the API always answers
# with the full 40 characters.
CommitPath = Annotated[str, Path(pattern=COMMIT_ID_PATTERN)]
