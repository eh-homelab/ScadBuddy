"""The fake Bambuddy library answers what `BambuddyClient` reads: every call here goes
through the client, so its own models validate each answer."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.models import FileUpdate, FolderCreate, FolderUpdate
from scadbuddy.core.problems import ApiError
from tests.support.fake_bambuddy_library import build_app


@pytest.fixture
async def client() -> AsyncIterator[BambuddyClient]:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport) as http:
        yield BambuddyClient(BambuddyConfig(base_url="http://fake"), http=http)


async def download(client: BambuddyClient, file_id: int) -> bytes:
    return b"".join([chunk async for chunk in client.download_library_file(file_id)])


async def test_folders_nest_and_move(client: BambuddyClient) -> None:
    outer = await client.create_folder(FolderCreate(name="Dinos"))
    inner = await client.create_folder(FolderCreate(name="Rex", parent_id=outer.id))
    [top] = await client.folders()
    assert (top.name, [child.name for child in top.children]) == ("Dinos", ["Rex"])

    moved = await client.update_folder(inner.id, FolderUpdate(name="T-Rex", parent_id=0))
    assert (moved.name, moved.parent_id) == ("T-Rex", None)
    assert sorted(f.name for f in await client.folders()) == ["Dinos", "T-Rex"]

    await client.delete_folder(inner.id)
    assert [f.name for f in await client.folders()] == ["Dinos"]


async def test_files_upload_list_read_update(client: BambuddyClient) -> None:
    folder = await client.create_folder(FolderCreate(name="Dinos"))
    uploaded = await client.upload_library_file("rex.3mf", b"abc", folder_id=folder.id)
    assert (uploaded.filename, uploaded.file_type, uploaded.file_size) == ("rex.3mf", "3mf", 3)

    read = await client.library_file(uploaded.id)
    assert read.folder_id == folder.id
    assert read.file_hash is not None
    assert await download(client, uploaded.id) == b"abc"
    assert [f.id for f in await client.library_files(folder.id)] == [uploaded.id]
    assert [r.id for r in await client.library_listing(folder_id=folder.id)] == [uploaded.id]
    assert await client.library_listing(folder_id=None) == []
    assert [r.id for r in await client.library_listing(folder_id=None, every_folder=True)] == [
        uploaded.id
    ]

    updated = await client.update_library_file(
        uploaded.id, FileUpdate(filename="trex.3mf", folder_id=0)
    )
    assert (updated.filename, updated.folder_id) == ("trex.3mf", None)
    assert [r.filename for r in await client.library_listing(folder_id=None)] == ["trex.3mf"]


async def test_delete_trash_and_restore(client: BambuddyClient) -> None:
    one = await client.upload_library_file("a.3mf", b"a")
    two = await client.upload_library_file("b.3mf", b"b")

    assert await client.delete_library_file(one.id) is True
    assert await client.bulk_delete_library_files([two.id]) == 1
    assert await client.library_listing(folder_id=None) == []
    assert sorted(f.id for f in await client.trashed_library_files()) == [one.id, two.id]
    assert await client.sees_every_users_trash() is True

    with pytest.raises(ApiError) as missing:
        await client.library_file(one.id)
    assert missing.value.status == 404

    await client.restore_library_file(one.id)
    assert [r.id for r in await client.library_listing(folder_id=None)] == [one.id]


async def test_the_queue_is_empty(client: BambuddyClient) -> None:
    assert await client.queue(status="pending") == []
