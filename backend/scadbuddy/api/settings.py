from __future__ import annotations

import logging

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import Folder, Pipeline, PresetRef, Printer
from scadbuddy.bambuddy.send import SidebarLink, register_sidebar
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsPatch, StoredSettings

router = APIRouter(tags=["settings"])

logger = logging.getLogger(__name__)


class SettingsView(BaseModel):
    """What the browser may see. The API key itself never appears here."""

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


class ConnectionTest(BaseModel):
    ok: bool
    detail: str
    printers: list[Printer] = Field(default_factory=list)


class BambuddyTargets(BaseModel):
    """Everything the settings page needs to fill its pickers."""

    folders: list[Folder] = Field(default_factory=list)
    pipelines: list[Pipeline] = Field(default_factory=list)
    printers: list[Printer] = Field(default_factory=list)


def _view(settings: StoredSettings) -> SettingsView:
    return SettingsView(
        bambuddy_url=settings.bambuddy_url,
        has_api_key=bool(settings.bambuddy_api_key),
        public_url=settings.public_url,
        library_folder_id=settings.library_folder_id,
        pipeline_id=settings.pipeline_id,
        printer_id=settings.printer_id,
        printer_preset=settings.printer_preset,
        process_preset=settings.process_preset,
        filament_presets=settings.filament_presets,
        bed_type=settings.bed_type,
    )


@router.get("/settings", response_model=SettingsView, summary="Bambuddy connection")
def get_settings(store: SettingsStoreDep) -> SettingsView:
    return _view(store.load())


@router.put("/settings", response_model=SettingsView, summary="Update the connection")
def put_settings(patch: SettingsPatch, store: SettingsStoreDep) -> SettingsView:
    return _view(store.save(patch))


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
    """Upsert the ``Customize`` External Link by name.

    Bambuddy renders a link with ``open_in_new_tab: false`` inside its own shell, in a
    sandboxed iframe at ``/external/{id}`` — so ScadBuddy appears as a sidebar page
    rather than a tab.
    """
    async with client_for(store.load()) as client:
        return await register_sidebar(client, store.load())
