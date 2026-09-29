from __future__ import annotations

from typing import Self

from fastapi import APIRouter, status
from pydantic import BaseModel, Field, model_validator

from scadbuddy.api.deps import SettingsStoreDep, StateDep
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import Folder, Printer
from scadbuddy.bambuddy.options import BAMBUDDY_DEFAULTS, OptionScope, PrintOptions
from scadbuddy.bambuddy.send import SidebarLink, register_sidebar
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import DisplayUnit, SettingsPatch, StoredSettings

router = APIRouter(tags=["settings"])


class SettingsView(BaseModel):
    """What the browser may see. The API key itself never appears here."""

    bambuddy_url: str | None = None
    has_api_key: bool = False
    public_url: str | None = None
    library_folder_id: int | None = None
    printer_id: int | None = None
    default_plate: str | None = None
    display_unit: DisplayUnit = "mm"
    #: The largest media upload (#274), in bytes. Read-only: it is
    #: ``SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES``, reported so the UI can refuse a file
    #: before sending it.
    media_upload_max_bytes: int


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


class ConnectionTest(BaseModel):
    ok: bool
    detail: str
    printers: list[Printer] = Field(default_factory=list)


class BambuddyTargets(BaseModel):
    """Everything the settings page needs to fill its pickers."""

    folders: list[Folder] = Field(default_factory=list)
    printers: list[Printer] = Field(default_factory=list)


def _view(settings: StoredSettings, media_upload_max_bytes: int) -> SettingsView:
    return SettingsView(
        bambuddy_url=settings.bambuddy_url,
        has_api_key=bool(settings.bambuddy_api_key),
        public_url=settings.public_url,
        library_folder_id=settings.library_folder_id,
        printer_id=settings.printer_id,
        default_plate=settings.default_plate,
        display_unit=settings.display_unit,
        media_upload_max_bytes=media_upload_max_bytes,
    )


@router.get("/settings", response_model=SettingsView, summary="Bambuddy connection")
def get_settings(store: SettingsStoreDep, state: StateDep) -> SettingsView:
    return _view(store.load(), state.settings.media_upload_max_bytes)


@router.put("/settings", response_model=SettingsView, summary="Update the connection")
def put_settings(patch: SettingsPatch, store: SettingsStoreDep, state: StateDep) -> SettingsView:
    return _view(store.save(patch), state.settings.media_upload_max_bytes)


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
    1.2.5.5 answers 404.

    A reachable Bambuddy that refuses the key is a *result* (``ok: false``), not an
    error; only a missing URL — nothing to test at all — is still a 409.
    """
    async with client_for(store.load()) as client:
        try:
            printers = await client.printers()
        except ApiError as error:
            return ConnectionTest(ok=False, detail=error.detail)
    names = ", ".join(printer.name for printer in printers) or "no printers"
    return ConnectionTest(
        ok=True, detail=f"Connected. Bambuddy reports {names}.", printers=printers
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
