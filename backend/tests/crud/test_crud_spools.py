"""The print worker's kept presets and a native slice's start through its CRUD routes
(area ``spools``, #5298 Task 31, inventory Q30): the reads answer as the SQL stores do, a
native slice's start sent twice starts once, another caller's credential is refused, and
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
from scadbuddy.auth.caller_grants import GRANTS
from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.bambuddy.models import LocalPresetDetail
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient, CrudRefusedError, CrudSyncClient
from scadbuddy.crud.guard import CRUD_HEADERS
from scadbuddy.crud.print import (
    BASE,
    HELD_PRESETS_API_ONLY,
    NATIVE_SLICES_API_ONLY,
    SLICE_SUBJECT,
    ApiOnlyError,
    RemoteHeldPresets,
    RemoteNativeSlices,
)
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.slicing.held_presets import HeldPresetStore
from scadbuddy.slicing.store import NativeSliceStore, SliceSubjectError
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import FixedToken, served_print_crud, threaded_print_crud

#: The `(store, remote, refused, composed)` of each store: the composed methods only call
#: others of the store's own, each of which the worker sends over a route. None here.
STORES: list[tuple[type, type, tuple[str, ...], set[str]]] = [
    (HeldPresetStore, RemoteHeldPresets, HELD_PRESETS_API_ONLY, set()),
    (NativeSliceStore, RemoteNativeSlices, NATIVE_SLICES_API_ONLY, set()),
]


def _public(cls: type) -> set[str]:
    return {
        name for name, _ in inspect.getmembers(cls, inspect.isfunction) if not name.startswith("_")
    }


# -- with no database --------------------------------------------------------------------


def test_the_area_is_served_and_awaited() -> None:
    assert "spools" in PRINT_CRUD_AREAS
    # Appended, so an older API's list is a prefix of a newer one's.
    assert CRUD_AREAS["print"][-4:] == ["spools", "transfer-urls", "access-codes", "filament"]


def test_the_print_worker_is_granted_no_table() -> None:
    """Its last tables went behind these routes: it holds no pool (#5298 Task 31), and
    it has no list at all since #5298 Task 35."""
    assert "print" not in GRANTS


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
    # Neither of these SQL stores has a public pool; the remote ones keep none of their own.
    assert not hasattr(RemoteHeldPresets(None), "_pool")  # type: ignore[arg-type]
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


async def test_nothing_to_ask_asks_nothing() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"asked {request.url}")

    client, blocking = _stand_in(answer)
    try:
        assert await RemoteHeldPresets(client).documents([]) == {}
    finally:
        await client.aclose()
        blocking.close()


#: A kept machine preset's document with Bambu Studio's print-host fields.
_HOSTED = {
    "print_host": "octopi.local",
    "printhost_apikey": "k-123",
    "printhost_password": "p-123",
    "printhost_user": "pi",
    "upload_token": "t-123",
    "printable_height": "300",
}
_CREDENTIALS = {"printhost_apikey", "printhost_password", "printhost_user", "upload_token"}


def _hosted(preset_id: int) -> LocalPresetDetail:
    return _preset(preset_id).model_copy(update={"preset_type": "printer", "setting": _HOSTED})


async def test_the_documents_route_never_serves_a_print_host_credential() -> None:
    """The documents route never forwards a print-host credential key (#5298 Task 32)."""

    class Held:
        async def documents(self, ids: list[int]) -> dict[int, LocalPresetDetail]:
            return {found: _hosted(found) for found in ids}

    slicing = SimpleNamespace(held=Held())
    answer = await crud_print.held_preset_documents(slicing, [7])  # type: ignore[arg-type]
    setting = answer[7].setting
    assert not _CREDENTIALS & set(setting)
    assert setting["print_host"] == "octopi.local" and setting["printable_height"] == "300"


async def test_the_remote_documents_drop_a_print_host_credential() -> None:
    """An answer that still carries one (an older API) is stripped by the worker too."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"7": _hosted(7).model_dump(mode="json")}, headers=CRUD_HEADERS
        )

    client, blocking = _stand_in(answer)
    try:
        setting = (await RemoteHeldPresets(client).documents([7]))[7].setting
    finally:
        await client.aclose()
        blocking.close()
    assert not _CREDENTIALS & set(setting)
    assert setting["print_host"] == "octopi.local"


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


def _preset(preset_id: int) -> LocalPresetDetail:
    return LocalPresetDetail(
        id=preset_id,
        name=f"Preset {preset_id}",
        preset_type="filament",
        source="local",
        updated_at="2026-11-10T12:00:00Z",
        setting={"filament_type": ["PLA"]},
    )


@pytest.mark.requires_postgres
async def test_the_reads_answer_as_the_sql_stores(
    store: SettingsStore, crud: CrudClient, blocking: CrudSyncClient
) -> None:
    held = HeldPresetStore(store.pool)
    await held.keep(_preset(7))
    await held.keep(_preset(9))

    remote_held = RemoteHeldPresets(crud)
    assert await remote_held.rows() == await held.rows()
    assert await remote_held.documents([7, 8]) == await held.documents([7, 8])
    assert set(await remote_held.documents([7, 9])) == {7, 9}


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
            ("POST", f"{BASE}/native-slices/start"),
        ):
            with pytest.raises(CrudRefusedError) as refused:
                client.request(method, path, json={})
            assert refused.value.status == 403, path
            assert refused.value.problem["reason"] == "wrong_caller", path
