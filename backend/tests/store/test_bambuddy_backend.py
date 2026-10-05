"""The Bambuddy backend against recorded/shaped responses (spec 2026-09-27 §6.3)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import psycopg
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
    folder_lock_key,
    folder_name,
)
from scadbuddy.store.content import BlobMissingError, BlobScope
from scadbuddy.store.index import Pool
from tests.bambuddy.conftest import BASE_URL, recorded_schema, shaped
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS

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
async def test_a_delete_in_a_work_folder_of_another_inbox_is_refused(pool: Pool) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO store_folders (instance, inbox_id, slug, role, folder_id)"
            " VALUES (%s, %s, 'old', 'work', 42)",
            (BASE_URL, INBOX + 1),
        )
    respx.get(f"{API}/library/files/78").mock(
        return_value=httpx.Response(
            200, json=shaped("FileResponse", id=78, filename="p", folder_id=42)
        )
    )
    delete = respx.delete(f"{API}/library/files/78")
    backend = BambuddyContentBackend(target(), pool)
    with pytest.raises(RefusedDeleteError):
        await backend.remove("78")
    assert not delete.called
    await backend.aclose()


OTHER = "https://other-bambuddy.test"


def _record_folders(pool: Pool, instance: str, *rows: tuple[str, str, int]) -> None:
    with pool.connection() as conn:
        for slug, role, folder_id in rows:
            conn.execute(
                "INSERT INTO store_folders (instance, inbox_id, slug, role, folder_id)"
                " VALUES (%s, %s, %s, %s, %s)",
                (instance, INBOX, slug, role, folder_id),
            )


@respx.mock
async def test_a_delete_in_a_work_folder_of_another_bambuddy_is_refused(pool: Pool) -> None:
    """#683: the same inbox id on another Bambuddy is another folder. A Work folder
    recorded there must not make this instance's folder of that id deletable."""
    _record_folders(pool, OTHER, ("old", "work", 42))
    respx.get(f"{API}/library/files/78").mock(
        return_value=httpx.Response(
            200, json=shaped("FileResponse", id=78, filename="p", folder_id=42)
        )
    )
    delete = respx.delete(f"{API}/library/files/78")
    backend = BambuddyContentBackend(target(), pool)
    with pytest.raises(RefusedDeleteError):
        await backend.remove("78")
    assert not delete.called
    await backend.aclose()


@respx.mock
async def test_folders_recorded_on_another_bambuddy_are_not_uploaded_into(pool: Pool) -> None:
    """#683: a repointed `bambuddy_url` finds or makes its own folders rather than
    writing into whatever has the other instance's recorded ids."""
    _record_folders(pool, OTHER, (SCOPE.slug or "", "template", 70), (SCOPE.slug or "", "work", 71))
    respx.get(f"{API}/library/folders").mock(return_value=inbox_tree())
    created = respx.post(f"{API}/library/folders/").mock(side_effect=create_folder)
    upload = respx.post(f"{API}/library/files").mock(return_value=uploaded())
    backend = BambuddyContentBackend(target(), pool)
    await backend.upload("piece", b"zip", name="piece-k.zip", scope=SCOPE)
    assert created.call_count == 2
    assert upload.calls[0].request.url.params["folder_id"] == "11"
    await backend.aclose()


def _placed_tree() -> httpx.Response:
    """This instance's tree: template folder 40 under the inbox, its Work folder 42."""
    return httpx.Response(
        200,
        json=[
            folder_row(
                id=INBOX,
                name="ScadBuddy",
                parent_id=None,
                children=[
                    folder_row(
                        id=40,
                        name="Old",
                        parent_id=INBOX,
                        children=[folder_row(id=42, name="Work", parent_id=40)],
                    )
                ],
            )
        ],
    )


def _instances(pool: Pool) -> dict[int, str]:
    with pool.connection() as conn:
        rows = conn.execute("SELECT folder_id, instance FROM store_folders").fetchall()
    return {int(row["folder_id"]): row["instance"] for row in rows}


def _file_in(folder_id: int, file_id: int = 78) -> None:
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200, json=shaped("FileResponse", id=file_id, filename="p", folder_id=folder_id)
        )
    )


@respx.mock
async def test_pre_instance_folders_placed_on_this_bambuddy_are_claimed(pool: Pool) -> None:
    """Rows from before #683 carry no instance (''). One whose folder sits where
    ScadBuddy put it on the configured Bambuddy is claimed for it, so the files already
    in that Work folder stay deletable."""
    _record_folders(pool, "", ("old", "template", 40), ("old", "work", 42))
    respx.get(f"{API}/library/folders").mock(return_value=_placed_tree())
    _file_in(42)
    delete = respx.delete(f"{API}/library/files/78").mock(return_value=httpx.Response(200, json={}))
    backend = BambuddyContentBackend(target(), pool)
    await backend.remove("78")
    assert delete.called
    assert _instances(pool) == {40: BASE_URL, 42: BASE_URL}
    await backend.aclose()


@respx.mock
async def test_pre_instance_folders_not_on_this_bambuddy_are_dropped_not_claimed(
    pool: Pool,
) -> None:
    """The #683 case across the upgrade: the URL was already repointed, so the legacy
    rows hold another instance's ids. Here id 42 is some unrelated folder (not under a
    recorded template folder). It must not become deletable, and the row is dropped
    rather than left for whichever instance comes next."""
    _record_folders(pool, "", ("old", "template", 50), ("old", "work", 42))
    respx.get(f"{API}/library/folders").mock(return_value=_placed_tree())
    _file_in(42)
    delete = respx.delete(f"{API}/library/files/78")
    backend = BambuddyContentBackend(target(), pool)
    with pytest.raises(RefusedDeleteError):
        await backend.remove("78")
    assert not delete.called
    assert _instances(pool) == {}
    await backend.aclose()


@respx.mock
async def test_a_pre_instance_row_this_bambuddy_already_records_is_dropped(pool: Pool) -> None:
    """A legacy row for a folder or a slot the instance already has (another process
    claimed or made it first) is not claimed twice and raises no unique violation."""
    _record_folders(pool, BASE_URL, ("old", "template", 40), ("old", "work", 42))
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO store_folders (instance, inbox_id, slug, role, folder_id)"
            " VALUES ('', %s, 'old', 'template', 40), ('', %s, 'old', 'work', 43)",
            (INBOX, INBOX),
        )
    respx.get(f"{API}/library/folders").mock(return_value=_placed_tree())
    _file_in(42)
    respx.delete(f"{API}/library/files/78").mock(return_value=httpx.Response(200, json={}))
    backend = BambuddyContentBackend(target(), pool)
    await backend.remove("78")
    assert _instances(pool) == {40: BASE_URL, 42: BASE_URL}
    await backend.aclose()


@respx.mock
async def test_a_live_switch_to_another_bambuddy_does_not_reuse_cached_folders(
    pool: Pool,
) -> None:
    """#683 inside one process: Settings repoints the URL and the next call sees it
    without a restart. The folder ids this process cached for the first instance must
    not be used on the second."""
    other_api = f"{OTHER}/api/v1"
    current = [BASE_URL]

    async def switching() -> BambuddyTarget:
        return BambuddyTarget(
            config=BambuddyConfig(base_url=current[0], api_key="narrow"), inbox_id=INBOX
        )

    for api in (API, other_api):
        respx.get(f"{api}/library/folders").mock(return_value=inbox_tree())
        respx.post(f"{api}/library/folders/").mock(side_effect=create_folder)
    respx.post(f"{API}/library/files").mock(return_value=uploaded())
    created_there = respx.post(f"{other_api}/library/folders/").mock(side_effect=create_folder)
    upload_there = respx.post(f"{other_api}/library/files").mock(return_value=uploaded())
    backend = BambuddyContentBackend(switching, pool)
    await backend.upload("piece", b"a", name="a.zip", scope=SCOPE)
    current[0] = OTHER
    await backend.upload("piece", b"b", name="b.zip", scope=SCOPE)
    assert created_there.call_count == 2  # its own template folder and Work
    assert upload_there.called
    with pool.connection() as conn:
        rows = conn.execute("SELECT DISTINCT instance FROM store_folders").fetchall()
    assert {row["instance"] for row in rows} == {BASE_URL, OTHER}
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
        pool,
        Settings(
            data_dir=tmp_path,
            database_url=UNUSED_DATABASE_URL,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
        ),
        ttl=0,
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
    # Fewer default-executor threads than finders, as on a small pod: a finder that
    # waits for its lock in a thread would take them all. (The loop is this test's own.)
    executor = ThreadPoolExecutor(4)
    asyncio.get_running_loop().set_default_executor(executor)
    workers = [BambuddyContentBackend(target(), small_pool) for _ in range(3)]
    scopes = [BlobScope(slug=f"kit-{n}", title=f"Kit {n}") for n in range(3)]
    uploads = [
        w.upload("piece", b"z", name="p.zip", scope=scope) for w in workers for scope in scopes
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*uploads), 10)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    assert created.call_count == 6  # a template folder and a Work per template
    for w in workers:
        await w.aclose()


def _advisory_locks(conninfo: str, key: int) -> int:
    """Held or awaited locks on `key` in the whole cluster: a bigint key is split into
    `classid` (high half) and `objid` (low half)."""
    unsigned = key & 0xFFFFFFFFFFFFFFFF
    with psycopg.connect(conninfo) as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
            " AND classid = %s AND objid = %s AND objsubid = 1",
            (unsigned >> 32, unsigned & 0xFFFFFFFF),
        ).fetchone()
    assert row is not None
    return int(row[0])


@respx.mock
async def test_a_find_cancelled_while_waiting_for_the_lock_leaves_none_held(
    pool: Pool, pg_conninfo: str
) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=inbox_tree())
    respx.post(f"{API}/library/folders/").mock(side_effect=create_folder)
    respx.post(f"{API}/library/files").mock(return_value=uploaded())
    key = folder_lock_key(BASE_URL, INBOX, SCOPE.slug or "", "template")
    with psycopg.connect(pg_conninfo, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (key,))
        assert _advisory_locks(pg_conninfo, key) == 1  # the key maps onto pg_locks as assumed
        first = BambuddyContentBackend(target(), pool)
        waiting = asyncio.create_task(first.upload("piece", b"a", name="a.zip", scope=SCOPE))
        await asyncio.sleep(0.5)  # blocked on the lock the holder has
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        holder.execute("SELECT pg_advisory_unlock(%s)", (key,))
    second = BambuddyContentBackend(target(), pool)
    await asyncio.wait_for(second.upload("piece", b"b", name="b.zip", scope=SCOPE), 5)
    assert _advisory_locks(pg_conninfo, key) == 0
    for backend in (first, second):
        await backend.aclose()


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
        Settings(
            data_dir=tmp_path,
            database_url=UNUSED_DATABASE_URL,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
        ),
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
