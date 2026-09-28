from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Query, status
from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, Field, model_validator

from scadbuddy.api.deps import AppState, SettingsStoreDep, SlugPath, StateDep
from scadbuddy.api.runtime import apply_runtime, restart_required
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.errors import SCOPE_PROBLEM, Scope
from scadbuddy.bambuddy.models import Folder, Pipeline, PresetRef, Printer
from scadbuddy.bambuddy.options import BAMBUDDY_DEFAULTS, OptionScope, PrintOptions
from scadbuddy.bambuddy.send import SidebarLink, register_sidebar
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
)

router = APIRouter(tags=["settings"])

logger = logging.getLogger(__name__)


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
    public_url: str | None = None
    library_folder_id: int | None = None
    pipeline_id: int | None = None
    printer_id: int | None = None
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    filament_presets: list[PresetRef] = Field(default_factory=list)
    bed_type: str | None = None
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
    render_queue_timeout: float
    render_poll_interval: float
    render_fallback_poll_interval: float
    render_lease_timeout: float
    render_max_attempts: int
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
    has_google_fonts_api_key: bool = False
    fonts_catalogue_ttl: float
    event_log_retention_seconds: float
    event_log_retention_rows: int
    log_level: str

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
    """The view plus the printer the per-printer scope keys on.

    Only the GET carries it, and only the GET may touch Bambuddy: with a slicer pipeline
    configured the target printer lives on the pipeline, so reading it costs one
    ``GET /slicer-pipelines/{id}``. The PUT deliberately does not resolve it — remembering
    an option must not need a reachable Bambuddy — and an override saved against the wrong
    id would silently never apply, which is why this is served rather than guessed.
    """

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
    #: Required for sending and printing; the others only for projects and attachments,
    #: so their absence is a warning rather than a failed test.
    required: bool
    #: What needs it, or what went wrong.
    detail: str


class ConnectionTest(BaseModel):
    #: Reachable, and every required scope is there.
    ok: bool
    detail: str
    printers: list[Printer] = Field(default_factory=list)
    scopes: list[ScopeCheck] = Field(default_factory=list)


class RememberedChoices(BaseModel):
    """What the print dialog and send bar remember (#322), for Settings to show and
    forget. Each is changed one key at a time through its own route."""

    #: Model slug -> the pipeline it once printed with (#86); no longer read.
    model_pipelines: dict[str, int] = Field(default_factory=dict)
    model_print_choices: dict[str, ModelPrintChoices] = Field(default_factory=dict)
    #: Stringified Bambuddy printer id -> the plate last printed on it.
    printer_bed_types: dict[str, str] = Field(default_factory=dict)
    print_options: PrintOptions = Field(default_factory=PrintOptions)
    printer_print_options: dict[str, PrintOptions] = Field(default_factory=dict)
    model_print_options: dict[str, PrintOptions] = Field(default_factory=dict)


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
    pipelines: list[Pipeline] = Field(default_factory=list)
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
        public_url=stored.public_url,
        library_folder_id=stored.library_folder_id,
        pipeline_id=stored.pipeline_id,
        printer_id=stored.printer_id,
        printer_preset=stored.printer_preset,
        process_preset=stored.process_preset,
        filament_presets=stored.filament_presets,
        bed_type=stored.bed_type,
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
    store.save(patch)
    snapshot = store.snapshot()
    apply_runtime(state, snapshot.runtime)
    return _view(snapshot, state)


def _remembered(settings: StoredSettings) -> RememberedChoices:
    return RememberedChoices(
        model_pipelines=settings.model_pipelines,
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
def get_remembered(store: SettingsStoreDep) -> RememberedChoices:
    """Each entry is forgotten through its own route: ``PUT /print/models/{slug}/choices``
    with an empty body, ``PUT /print/printers/{id}/bed-type`` with a ``null`` plate,
    ``PUT /settings/print-options`` with no options, and the ``DELETE`` below for a
    model's pipeline, so the browser never posts a whole map back."""
    return _remembered(store.load())


@router.delete(
    "/settings/remembered",
    response_model=RememberedChoices,
    response_model_exclude_none=True,
    summary="Forget every remembered choice",
)
def delete_remembered(store: SettingsStoreDep) -> RememberedChoices:
    return _remembered(store.forget_remembered())


@router.delete(
    "/settings/remembered/model-pipelines/{slug}",
    response_model=RememberedChoices,
    response_model_exclude_none=True,
    summary="Forget the pipeline one model printed with",
)
def delete_model_pipeline(slug: SlugPath, store: SettingsStoreDep) -> RememberedChoices:
    return _remembered(store.set_model_pipeline(slug, None))


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
async def get_print_options(
    store: SettingsStoreDep,
    pipeline_id: Annotated[
        int | None,
        Query(description="The pipeline about to run, when the caller has already chosen one"),
    ] = None,
) -> PrintOptionsState:
    settings = store.load()
    printer_id = settings.printer_id
    # The Settings pipeline, for every model (a legacy per-model one is no longer read).
    # A caller that has already chosen one passes it (#145), and the run keys the scope
    # on that pipeline's target.
    if pipeline_id is None:
        pipeline_id = settings.pipeline_id
    if printer_id is None and pipeline_id is not None:
        try:
            async with client_for(settings) as client:
                printer_id = (await client.pipeline(pipeline_id)).target_printer_id
        except ApiError as error:
            # Everything else here is read from the stored settings and needs no network, so
            # a Bambuddy hiccup — or a pipeline deleted on its side, which ScadBuddy cannot
            # notice, since it stores only the id — must not take the whole panel down. The
            # fallback is the state the UI already has a shape for: no printer known, so the
            # per-printer scope is disabled and the global and per-model rows still show.
            logger.info(
                "could not resolve the pipeline's target printer; the per-printer scope "
                "will be unavailable",
                extra={"pipeline_id": pipeline_id, "detail": error.detail},
            )
    return PrintOptionsState(**_options_view(settings).model_dump(), printer_id=printer_id)


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
    1.2.5.5 answers 404 — then one probe per write scope (#322).

    A reachable Bambuddy that refuses the key is a *result* (``ok: false``), not an
    error; only a missing URL — nothing to test at all — is still a 409.
    """
    async with client_for(store.load()) as client:
        try:
            printers = await client.printers()
        except ApiError as error:
            return ConnectionTest(ok=False, detail=error.detail, scopes=_all_failed(error))
        probed = await asyncio.gather(*(_probe(client, scope) for scope, _, _ in SCOPE_PROBES[1:]))
    read = ScopeCheck(
        scope=Scope.READ_STATUS, status="ok", required=True, detail=SCOPE_PROBES[0][2]
    )
    scopes = [read, *probed]
    missing = [check.scope for check in scopes if check.required and check.status != "ok"]
    names = ", ".join(printer.name for printer in printers) or "no printers"
    if missing:
        detail = f"Connected, but the key lacks {', '.join(missing)}."
    else:
        detail = f"Connected. Bambuddy reports {names}."
    return ConnectionTest(ok=not missing, detail=detail, printers=printers, scopes=scopes)


#: Every scope ScadBuddy uses, whether a send needs it, and what for. Read Status is
#: the printer list itself; each other scope is probed by the client
#: (:meth:`BambuddyClient.scope_granted`) with a write naming a record that cannot
#: exist, which Bambuddy refuses on the scope before it looks the record up.
SCOPE_PROBES: tuple[tuple[Scope, bool, str], ...] = (
    (Scope.READ_STATUS, True, "Printers, their status, and the print history."),
    (Scope.MANAGE_LIBRARY, True, "Uploading 3MFs to the library, and its folders."),
    (Scope.MANAGE_QUEUE, True, "Queueing prints and running slicer pipelines."),
    (Scope.MANAGE_PROJECTS, False, "Sending to a Bambuddy project."),
    (Scope.MANAGE_ARCHIVES, False, "Attaching photos and timelapses to a print."),
)


def _all_failed(error: ApiError) -> list[ScopeCheck]:
    """The printer list failed, so no scope can be told apart from another."""
    status: ScopeStatus = "missing" if error.type == SCOPE_PROBLEM else "error"
    return [
        ScopeCheck(scope=scope, status=status, required=required, detail=error.detail)
        for scope, required, _ in SCOPE_PROBES
    ]


async def _probe(client: BambuddyClient, scope: Scope) -> ScopeCheck:
    required, what = next((r, w) for s, r, w in SCOPE_PROBES if s == scope)
    try:
        answer = await client.scope_granted(scope)
    except ApiError as error:
        return ScopeCheck(scope=scope, status="error", required=required, detail=error.detail)
    if answer is None:
        return ScopeCheck(
            scope=scope,
            status="unknown",
            required=required,
            detail=f"Bambuddy's answer did not say. Needed for: {what}",
        )
    if answer:
        return ScopeCheck(scope=scope, status="ok", required=required, detail=what)
    return ScopeCheck(
        scope=scope,
        status="missing",
        required=required,
        detail=f"The key does not have {scope}. Needed for: {what}",
    )


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
    summary="Folders, pipelines and printers to choose from",
)
async def get_targets(store: SettingsStoreDep) -> BambuddyTargets:
    async with client_for(store.load()) as client:
        return BambuddyTargets(
            folders=await client.folders(),
            pipelines=await client.pipelines(),
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
