"""The print worker's native slice start through its CRUD routes (area ``spools``, #5298
Task 31, inventory Q30): a native slice's start sent twice starts once, the retired kept
presets' reads answer empty (#7244), another caller's credential is refused, and
every method the worker does not call is refused on it. The spool overlay's routes went
with it (#6262 Task 12): they answer 404."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from scadbuddy.api import crud_print
from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient, CrudRefusedError, CrudSyncClient
from scadbuddy.crud.guard import CRUD_HEADERS
from scadbuddy.crud.print import (
    BASE,
    NATIVE_SLICES_API_ONLY,
    OWN_PRESET_DOCUMENTS_MAX,
    SLICE_SUBJECT,
    ApiOnlyError,
    RemoteFilamentPresets,
    RemoteNativeSlices,
)
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.slicing.own_presets import OwnRow
from scadbuddy.slicing.store import NativeSliceStore, SliceSubjectError
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import FixedToken, served_print_crud, threaded_print_crud

#: The `(store, remote, refused, composed)` of each store: the composed methods only call
#: others of the store's own, each of which the worker sends over a route. None here.
STORES: list[tuple[type, type, tuple[str, ...], set[str]]] = [
    (NativeSliceStore, RemoteNativeSlices, NATIVE_SLICES_API_ONLY, set()),
]


def _public(cls: type) -> set[str]:
    return {
        name for name, _ in inspect.getmembers(cls, inspect.isfunction) if not name.startswith("_")
    }


# -- with no database --------------------------------------------------------------------


def test_the_area_is_served_and_awaited() -> None:
    assert "spools" in PRINT_CRUD_AREAS
    # Appended, so an older API's list is a prefix of a newer one's: each came after the one before.
    # Check the order, not the list's tail, which every new area extends.
    areas = CRUD_AREAS["print"]
    positions = [
        areas.index(area)
        for area in [
            "spools",
            "transfer-urls",
            "access-codes",
            "filament",
            "filament-sync",
            "filament-pull",
        ]
    ]
    assert positions == sorted(positions)


@pytest.mark.parametrize(("store", "remote", "refused", "composed"), STORES)
def test_every_method_is_remote_refused_or_named(
    store: type, remote: type, refused: tuple[str, ...], composed: set[str]
) -> None:
    """No method of the SQL store reaches the worker's SQL unnoticed: each is overridden
    over the routes, refused as the API's, or named as one that only calls those."""
    public = _public(store)
    overridden = {name for name in public if name in vars(remote)}
    assert public - overridden == composed
    assert set(refused) <= overridden
    # Every ``_in`` write runs on a connection the worker has none of.
    assert {name for name in public if name.endswith("_in")} <= set(refused)


@pytest.mark.parametrize(
    ("remote", "name"),
    [(remote, name) for _, remote, refused, _ in STORES for name in refused],
)
def test_a_method_only_the_api_calls_is_refused(remote: type, name: str) -> None:
    # Refused when called, before any await: no coroutine is made.
    with pytest.raises(ApiOnlyError):
        getattr(remote(None), name)("x")


def test_the_worker_stores_hold_no_pool() -> None:
    # The SQL store has no public pool; the remote one keeps none of its own.
    assert not hasattr(RemoteNativeSlices(None), "_pool")  # type: ignore[arg-type]


def _stand_in(answer: Any) -> tuple[CrudClient, CrudSyncClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        return answer(request)  # type: ignore[no-any-return]

    transport = httpx.MockTransport(handler)
    auth = ServiceAuth(FixedToken())
    return (
        CrudClient("http://api", "print", auth, transport=transport),
        CrudSyncClient("http://api", "print", auth, transport=transport),
    )


#: A preset document with Bambu Studio's print-host fields.
_HOSTED = {
    "print_host": "octopi.local",
    "printhost_apikey": "k-123",
    "printhost_password": "p-123",
    "printhost_user": "pi",
    "upload_token": "t-123",
    "printable_height": "300",
}
_CREDENTIALS = {"printhost_apikey", "printhost_password", "printhost_user", "upload_token"}


async def test_the_retired_held_presets_reads_answer_empty() -> None:
    """No copy of Bambuddy's local presets is kept (#7244): a print worker of an older
    build, during a roll, reads none and so slices none."""
    assert await crud_print.held_presets() == []
    assert await crud_print.held_preset_documents([7]) == {}


def test_the_own_presets_area_is_served_and_awaited() -> None:
    """ScadBuddy's own filament presets (#7176), an area of their own, appended."""
    assert "own-presets" in PRINT_CRUD_AREAS
    areas = CRUD_AREAS["print"]
    assert areas.index("own-presets") > areas.index("sensor-notify")


async def test_the_own_presets_cross_as_the_store_answers() -> None:
    """The worker reads the rows and documents the API's store answers, and strips a
    print-host credential an older API might still send."""
    row = OwnRow(
        id=12,
        name="Silk",
        sha256="a" * 64,
        filament_type="PLA",
        filament_colour="#C0C0C0",
        compatible_printers=["Bambu Lab H2C 0.4 nozzle"],
        bb_preset_id=141,
    )

    class Own:
        async def rows(self) -> list[OwnRow]:
            return [row]

        async def documents(self, ids: list[int]) -> dict[int, dict[str, Any]]:
            return {found: {**_HOSTED, "name": "Silk"} for found in ids if found == 12}

    slicing = SimpleNamespace(own=Own())
    rows = await crud_print.own_filament_presets(slicing)  # type: ignore[arg-type]
    docs = await crud_print.filament_preset_documents(slicing, [12, 13])  # type: ignore[arg-type]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/own-presets"):
            body: Any = [r.model_dump(mode="json") for r in rows]
        else:
            assert request.url.params.get_list("id") == ["12", "13"]
            body = {str(k): v for k, v in docs.items()}
        return httpx.Response(200, json=body, headers=CRUD_HEADERS)

    client, blocking = _stand_in(answer)
    try:
        remote = RemoteFilamentPresets(client)
        assert await remote.rows() == [row]
        got = await remote.documents([13, 12])
    finally:
        await client.aclose()
        blocking.close()
    assert set(got) == {12}
    assert not _CREDENTIALS & set(got[12])
    assert got[12]["print_host"] == "octopi.local"


async def test_many_own_preset_documents_are_read_in_batches() -> None:
    """More ids than one request may name (the route's cap) are sent in batches, and
    every answer is kept."""
    asked: list[int] = []

    def answer(request: httpx.Request) -> httpx.Response:
        ids = [int(found) for found in request.url.params.get_list("id")]
        assert 0 < len(ids) <= OWN_PRESET_DOCUMENTS_MAX
        asked.append(len(ids))
        return httpx.Response(
            200, json={str(found): {"name": str(found)} for found in ids}, headers=CRUD_HEADERS
        )

    client, blocking = _stand_in(answer)
    try:
        got = await RemoteFilamentPresets(client).documents(range(1, 452))
    finally:
        await client.aclose()
        blocking.close()
    assert asked == [200, 200, 51]
    assert sorted(got) == list(range(1, 452))


async def test_the_routes_refusals_are_raised_as_the_sql_stores_raise() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"type": "about:blank", "detail": "slice x belongs to y", "reason": SLICE_SUBJECT},
            headers=CRUD_HEADERS,
        )

    client, blocking = _stand_in(answer)
    try:
        with pytest.raises(SliceSubjectError):
            await RemoteNativeSlices(client).start(
                workflow_id="slice-1-x",
                subject="output:a",
                library_file_id=1,
                plate_id=1,
                preset_key="k",
            )
    finally:
        await client.aclose()
        blocking.close()


# -- over the routes ---------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, pg_conninfo: str) -> Iterator[SettingsStore]:
    opened = SettingsStore(
        Settings(
            data_dir=tmp_path,
            database_url=pg_conninfo,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            auth_mode="enforce",
        )
    )
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
async def crud(store: SettingsStore) -> AsyncIterator[CrudClient]:
    async with served_print_crud(store) as client:
        yield client


@pytest.fixture
def blocking(store: SettingsStore) -> Iterator[CrudSyncClient]:
    with threaded_print_crud(store) as client:
        yield client


def _rows(store: SettingsStore, table: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return sorted((dict(row) for row in rows), key=lambda row: repr(sorted(row.items())))


@pytest.mark.requires_postgres
async def test_the_held_presets_routes_answer_empty(crud: CrudClient) -> None:
    assert (await crud.request("GET", f"{BASE}/held-presets")).json() == []
    answer = await crud.request("GET", f"{BASE}/held-presets/documents?id=7")
    assert answer.json() == {}


@pytest.mark.requires_postgres
@pytest.mark.parametrize(
    "path", ["/spools/records", "/spools/refreshes/native/failed", "/spools/overlay"]
)
async def test_the_overlays_routes_are_gone(crud: CrudClient, path: str) -> None:
    """#6262 Task 12: nothing serves them, so a call from a print worker from before
    Task 6 is never answered from tables nothing keeps (the client reads a route with no
    CRUD header as missing)."""
    answer = await crud._http.post(f"{BASE}{path}", json={}, headers={"Idempotency-Key": "k"})
    assert answer.status_code == 404


@pytest.mark.requires_postgres
async def test_a_native_slice_sent_twice_starts_once(
    store: SettingsStore, crud: CrudClient
) -> None:
    remote = RemoteNativeSlices(crud)
    ask: dict[str, Any] = {
        "workflow_id": "slice-5298-abc",
        "subject": "output:a",
        "library_file_id": 5298,
        "plate_id": 1,
        "preset_key": "k",
    }
    first = await remote.start(**ask)
    again = await remote.start(**ask)
    assert first.id == again.id and again.status == "running"
    assert len(_rows(store, "native_slices")) == 1
    with pytest.raises(SliceSubjectError):
        await remote.start(**{**ask, "subject": "output:b"})
    assert len(_rows(store, "native_slices")) == 1


@pytest.mark.requires_postgres
def test_another_callers_credential_is_refused(store: SettingsStore) -> None:
    with threaded_print_crud(store, caller="projects") as client:
        for method, path in (
            ("GET", f"{BASE}/held-presets"),
            ("GET", f"{BASE}/held-presets/documents?id=1"),
            ("GET", f"{BASE}/own-presets"),
            ("GET", f"{BASE}/own-presets/documents?id=1"),
            ("POST", f"{BASE}/native-slices/start"),
        ):
            with pytest.raises(CrudRefusedError) as refused:
                client.request(method, path, json={})
            assert refused.value.status == 403, path
            assert refused.value.problem["reason"] == "wrong_caller", path
