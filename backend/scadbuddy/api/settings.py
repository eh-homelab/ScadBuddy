from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsPatch, StoredSettings

router = APIRouter(tags=["settings"])

logger = logging.getLogger(__name__)

PRINTERS_PATH = "/api/v1/printers"
TEST_TIMEOUT = 10.0

# Reserved for the Bambuddy epic: POST /settings/register-sidebar


class SettingsView(BaseModel):
    bambuddy_url: str | None = None
    has_api_key: bool = False
    library_folder_id: str | None = None
    pipeline_id: str | None = None
    printer_id: str | None = None
    public_url: str | None = None


class Printer(BaseModel):
    id: str | None = None
    name: str | None = None
    model: str | None = None


class ConnectionTest(BaseModel):
    ok: bool
    printers: list[Printer] = Field(default_factory=list)
    error: str | None = None


def _view(settings: StoredSettings) -> SettingsView:
    return SettingsView(
        bambuddy_url=settings.bambuddy_url,
        has_api_key=bool(settings.bambuddy_api_key),
        library_folder_id=settings.library_folder_id,
        pipeline_id=settings.pipeline_id,
        printer_id=settings.printer_id,
        public_url=settings.public_url,
    )


def _as_text(value: Any) -> str | None:
    return None if value is None else str(value)


def parse_printers(payload: Any) -> list[Printer]:
    """Bambuddy answers with a bare list or a wrapped one; accept either."""
    rows = payload
    if isinstance(payload, dict):
        for key in ("items", "printers", "results", "data"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:
            rows = []
    if not isinstance(rows, list):
        return []
    return [
        Printer(
            id=_as_text(row.get("id")),
            name=_as_text(row.get("name")),
            model=_as_text(row.get("model")),
        )
        for row in rows
        if isinstance(row, dict)
    ]


@router.get("/settings", response_model=SettingsView, summary="Bambuddy connection")
def get_settings(store: SettingsStoreDep) -> SettingsView:
    return _view(store.load())


@router.put("/settings", response_model=SettingsView, summary="Update the connection")
def put_settings(patch: SettingsPatch, store: SettingsStoreDep) -> SettingsView:
    return _view(store.save(patch))


@router.post("/settings/test", response_model=ConnectionTest, summary="Verify the API key")
async def test_settings(store: SettingsStoreDep) -> ConnectionTest:
    settings = store.load()
    if not settings.bambuddy_url:
        raise ApiError(status.HTTP_409_CONFLICT, "no Bambuddy URL is configured")
    url = settings.bambuddy_url.rstrip("/") + PRINTERS_PATH
    headers = {"X-API-Key": settings.bambuddy_api_key or ""}
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
        response.raise_for_status()
        printers = parse_printers(response.json())
    except httpx.HTTPStatusError as error:
        return ConnectionTest(ok=False, error=f"Bambuddy answered {error.response.status_code}")
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("the Bambuddy connection test failed", extra={"url": url})
        return ConnectionTest(ok=False, error=f"{type(error).__name__}: {error}")
    return ConnectionTest(ok=True, printers=printers)
