"""The render worker's Bambuddy store folders, found by the API (#5298 Task 17, inventory
R17, R18, plan D6): `RemoteStoreFolders` over `api/crud_render.py`. The API reads its own
render target, takes the folder's advisory lock (`folder_lock_key`, the 30 s
`lock_timeout`), finds or makes the folder in Bambuddy and records it, all inside one
request, so no lock is held across two. An older worker still finding over SQL takes the
same key, so the two exclude each other during a roll. A find is natural: the same slug and
role answer the same folder. A forget of nothing is a success."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import respx

from scadbuddy.bambuddy import commits
from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM, is_transient
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient
from scadbuddy.crud.render import (
    FIND_BUDGET,
    FIND_TIMEOUT,
    RemoteRenderSettingsSource,
    RemoteStoreFolders,
)
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from scadbuddy.store import bambuddy
from scadbuddy.store.bambuddy import (
    FOLDER_LOCK_SECONDS,
    FOLDER_LOCK_TIMEOUT,
    BambuddyContentBackend,
    BambuddyTarget,
    FolderFinder,
    Inbox,
    folder_lock_key,
    instance_key,
)
from scadbuddy.store.content import BlobScope
from tests.bambuddy.conftest import BASE_URL, recorded_schema, shaped
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import served_render_crud

pytestmark = pytest.mark.requires_postgres

API = f"{BASE_URL}/api/v1"
INBOX = 1
HERE = Inbox(instance_key(BASE_URL), INBOX)
SLUG = "dollhouse-kit"
TITLE = "Dollhouse Kit"
RENDER_KEY = "render-key-123456"


@pytest.fixture(autouse=True)
def no_commit_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commits, "COMMIT_WAITS", (0.0, 0.0, 0.0, 0.0))


@pytest.fixture
def keyed(tmp_path: Path, pg_conninfo: str) -> Settings:
    key = tmp_path / "secret.key"
    key.write_text(base64.b64encode(b"\x5a" * 32).decode() + "\n")
    return Settings(
        data_dir=tmp_path,
        database_url=pg_conninfo,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        secret_key_file=key,
        auth_mode="enforce",
    )


@pytest.fixture
def store(keyed: Settings) -> Iterator[SettingsStore]:
    opened = SettingsStore(keyed)
    opened.open()
    opened.save(
        SettingsPatch(
            store_backend="bambuddy",
            bambuddy_url=BASE_URL,
            library_folder_id=INBOX,
            bambuddy_api_key="full-key-123456",
            bambuddy_render_api_key=RENDER_KEY,
        )
    )
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
async def crud(store: SettingsStore, keyed: Settings) -> AsyncIterator[CrudClient]:
    async with served_render_crud(store.pool, keyed, unavailable_budget=0.5) as client:
        yield client


def _folder(**values: Any) -> dict[str, Any]:
    missing = set(recorded_schema("FolderTreeItem")["required"]) - set(values)
    assert not missing, f"FolderTreeItem requires {sorted(missing)}"
    return shaped("FolderTreeItem", **values)


def _inbox_tree() -> httpx.Response:
    return httpx.Response(200, json=[_folder(id=INBOX, name="ScadBuddy", parent_id=None)])


async def _create_folder(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    await asyncio.sleep(0.05)  # long enough for a second finder to arrive meanwhile
    ids = {TITLE: 10, "Work": 11}
    return httpx.Response(200, json=_folder(id=ids[body["name"]], **body))


def _bambuddy() -> tuple[respx.Route, respx.Route]:
    """Bambuddy's folder routes; the CRUD routes on the loopback pass through."""
    respx.route(host="127.0.0.1").pass_through()
    listing = respx.get(f"{API}/library/folders").mock(return_value=_inbox_tree())
    created = respx.post(f"{API}/library/folders/").mock(side_effect=_create_folder)
    return listing, created


def _rows(store: SettingsStore) -> list[tuple[str, int, str, str, int]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT instance, inbox_id, slug, role, folder_id FROM store_folders"
            " ORDER BY slug, role"
        ).fetchall()
    return [(r["instance"], r["inbox_id"], r["slug"], r["role"], r["folder_id"]) for r in rows]


def _record(store: SettingsStore, instance: str, *rows: tuple[str, str, int]) -> None:
    with store.pool.connection() as conn:
        for slug, role, folder_id in rows:
            conn.execute(
                "INSERT INTO store_folders (instance, inbox_id, slug, role, folder_id)"
                " VALUES (%s, %s, %s, %s, %s)",
                (instance, INBOX, slug, role, folder_id),
            )


@respx.mock
async def test_two_concurrent_finds_create_one_folder(
    crud: CrudClient, store: SettingsStore
) -> None:
    _, created = _bambuddy()
    workers = [RemoteStoreFolders(crud), RemoteStoreFolders(crud)]
    found = await asyncio.gather(*(w.find(None, HERE, SLUG, "work", TITLE) for w in workers))
    assert found == [11, 11]
    # One template folder and one Work, however many finders.
    assert [json.loads(c.request.content) for c in created.calls] == [
        {"name": TITLE, "parent_id": INBOX},
        {"name": "Work", "parent_id": 10},
    ]
    # The API called Bambuddy with the render key it holds, not the worker's.
    assert created.calls[0].request.headers["X-API-Key"] == RENDER_KEY
    assert _rows(store) == [
        (HERE.instance, INBOX, SLUG, "template", 10),
        (HERE.instance, INBOX, SLUG, "work", 11),
    ]
    # Natural: the same slug and role answer the same folder.
    assert await workers[0].find(None, HERE, SLUG, "template", TITLE) == 10


@respx.mock
async def test_a_find_after_a_recorded_folder_makes_no_bambuddy_call(
    crud: CrudClient, store: SettingsStore
) -> None:
    listing, created = _bambuddy()
    _record(store, HERE.instance, (SLUG, "template", 20), (SLUG, "work", 21))
    folders = RemoteStoreFolders(crud)
    assert await folders.find(None, HERE, SLUG, "work", TITLE) == 21
    assert await folders.find(None, HERE, SLUG, "template", TITLE) == 20
    assert not listing.called and not created.called


@respx.mock
async def test_an_old_worker_on_sql_and_the_api_exclude_each_other(
    crud: CrudClient, store: SettingsStore
) -> None:
    """A roll: one worker still finds over SQL, the other through the API. Both take
    `folder_lock_key`, so only one pair of folders is made."""
    _, created = _bambuddy()

    async def target() -> BambuddyTarget:
        return BambuddyTarget(
            config=BambuddyConfig(base_url=BASE_URL, api_key=RENDER_KEY), inbox_id=INBOX
        )

    old_worker = FolderFinder(store.pool)
    new_worker = RemoteStoreFolders(crud)
    target_now = await target()
    async with BambuddyClient(target_now.config) as client:
        found = await asyncio.gather(
            old_worker.find(client, HERE, SLUG, "work", TITLE),
            new_worker.find(None, HERE, SLUG, "work", TITLE),
        )
    assert list(found) == [11, 11]
    assert created.call_count == 2


@respx.mock
async def test_a_find_waits_for_the_lock_within_one_request(
    crud: CrudClient, store: SettingsStore, pg_conninfo: str
) -> None:
    """Another finder holds the folder's lock for a while: the API waits for it, inside
    the request, and the worker's call is not given up meanwhile."""
    _, created = _bambuddy()
    key = folder_lock_key(HERE.instance, INBOX, SLUG, "template")
    with psycopg.connect(pg_conninfo, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (key,))
        finding = asyncio.create_task(
            RemoteStoreFolders(crud).find(None, HERE, SLUG, "work", TITLE)
        )
        await asyncio.sleep(1.0)  # longer than the client's own 0.5 s budget
        assert not finding.done()
        holder.execute("SELECT pg_advisory_unlock(%s)", (key,))
    assert await asyncio.wait_for(finding, 10) == 11
    assert created.call_count == 2


def test_the_find_timeout_covers_the_lock_and_bambuddy() -> None:
    """One request waits for two finds (the template folder's, then Work's), each up to
    the 30 s lock and a listing and a create at Bambuddy's 30 s timeout. The budget for
    trying again is small: only a request that never reached the API is sent again."""
    assert FOLDER_LOCK_TIMEOUT == "30s"  # unchanged: an old worker takes the same
    one_find = FOLDER_LOCK_SECONDS + 2 * BambuddyConfig("x").timeout
    assert 2 * one_find <= FIND_TIMEOUT
    assert FIND_BUDGET <= 10


@respx.mock
async def test_a_lock_timeout_fails_the_call_at_once(
    crud: CrudClient, store: SettingsStore, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API's `lock_timeout` costs what it cost over SQL: one wait, then the call fails
    with the `LockNotAvailable` the SQL find raised, which Temporal's retry handles. The
    worker's client does not send it again."""
    _, created = _bambuddy()
    monkeypatch.setattr(bambuddy, "FOLDER_LOCK_TIMEOUT", "300ms")
    key = folder_lock_key(HERE.instance, INBOX, SLUG, "template")
    sent = 0
    folders = RemoteStoreFolders(crud)
    request = crud.request

    async def counted(*args: Any, **kwargs: Any) -> httpx.Response:
        nonlocal sent
        sent += 1
        return await request(*args, **kwargs)

    monkeypatch.setattr(crud, "request", counted)
    with psycopg.connect(pg_conninfo, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (key,))
        started = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            await folders.find(None, HERE, SLUG, "work", TITLE)
        elapsed = time.monotonic() - started
        holder.execute("SELECT pg_advisory_unlock(%s)", (key,))
    assert sent == 1
    assert elapsed < 3
    assert not created.called
    assert _rows(store) == []


@respx.mock
async def test_a_find_for_another_bambuddy_is_refused(
    crud: CrudClient, store: SettingsStore
) -> None:
    """The worker's settings are older than the API's (a URL or inbox changed in
    Settings): a folder of the API's Bambuddy would be another folder on the worker's,
    so the API makes none and the find fails, to be tried again."""
    listing, created = _bambuddy()
    folders = RemoteStoreFolders(crud)
    for elsewhere in (Inbox(instance_key("https://other.test"), INBOX), Inbox(HERE.instance, 2)):
        with pytest.raises(ApiError) as raised:
            await folders.find(None, elsewhere, SLUG, "work", TITLE)
        assert raised.value.status == 409
    assert not listing.called and not created.called


@respx.mock
async def test_bambuddys_refusal_reaches_the_worker_as_its_own(
    crud: CrudClient, store: SettingsStore
) -> None:
    """A Bambuddy 404 under the find (the inbox deleted in its UI) is the `ApiError` the
    SQL find raised, so an upload's forget-and-retry still sees it."""
    respx.route(host="127.0.0.1").pass_through()
    respx.get(f"{API}/library/folders").mock(return_value=_inbox_tree())
    respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )
    with pytest.raises(ApiError) as raised:
        await RemoteStoreFolders(crud).find(None, HERE, SLUG, "work", TITLE)
    assert raised.value.status == 404


async def test_forget_drops_a_templates_rows_and_of_nothing_succeeds(
    crud: CrudClient, store: SettingsStore
) -> None:
    other = instance_key("https://other.test")
    _record(store, HERE.instance, (SLUG, "template", 20), (SLUG, "work", 21), ("", "work", 31))
    _record(store, other, (SLUG, "work", 41))
    folders = RemoteStoreFolders(crud)
    await folders.forget(HERE, SLUG)
    await folders.forget(HERE, SLUG)  # again: nothing to forget is a success
    await folders.forget(HERE, "never-recorded")
    assert _rows(store) == [
        (HERE.instance, INBOX, "", "work", 31),
        (other, INBOX, SLUG, "work", 41),
    ]
    # The fonts' folder, whose slug is empty.
    await folders.forget(HERE, "")
    assert _rows(store) == [(other, INBOX, SLUG, "work", 41)]


async def test_work_folders_are_those_of_one_bambuddy_and_inbox(
    crud: CrudClient, store: SettingsStore
) -> None:
    _record(store, HERE.instance, (SLUG, "template", 20), (SLUG, "work", 21), ("", "work", 31))
    _record(store, instance_key("https://other.test"), (SLUG, "work", 41))
    folders = RemoteStoreFolders(crud)
    assert await folders.work_folders(HERE) == {21, 31}
    assert await folders.work_folders(Inbox(HERE.instance, 2)) == set()


@respx.mock
async def test_the_bambuddy_store_finds_its_folders_through_the_api(
    crud: CrudClient, store: SettingsStore
) -> None:
    """The worker's backend over `RemoteStoreFolders`: an upload's folder is the API's."""
    _, created = _bambuddy()
    upload = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(
            200, json=shaped("FileUploadResponse", id=500, filename="f", file_size=1)
        )
    )

    async def target() -> BambuddyTarget:
        return BambuddyTarget(
            config=BambuddyConfig(base_url=BASE_URL, api_key=RENDER_KEY), inbox_id=INBOX
        )

    backend = BambuddyContentBackend(target, store.pool, folders=RemoteStoreFolders(crud))
    try:
        scope = BlobScope(slug=SLUG, title=TITLE)
        assert await backend.upload("piece", b"a", name="a.zip", scope=scope) == "500"
        await backend.upload("piece", b"b", name="b.zip", scope=scope)
    finally:
        await backend.aclose()
    assert upload.calls[0].request.url.params["folder_id"] == "11"
    assert created.call_count == 2


@pytest.mark.parametrize(
    "down",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.Response(503, json={"detail": "starting"}),
    ],
    ids=["unreachable", "timeout", "5xx"],
)
@respx.mock
async def test_a_find_while_bambuddy_is_down_fails_at_once(
    crud: CrudClient, store: SettingsStore, down: Exception | httpx.Response
) -> None:
    """As over SQL: the call fails at once with the `ApiError` Bambuddy's client raised
    (a transient one, `is_transient`), so the activity's own retry takes over, rather
    than the CRUD client retrying it for its whole budget."""
    respx.route(host="127.0.0.1").pass_through()
    listing = respx.get(f"{API}/library/folders")
    if isinstance(down, httpx.Response):
        listing.mock(return_value=down)
    else:
        listing.mock(side_effect=down)
    started = time.monotonic()
    with pytest.raises(ApiError) as raised:
        await RemoteStoreFolders(crud).find(None, HERE, SLUG, "work", TITLE)
    assert time.monotonic() - started < 5
    assert listing.call_count == 1  # one request to the API, not a retry
    assert raised.value.status in (502, 504)
    assert raised.value.type == UNAVAILABLE_PROBLEM
    assert is_transient(raised.value)
    assert _rows(store) == []


@respx.mock
async def test_a_moved_target_rereads_the_settings_for_the_next_attempt(
    crud: CrudClient, store: SettingsStore
) -> None:
    """The worker's settings are older than a change in Settings: the 409 makes it read
    them again, so the next attempt (`pipelines.RETRY`'s, 2 s later) finds the folder
    on the Bambuddy and inbox Settings now name, rather than failing on the 30 s cache."""
    _bambuddy()
    upload = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(
            200, json=shaped("FileUploadResponse", id=500, filename="f", file_size=1)
        )
    )
    source = RemoteRenderSettingsSource(crud)  # the 30 s TTL
    fresh = await source.current()
    source.seed(fresh.model_copy(update={"library_folder_id": 2}))  # the inbox before
    backend = BambuddyContentBackend(
        source.target, store.pool, folders=RemoteStoreFolders(crud, source=source)
    )
    scope = BlobScope(slug=SLUG, title=TITLE)
    try:
        with pytest.raises(ApiError) as raised:
            await backend.upload("piece", b"a", name="a.zip", scope=scope)
        assert raised.value.status == 409
        assert await backend.upload("piece", b"a", name="a.zip", scope=scope) == "500"
    finally:
        await backend.aclose()
    assert upload.calls[0].request.url.params["folder_id"] == "11"
    assert (await source.current()).library_folder_id == INBOX


@respx.mock
async def test_a_refusal_reads_as_it_did_over_sql(crud: CrudClient, store: SettingsStore) -> None:
    """Bambuddy refusing the render key: the worker's `ApiError` has the status, title,
    type and upstream fields the SQL find's had."""
    respx.route(host="127.0.0.1").pass_through()
    respx.get(f"{API}/library/folders").mock(return_value=httpx.Response(403))
    target = BambuddyTarget(
        config=BambuddyConfig(base_url=BASE_URL, api_key=RENDER_KEY), inbox_id=INBOX
    )
    async with BambuddyClient(target.config) as client:
        with pytest.raises(ApiError) as over_sql:
            await FolderFinder(store.pool).find(client, HERE, SLUG, "work", TITLE)
    with pytest.raises(ApiError) as remote:
        await RemoteStoreFolders(crud).find(None, HERE, SLUG, "work", TITLE)
    expected, got = over_sql.value, remote.value
    assert (got.status, got.title, got.type, got.detail) == (
        expected.status,
        expected.title,
        expected.type,
        expected.detail,
    )
    assert got.extensions == expected.extensions
    assert "required_scope" in got.extensions
