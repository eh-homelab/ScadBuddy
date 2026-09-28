"""The Bambuddy backend against recorded/shaped responses (spec 2026-09-27 §6.3)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.client import BambuddyConfig
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.store.bambuddy import (
    BambuddyContentBackend,
    BambuddyTarget,
    RefusedDeleteError,
    RenderSettingsSource,
    folder_name,
)
from scadbuddy.store.content import BlobMissingError, BlobScope
from scadbuddy.store.index import Pool
from tests.bambuddy.conftest import BASE_URL, recorded_schema, shaped
from tests.conftest import UNUSED_DATABASE_URL

pytestmark = pytest.mark.requires_postgres
API = f"{BASE_URL}/api/v1"
INBOX = 1
SCOPE = BlobScope(slug="dollhouse-kit", title="Dollhouse Kit")


def target(key: str = "narrow") -> Callable[[], Awaitable[BambuddyTarget]]:
    async def current() -> BambuddyTarget:
        return BambuddyTarget(config=BambuddyConfig(base_url=BASE_URL, api_key=key), inbox_id=INBOX)

    return current


def folder_row(**values: Any) -> dict[str, Any]:
    """A `FolderTreeItem`: no field the recorded schema lacks, none it requires missing."""
    missing = set(recorded_schema("FolderTreeItem")["required"]) - set(values)
    assert not missing, f"FolderTreeItem requires {sorted(missing)}"
    return shaped("FolderTreeItem", **values)


def inbox_tree() -> httpx.Response:
    return httpx.Response(
        200, json=[folder_row(id=INBOX, name="ScadBuddy", parent_id=None, children=[])]
    )


async def create_folder(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    await asyncio.sleep(0.05)  # long enough for a second worker to arrive meanwhile
    ids = {"Dollhouse Kit": 10, "Work": 11}
    return httpx.Response(200, json=folder_row(id=ids[body["name"]], **body))


def uploaded(file_id: int = 500) -> httpx.Response:
    return httpx.Response(
        200, json=shaped("FileUploadResponse", id=file_id, filename="f", file_size=1)
    )


@respx.mock
async def test_the_first_upload_makes_the_template_folder_and_work(pool: Pool) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=inbox_tree())
    created = respx.post(f"{API}/library/folders/").mock(side_effect=create_folder)
    upload = respx.post(f"{API}/library/files").mock(return_value=uploaded())
    backend = BambuddyContentBackend(target(), pool)
    assert await backend.upload("piece", b"zip", name="piece-k.zip", scope=SCOPE) == "500"
    await backend.upload("piece", b"zip2", name="piece-j.zip", scope=SCOPE)
    assert [json.loads(c.request.content) for c in created.calls] == [
        {"name": "Dollhouse Kit", "parent_id": INBOX},
        {"name": "Work", "parent_id": 10},
    ]
    request = upload.calls[0].request
    assert request.url.params["folder_id"] == "11"
    assert request.headers["X-API-Key"] == "narrow"
    assert b"application/zip" in request.content
    await backend.aclose()


@respx.mock
async def test_concurrent_first_uploads_create_one_folder_pair(pool: Pool) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=inbox_tree())
    created = respx.post(f"{API}/library/folders/").mock(side_effect=create_folder)
    respx.post(f"{API}/library/files").mock(return_value=uploaded())
    workers = [BambuddyContentBackend(target(), pool) for _ in range(2)]
    await asyncio.gather(
        *(w.upload("piece", b"z", name="piece-k.zip", scope=SCOPE) for w in workers)
    )
    assert created.call_count == 2  # one template folder, one Work, however many workers
    for w in workers:
        await w.aclose()


@respx.mock
async def test_an_existing_folder_of_that_name_is_adopted(pool: Pool) -> None:
    respx.get(f"{API}/library/folders").mock(
        return_value=httpx.Response(
            200,
            json=[
                folder_row(
                    id=INBOX,
                    name="ScadBuddy",
                    parent_id=None,
                    children=[
                        folder_row(
                            id=20,
                            name="Dollhouse Kit",
                            parent_id=INBOX,
                            children=[folder_row(id=21, name="Work", parent_id=20)],
                        )
                    ],
                )
            ],
        )
    )
    created = respx.post(f"{API}/library/folders/")
    upload = respx.post(f"{API}/library/files").mock(return_value=uploaded())
    backend = BambuddyContentBackend(target(), pool)
    await backend.upload("snapshot", b"src", name="src-1.zip", scope=SCOPE)
    assert not created.called
    assert upload.calls[0].request.url.params["folder_id"] == "21"
    await backend.aclose()


def test_folder_names_carry_no_dots_or_separators() -> None:
    assert folder_name(".hidden/Kit\\2") == "hidden Kit 2"
    assert folder_name("...") == "Template"


@respx.mock
async def test_delete_outside_a_work_folder_is_refused_without_a_request(pool: Pool) -> None:
    respx.get(f"{API}/library/files/77").mock(
        return_value=httpx.Response(
            200, json=shaped("FileResponse", id=77, filename="house.3mf", folder_id=99)
        )
    )
    delete = respx.delete(f"{API}/library/files/77")
    backend = BambuddyContentBackend(target(), pool)
    with pytest.raises(RefusedDeleteError):
        await backend.remove("77")
    assert not delete.called
    await backend.aclose()


@respx.mock
async def test_delete_in_work_is_sent_and_an_already_gone_file_is_fine(pool: Pool) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=inbox_tree())
    respx.post(f"{API}/library/folders/").mock(side_effect=create_folder)
    respx.post(f"{API}/library/files").mock(return_value=uploaded(500))
    respx.get(f"{API}/library/files/500").mock(
        return_value=httpx.Response(
            200, json=shaped("FileResponse", id=500, filename="p", folder_id=11)
        )
    )
    delete = respx.delete(f"{API}/library/files/500").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(f"{API}/library/files/501").mock(
        return_value=httpx.Response(404, json={"detail": "gone"})
    )
    backend = BambuddyContentBackend(target(), pool)
    await backend.upload("piece", b"z", name="piece-k.zip", scope=SCOPE)
    await backend.remove("500")
    await backend.remove("501")
    assert delete.call_count == 1
    await backend.aclose()


@respx.mock
async def test_a_missing_download_is_blob_missing(pool: Pool) -> None:
    respx.get(f"{API}/library/files/9/download").mock(
        return_value=httpx.Response(404, json={"detail": "x"})
    )
    backend = BambuddyContentBackend(target(), pool)
    with pytest.raises(BlobMissingError):
        async for _ in backend.download("9"):
            pass
    await backend.aclose()


@respx.mock
async def test_a_work_folder_deleted_in_bambuddy_is_made_again(pool: Pool) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=inbox_tree())
    created = respx.post(f"{API}/library/folders/").mock(side_effect=create_folder)
    upload = respx.post(f"{API}/library/files").mock(
        side_effect=[
            uploaded(1),
            httpx.Response(404, json={"detail": "Folder not found"}),
            uploaded(2),
        ]
    )
    backend = BambuddyContentBackend(target(), pool)
    await backend.upload("piece", b"a", name="a.zip", scope=SCOPE)
    assert await backend.upload("piece", b"b", name="b.zip", scope=SCOPE) == "2"
    assert created.call_count == 4 and upload.call_count == 3
    await backend.aclose()


@respx.mock
async def test_a_rotated_render_key_is_used_without_a_restart(pool: Pool, tmp_path: Path) -> None:
    def put(name: str, value: object) -> None:
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO settings (name, value) VALUES (%s, %s)"
                " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
                (name, Jsonb(value)),
            )

    put("bambuddy_url", BASE_URL)
    put("library_folder_id", INBOX)
    put("bambuddy_render_api_key", "old")
    source = RenderSettingsSource(
        pool, Settings(data_dir=tmp_path, database_url=UNUSED_DATABASE_URL), ttl=0
    )
    route = respx.get(f"{API}/library/files/5").mock(
        return_value=httpx.Response(200, json=shaped("FileResponse", id=5, filename="a.zip"))
    )
    backend = BambuddyContentBackend(source.target, pool)
    await backend.exists("5")
    put("bambuddy_render_api_key", "new")
    await backend.exists("5")
    assert [c.request.headers["X-API-Key"] for c in route.calls] == ["old", "new"]
    put("bambuddy_render_api_key", None)
    put("bambuddy_api_key", "full")
    assert (await source.current()).key_is_fallback is True
    await backend.aclose()


@pytest.fixture
def small_pool(pool: Pool, pg_conninfo: str) -> Iterator[Pool]:
    """Two connections, over the schema `pool` migrated."""
    opened: Pool = ConnectionPool(
        pg_conninfo,
        min_size=1,
        max_size=2,
        open=True,
        connection_class=Connection[DictRow],
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    try:
        yield opened
    finally:
        opened.close()


@respx.mock
async def test_first_uploads_to_more_templates_than_connections_do_not_starve_the_pool(
    small_pool: Pool,
) -> None:
    ids = iter(range(100, 200))

    async def create(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)  # every finder holds its lock across this
        return httpx.Response(200, json=folder_row(id=next(ids), **json.loads(request.content)))

    respx.get(f"{API}/library/folders").mock(return_value=inbox_tree())
    created = respx.post(f"{API}/library/folders/").mock(side_effect=create)
    respx.post(f"{API}/library/files").mock(return_value=uploaded())
    workers = [BambuddyContentBackend(target(), small_pool) for _ in range(3)]
    scopes = [BlobScope(slug=f"kit-{n}", title=f"Kit {n}") for n in range(3)]
    uploads = [
        w.upload("piece", b"z", name="p.zip", scope=scope) for w in workers for scope in scopes
    ]
    await asyncio.wait_for(asyncio.gather(*uploads), 10)
    assert created.call_count == 6  # a template folder and a Work per template
    for w in workers:
        await w.aclose()


@respx.mock
async def test_a_work_folder_that_keeps_vanishing_is_made_again_once_not_looped(
    pool: Pool,
) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=inbox_tree())
    created = respx.post(f"{API}/library/folders/").mock(side_effect=create_folder)
    gone = httpx.Response(404, json={"detail": "Folder not found"})
    upload = respx.post(f"{API}/library/files").mock(side_effect=[uploaded(1), gone, gone])
    backend = BambuddyContentBackend(target(), pool)
    await backend.upload("piece", b"a", name="a.zip", scope=SCOPE)
    with pytest.raises(ApiError):
        await backend.upload("piece", b"b", name="b.zip", scope=SCOPE)
    assert upload.call_count == 3 and created.call_count == 4
    await backend.aclose()


async def test_render_settings_are_cached_for_the_ttl_and_invalidate_rereads(
    pool: Pool, tmp_path: Path
) -> None:
    def key(value: str) -> None:
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO settings (name, value) VALUES ('bambuddy_render_api_key', %s)"
                " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
                (Jsonb(value),),
            )

    now = [100.0]
    source = RenderSettingsSource(
        pool,
        Settings(data_dir=tmp_path, database_url=UNUSED_DATABASE_URL),
        ttl=30,
        clock=lambda: now[0],
    )
    key("a")
    assert (await source.current()).api_key == "a"
    key("b")
    now[0] += 29
    assert (await source.current()).api_key == "a"  # within the TTL: not re-read
    now[0] += 1
    assert (await source.current()).api_key == "b"  # at the TTL: re-read
    key("c")
    source.invalidate()
    assert (await source.current()).api_key == "c"
