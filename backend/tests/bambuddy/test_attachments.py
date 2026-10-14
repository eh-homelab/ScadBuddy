"""Where a print's attachments go in Bambuddy's library (#309)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
import respx

from scadbuddy.bambuddy.attachments import media_folder
from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.models import ArchiveDetail
from tests.bambuddy.conftest import recording

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"


def archive(archive_id: int) -> ArchiveDetail:
    return ArchiveDetail.model_validate(
        {**recording("archive-detail.json"), "id": archive_id, "project_id": 1}
    )


@respx.mock
async def test_two_prints_of_a_project_attached_at_once_make_one_media_folder() -> None:
    """#2899 (review on #2831): each print's operation is its own subject, so two can
    look for ``Media/`` at once; both must find the one the first made."""
    folders: list[dict[str, Any]] = [{"id": 10, "name": "Keychains", "parent_id": None}]
    for folder in folders:
        folder["project_id"] = 1

    arrived = 0
    both = asyncio.Event()

    async def listed(request: httpx.Request) -> httpx.Response:
        # Wait briefly for the other lookup: unserialised, both read before either makes.
        nonlocal arrived
        seen = list(folders)
        arrived += 1
        if arrived == 2:
            both.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both.wait(), 0.2)
        return httpx.Response(200, json=seen)

    def made(request: httpx.Request) -> httpx.Response:
        folder = {"id": 20 + len(folders), "name": "Media", "parent_id": 10, "project_id": 1}
        folders.append(folder)
        return httpx.Response(200, json=folder)

    respx.get(f"{API}/library/folders/by-project/1").mock(side_effect=listed)
    create = respx.post(f"{API}/library/folders/").mock(side_effect=made)
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        first, second = await asyncio.gather(
            media_folder(client, archive(35), None), media_folder(client, archive(36), None)
        )
    assert create.call_count == 1
    assert first == second == 21


@respx.mock
async def test_of_two_media_folders_the_first_is_used() -> None:
    folders = [
        {"id": 10, "name": "Keychains", "parent_id": None, "project_id": 1},
        {"id": 31, "name": "Media", "parent_id": 10, "project_id": 1},
        {"id": 22, "name": "Media", "parent_id": 10, "project_id": 1},
    ]
    respx.get(f"{API}/library/folders/by-project/1").mock(
        return_value=httpx.Response(200, json=folders)
    )
    async with BambuddyClient(BambuddyConfig(base_url=BASE, api_key="k")) as client:
        assert await media_folder(client, archive(35), None) == 22
