from __future__ import annotations

from typing import Any, Literal, Self

from fastapi import APIRouter, status
from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, Field, model_validator

from scadbuddy.api.deps import AppState, SettingsStoreDep, StateDep, UploadsDep
from scadbuddy.api.runtime import apply_runtime, restart_required
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.errors import SCOPE_PROBLEM, Scope
from scadbuddy.bambuddy.models import Folder, Printer
from scadbuddy.bambuddy.options import BAMBUDDY_DEFAULTS, OptionScope, PrintOptions
from scadbuddy.bambuddy.send import SidebarLink, register_sidebar
from scadbuddy.bambuddy.uploads import ProjectTarget
from scadbuddy.core.config import StoreBackend
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import (
    APPLIES,
    BOOTSTRAP_FIELDS,
    Applies,
    Settings,
    env_var,
)
from scadbuddy.library.settings_store import (
    DisplayUnit,
    ModelPrintChoices,
    SettingSource,
    SettingsPatch,
    SettingsSnapshot,
    StoredSettings,
    StoreNotReadyError,
)

router = APIRouter(tags=["settings"])


class BootstrapValue(BaseModel):
    """A setting the UI shows but cannot change (#322), and why."""

    name: str
    env_var: str
    #: As the process runs with it. The database URL is shown without its credentials.
    value: str | None
    source: Literal["env", "default"]
    reason: str


class AboutView(BaseModel):
    """The build this process is, for Settings' About section."""

    version: str
    revision: str
    #: ``openscad --version`` at start, or ``None`` when it is not on ``PATH``.
    openscad_version: str | None = None


class SettingsView(BaseModel):
    """What the browser may see. The API keys themselves never appear here.

    Every env-seeded field (#322) is here with the value the store resolves it to, its
    source, and whether a change applies at once or at the next start; a secret is
    reported only as ``has_*``.
    """

    bambuddy_url: str | None = None
    has_api_key: bool = False
    #: As saved (comma-separated); the first is where Bambuddy links point (#775).
    bambuddy_web_urls: str | None = None
    has_render_api_key: bool = False
    #: True while render workers would hold the full key (spec §9): a key is stored and
    #: no render key is. The Settings page shows a persistent warning.
    render_key_fallback: bool = False
    store_backend: StoreBackend = "local"
    public_url: str | None = None
    library_folder_id: int | None = None
    printer_id: int | None = None
    default_plate: str | None = None
    display_unit: DisplayUnit = "mm"
    last_project_id: int | None = None
    #: The largest media upload (#274), in bytes, which the UI checks a file against
    #: before sending it.
    media_upload_max_bytes: int

    render_timeout: float
    render_concurrency: int
    solid_concurrency: int
    render_queue_max: int
    render_queue_depth_slo: int
    render_latency_slo: float
    check_concurrency: int
    job_ttl: float
    preview_renders: bool
    lsp_sessions: int
    realtime_sockets: int
    library_max_bytes: int
    asset_max_total_bytes: int
    asset_max_count: int
    asset_sweep_grace: float
    asset_sweep_interval: float
    duplicate_staging_max_age: float
    store_max_total_bytes: int
    store_max_count: int
    worker_cache_max_bytes: int
    has_google_fonts_api_key: bool = False
    fonts_catalogue_ttl: float
    event_log_retention_seconds: float
    event_log_retention_rows: int
    log_level: str
    #: The Temporal web UI, which Settings → Administration links to (#668).
    temporal_ui_url: str | None = None

    #: Where each env-seeded field's value comes from.
    sources: dict[str, SettingSource] = Field(default_factory=dict)
    #: When a change to each env-seeded field takes effect.
    applies: dict[str, Applies] = Field(default_factory=dict)
    #: Saved fields this process is not running with yet: they apply at the next start.
    restart_required: list[str] = Field(default_factory=list)
    #: The settings the UI cannot change, with their values.
    bootstrap: list[BootstrapValue] = Field(default_factory=list)
    about: AboutView | None = None


class PrintOptionsView(BaseModel):
    """The remembered print options, plus what Bambuddy would do without them.

    ``defaults`` is Bambuddy 1.2.5.5's own ``PrintQueueItemCreate`` defaults and is the
    baseline the UI marks a value as non-default against. It is served rather than
    duplicated in the frontend so there is one copy of it in the codebase.
    """

    defaults: PrintOptions
    global_options: PrintOptions
    #: Keyed by stringified Bambuddy printer id.
    printers: dict[str, PrintOptions] = Field(default_factory=dict)
    #: Keyed by ScadBuddy model slug.
    models: dict[str, PrintOptions] = Field(default_factory=dict)


class PrintOptionsState(PrintOptionsView):
    """The view plus the printer the per-printer scope keys on: the printer set in
    Settings, or none. Neither half needs Bambuddy."""

    printer_id: int | None = None


class PrintOptionsUpdate(BaseModel):
    """Replaces one scope's overrides wholesale.

    An all-unset ``options`` clears the scope rather than storing an empty object.
    """

    scope: OptionScope
    #: The printer id or model slug. Absent for the global scope, required otherwise.
    key: str | None = None
    options: PrintOptions

    @model_validator(mode="after")
    def _key_matches_scope(self) -> Self:
        if self.scope == "global":
            if self.key is not None:
                raise ValueError("the global scope takes no key")
        elif not self.key:
            raise ValueError(f"the {self.scope!r} scope needs a key")
        return self


ScopeStatus = Literal["ok", "missing", "unknown", "error"]


class ScopeCheck(BaseModel):
    """Whether the key carries one Bambuddy scope ScadBuddy uses (#322)."""

    scope: str
    status: ScopeStatus
    #: Required for sending and printing; the others only for projects and attachments.
    required: bool
    #: What needs it, or what went wrong.
    detail: str


class ConnectionTest(BaseModel):
    #: Reachable, and the key reads Bambuddy (the write scopes are not checked).
    ok: bool
    detail: str
    printers: list[Printer] = Field(default_factory=list)
    scopes: list[ScopeCheck] = Field(default_factory=list)


class RememberedChoices(BaseModel):
    """What the print dialog remembers (#322), for Settings to show and forget. Each is
    changed one key at a time through its own route."""

    model_print_choices: dict[str, ModelPrintChoices] = Field(default_factory=dict)
    #: Stringified Bambuddy printer id -> the plate last printed on it.
    printer_bed_types: dict[str, str] = Field(default_factory=dict)
    print_options: PrintOptions = Field(default_factory=PrintOptions)
    printer_print_options: dict[str, PrintOptions] = Field(default_factory=dict)
    model_print_options: dict[str, PrintOptions] = Field(default_factory=dict)
    #: Stringified Bambuddy project id -> the printer and nozzle it last printed on (#599).
    project_print_targets: dict[str, ProjectTarget] = Field(default_factory=dict)


class BambuddyStatus(BaseModel):
    """Bambuddy's own version and finish-photo setting, read-only (#305, #322). Each is
    ``None`` when it could not be read, with ``detail`` saying why."""

    version: str | None = None
    #: Bambuddy's ``capture_finish_photo`` (``GET /api/v1/settings/``, Read Status).
    capture_finish_photo: bool | None = None
    #: Bambuddy's settings page, where the finish photo is turned on.
    settings_url: str | None = None
    detail: str | None = None


class BambuddyTargets(BaseModel):
    """Everything the settings page needs to fill its pickers."""

    folders: list[Folder] = Field(default_factory=list)
    printers: list[Printer] = Field(default_factory=list)


def _redacted_database_url(url: str) -> str:
    """Where the database is, without who logs in or how."""
    try:
        parts = conninfo_to_dict(url)
    except Exception:
        return "set"
    host = parts.get("host") or "localhost"
    port = f":{parts['port']}" if parts.get("port") else ""
    return f"{host}{port}/{parts.get('dbname') or ''}"


def _bootstrap(settings: Settings) -> list[BootstrapValue]:
    resolved: dict[str, object] = {
        "seed_models_dir": settings.resolve_seed_models_dir(),
        "seed_libraries_dir": settings.resolve_seed_libraries_dir(),
        "frontend_dir": settings.resolve_frontend_dir(),
    }
    values: list[BootstrapValue] = []
    for name, reason in BOOTSTRAP_FIELDS.items():
        value = resolved[name] if name in resolved else getattr(settings, name)
        if name == "database_url":
            value = _redacted_database_url(settings.database_url)
        values.append(
            BootstrapValue(
                name=name,
                env_var=env_var(name),
                value=None if value is None else str(value),
                source="env" if name in settings.model_fields_set else "default",
                reason=reason,
            )
        )
    return values


def _view(snapshot: SettingsSnapshot, state: AppState) -> SettingsView:
    stored, runtime = snapshot.stored, snapshot.runtime
    fields: dict[str, Any] = {
        name: getattr(runtime, name)
        for name in SettingsView.model_fields
        if name in Settings.model_fields and name not in StoredSettings.model_fields
    }
    return SettingsView(
        bambuddy_url=stored.bambuddy_url,
        has_api_key=bool(stored.bambuddy_api_key),
        has_render_api_key=bool(stored.bambuddy_render_api_key),
        render_key_fallback=bool(stored.bambuddy_api_key) and not stored.bambuddy_render_api_key,
        store_backend=stored.store_backend,
        bambuddy_web_urls=stored.bambuddy_web_urls,
        public_url=stored.public_url,
        library_folder_id=stored.library_folder_id,
        printer_id=stored.printer_id,
        default_plate=stored.default_plate,
        display_unit=stored.display_unit,
        last_project_id=stored.last_project_id,
        has_google_fonts_api_key=bool(runtime.google_fonts_api_key),
        sources=snapshot.sources,
        applies=dict(APPLIES),
        restart_required=restart_required(state, runtime),
        bootstrap=_bootstrap(state.settings),
        about=AboutView(
            version=state.settings.version,
            revision=state.settings.revision,
            openscad_version=state.openscad_version,
        ),
        **fields,
    )


@router.get("/settings", response_model=SettingsView, summary="Every setting, with its source")
def get_settings(store: SettingsStoreDep, state: StateDep) -> SettingsView:
    return _view(store.snapshot(), state)


@router.put("/settings", response_model=SettingsView, summary="Update the settings")
def put_settings(patch: SettingsPatch, store: SettingsStoreDep, state: StateDep) -> SettingsView:
    """Saves the fields given; a ``null`` clears one and ``reset`` puts one back on the
    deployment's value. A live field applies before this answers, here and (through
    ``settings.changed``) on every other replica."""
    try:
        store.save(patch)
    except StoreNotReadyError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    snapshot = store.snapshot()
    apply_runtime(state, snapshot.runtime)
    # This process sees its own write at once; workers within the source's TTL.
    state.store.source.invalidate()
    return _view(snapshot, state)


def _remembered(
    settings: StoredSettings, project_targets: dict[int, ProjectTarget]
) -> RememberedChoices:
    return RememberedChoices(
        project_print_targets={str(pid): target for pid, target in project_targets.items()},
        model_print_choices=settings.model_print_choices,
        printer_bed_types=settings.printer_bed_types,
        print_options=settings.print_options,
        printer_print_options=settings.printer_print_options,
        model_print_options=settings.model_print_options,
    )


@router.get(
    "/settings/remembered",
    response_model=RememberedChoices,
    response_model_exclude_none=True,
    summary="What the print dialog remembers",
)
async def get_remembered(store: SettingsStoreDep, uploads: UploadsDep) -> RememberedChoices:
    """Each entry is forgotten through its own route: ``PUT /print/models/{slug}/choices``
    with an empty body, ``PUT /print/printers/{id}/bed-type`` with a ``null`` plate,
    and ``PUT /settings/print-options`` with no options, so the browser never posts a
    whole map back; a project's printer and nozzle go through
    ``DELETE /settings/remembered/projects/{project_id}``."""
    return _remembered(store.load(), await uploads.project_targets())


@router.delete(
    "/settings/remembered",
    response_model=RememberedChoices,
    response_model_exclude_none=True,
    summary="Forget every remembered choice",
)
async def delete_remembered(store: SettingsStoreDep, uploads: UploadsDep) -> RememberedChoices:
    await uploads.forget_all_project_targets()
    return _remembered(store.forget_remembered(), {})


@router.delete(
    "/settings/remembered/projects/{project_id}",
    response_model=RememberedChoices,
    response_model_exclude_none=True,
    summary="Forget one project's remembered printer and nozzle",
)
async def delete_remembered_project(
    project_id: int, store: SettingsStoreDep, uploads: UploadsDep
) -> RememberedChoices:
    await uploads.forget_project_target(project_id)
    return _remembered(store.load(), await uploads.project_targets())


def _options_view(settings: StoredSettings) -> PrintOptionsView:
    return PrintOptionsView(
        defaults=BAMBUDDY_DEFAULTS,
        global_options=settings.print_options,
        printers=settings.printer_print_options,
        models=settings.model_print_options,
    )


@router.get(
    "/settings/print-options",
    response_model=PrintOptionsState,
    summary="Remembered print options",
)
def get_print_options(store: SettingsStoreDep) -> PrintOptionsState:
    settings = store.load()
    return PrintOptionsState(**_options_view(settings).model_dump(), printer_id=settings.printer_id)


@router.put(
    "/settings/print-options",
    response_model=PrintOptionsView,
    summary="Remember print options for one scope",
)
def put_print_options(body: PrintOptionsUpdate, store: SettingsStoreDep) -> PrintOptionsView:
    """Only the named scope changes; the other two are left exactly as they were."""
    scope: OptionScope = body.scope
    return _options_view(store.save_print_options(scope, body.key, body.options))


@router.post("/settings/test", response_model=ConnectionTest, summary="Verify the API key")
async def test_settings(store: SettingsStoreDep) -> ConnectionTest:
    """``GET /api/v1/printers/`` on Bambuddy — note the trailing slash, without which
    1.2.5.5 answers 404. Nothing is written to Bambuddy: only Read Status is checked,
    and each write scope is reported ``unknown`` (#322).

    A reachable Bambuddy that refuses the key is a *result* (``ok: false``), not an
    error; only a missing URL — nothing to test at all — is still a 409.
    """
    async with client_for(store.load()) as client:
        try:
            printers = await client.printers()
        except ApiError as error:
            return ConnectionTest(ok=False, detail=error.detail, scopes=_all_failed(error))
    scopes = [
        ScopeCheck(scope=scope, status="ok", required=required, detail=what)
        if scope == Scope.READ_STATUS
        else ScopeCheck(
            scope=scope,
            status="unknown",
            required=required,
            detail=f"Not checked: Bambuddy cannot be asked what a key carries without a"
            f" write, so a missing scope shows up when it is first used. Needed for: {what}",
        )
        for scope, required, what in SCOPES
    ]
    names = ", ".join(printer.name for printer in printers) or "no printers"
    return ConnectionTest(
        ok=True, detail=f"Connected. Bambuddy reports {names}.", printers=printers, scopes=scopes
    )


#: Every scope ScadBuddy uses, whether a send needs it, and what for. Only Read Status
#: is checked, by the printer list itself. Bambuddy has no key-introspection route, and
#: the only way to tell a write scope is present is to send a write, which a connection
#: test (reachable from an agent's ``read`` tool) must never do; so the others are
#: reported ``unknown`` and a missing one surfaces as a scope problem on first use.
SCOPES: tuple[tuple[Scope, bool, str], ...] = (
    (Scope.READ_STATUS, True, "Printers, their status, and the print history."),
    (Scope.MANAGE_LIBRARY, True, "Uploading 3MFs to the library, and its folders."),
    (Scope.MANAGE_QUEUE, True, "Queueing prints."),
    (Scope.MANAGE_PROJECTS, False, "Sending to a Bambuddy project."),
    (Scope.MANAGE_ARCHIVES, False, "Attaching photos and timelapses to a print."),
)


def _all_failed(error: ApiError) -> list[ScopeCheck]:
    """The printer list failed, so no scope can be told apart from another."""
    status: ScopeStatus = "missing" if error.type == SCOPE_PROBLEM else "error"
    return [
        ScopeCheck(scope=scope, status=status, required=required, detail=error.detail)
        for scope, required, _ in SCOPES
    ]


@router.get(
    "/settings/bambuddy",
    response_model=BambuddyStatus,
    summary="Bambuddy's version and finish-photo setting",
)
async def get_bambuddy_status(store: SettingsStoreDep) -> BambuddyStatus:
    """Read-only: ScadBuddy never writes Bambuddy's settings. What cannot be read — an
    older Bambuddy, or a key without Read Status — is ``None`` rather than an error."""
    async with client_for(store.load()) as client:
        problems: list[str] = []
        version: str | None = None
        photo: bool | None = None
        try:
            version = await client.version()
        except ApiError as error:
            problems.append(error.detail)
        try:
            photo = await client.capture_finish_photo()
        except ApiError as error:
            problems.append(error.detail)
        return BambuddyStatus(
            version=version,
            capture_finish_photo=photo,
            settings_url=client.config.web_url("/settings"),
            detail=" ".join(problems) or None,
        )


@router.get(
    "/settings/targets",
    response_model=BambuddyTargets,
    summary="Folders and printers to choose from",
)
async def get_targets(store: SettingsStoreDep) -> BambuddyTargets:
    async with client_for(store.load()) as client:
        return BambuddyTargets(
            folders=await client.folders(),
            printers=await client.printers(),
        )


@router.post(
    "/settings/register-sidebar",
    response_model=SidebarLink,
    status_code=status.HTTP_200_OK,
    summary="Add ScadBuddy to Bambuddy's sidebar",
)
async def post_register_sidebar(store: SettingsStoreDep) -> SidebarLink:
    """Upsert the ``ScadBuddy`` External Link by name.

    Bambuddy renders a link with ``open_in_new_tab: false`` inside its own shell, in a
    sandboxed iframe at ``/external/{id}`` — so ScadBuddy appears as a sidebar page
    rather than a tab.
    """
    async with client_for(store.load()) as client:
        return await register_sidebar(client, store.load())
