"""The print worker's spools, ScadBuddy's own spools, the kept presets and a native
slice's start through its CRUD routes (#5298 Task 31, inventory Q28-Q30): the reads answer
as the SQL stores do, a natural write sent twice leaves what one left, a provider's
records and a run's settle answer once per ``Idempotency-Key`` (428 without one), a
native slice's start sent twice starts once, another caller's credential is refused, and
every method the worker does not call is refused on it. Invented tags only (spec
2026-10-01 §7)."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from psycopg.types.json import Jsonb

from scadbuddy.api import crud_print
from scadbuddy.api.components import getter_for
from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.auth.caller_grants import GRANTS, PRINT_GRANTS, PRINT_SEQUENCES, SEQUENCES
from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.bambuddy.models import LocalPresetDetail
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import (
    CrudClient,
    CrudRefusedError,
    CrudSyncClient,
    CrudUnavailableError,
)
from scadbuddy.crud.guard import CRUD_HEADERS
from scadbuddy.crud.idempotency import KEY_REQUIRED_PROBLEM
from scadbuddy.crud.print import (
    BASE,
    DEADLINE_MARGIN,
    HELD_PRESETS_API_ONLY,
    NATIVE_SLICES_API_ONLY,
    NATIVE_SPOOLS_API_ONLY,
    PAST_DEADLINE,
    SLICE_SUBJECT,
    SPOOLS_API_ONLY,
    ApiOnlyError,
    RemoteHeldPresets,
    RemoteNativeSlices,
    RemoteNativeSpools,
    RemoteSpoolStore,
)
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.slicing.held_presets import HeldPresetStore
from scadbuddy.slicing.store import NativeSliceStore, SliceSubjectError
from scadbuddy.spools.component import SPOOLS
from scadbuddy.spools.models import SeenTray, SpoolSnapshot
from scadbuddy.spools.native import NativeSpoolCreate, NativeSpoolStore, SpoolUse
from scadbuddy.spools.store import SpoolStore
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import FixedToken, served_print_crud, threaded_print_crud

PRINTER = uuid.UUID("5d0c8f1e-0000-4000-8000-000000005298")
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
#: An invented tag, as a tray reports one.
TAG = "A1B2C3D4E5F60718"
RUN = "run-5298-spools"

#: The `(store, remote, refused, composed)` of each store: the composed methods only call
#: others of the store's own, each of which the worker sends over a route. None here.
STORES: list[tuple[type, type, tuple[str, ...], set[str]]] = [
    (SpoolStore, RemoteSpoolStore, SPOOLS_API_ONLY, set()),
    (NativeSpoolStore, RemoteNativeSpools, NATIVE_SPOOLS_API_ONLY, set()),
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
    assert CRUD_AREAS["print"][-1] == "spools"


def test_the_print_worker_is_granted_no_table() -> None:
    """Its last tables went behind these routes: it holds no pool (#5298 Task 31)."""
    assert PRINT_GRANTS == {} and GRANTS["print"] == {}
    assert PRINT_SEQUENCES == () and SEQUENCES["print"] == ()


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
    with pytest.raises(ApiOnlyError):
        _ = RemoteSpoolStore(None).pool  # type: ignore[arg-type]
    with pytest.raises(ApiOnlyError):
        _ = RemoteNativeSpools(None).pool  # type: ignore[arg-type]
    # Neither SQL store has a public pool; the remote ones keep none of their own.
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
        RemoteNativeSpools(blocking).record_uses(RUN, "p1", [])
    finally:
        await client.aclose()
        blocking.close()


async def test_the_routes_refusals_are_raised_as_the_sql_stores_raise() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        reason = SLICE_SUBJECT if request.url.path.endswith("/start") else PAST_DEADLINE
        return httpx.Response(
            409,
            json={"type": "about:blank", "detail": "slice x belongs to y", "reason": reason},
            headers=CRUD_HEADERS,
        )

    client, blocking = _stand_in(answer)
    try:
        with pytest.raises(TimeoutError):
            await RemoteSpoolStore(client).replace_records("native", [], NOW, timeout=5)
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


class _PastDeadlineStore:
    """A `SpoolStore` stand-in whose write is past its deadline: what the route gets."""

    pool = object()

    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    def deadline(self, timeout: float | None) -> float | None:
        self.timeouts.append(timeout)
        return None if timeout is None else 100.0 + timeout

    def replace_records_in(self, *args: Any) -> None:
        raise TimeoutError("past")


async def test_a_write_past_its_deadline_is_409_and_raised_as_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route maps a write past its deadline to 409 ``past-deadline``, and the remote
    store raises it as `TimeoutError`, as the SQL store does. The route's deadline is the
    caller's ``timeout`` less `DEADLINE_MARGIN`, so the API gives up first."""
    store = _PastDeadlineStore()
    monkeypatch.setattr(
        crud_print, "_once_on", lambda pool, key, route, body, write: write(object())
    )
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(crud_print.router, prefix="/api/v1")
    [guard] = crud_print.router.dependencies
    assert guard.dependency is not None
    app.dependency_overrides[guard.dependency] = lambda: None
    app.dependency_overrides[getter_for(SPOOLS)] = lambda: SimpleNamespace(store=store)
    client = CrudClient(
        "http://api",
        "print",
        ServiceAuth(FixedToken()),
        transport=httpx.ASGITransport(app=app),
    )
    try:
        with pytest.raises(TimeoutError):
            await RemoteSpoolStore(client).replace_records("native", [], NOW, timeout=5)
        answer = await client._http.post(
            f"{BASE}/spools/records",
            json={"provider": "native", "snapshots": [], "at": NOW.isoformat(), "timeout": 2},
            headers={"Idempotency-Key": "k"},
        )
    finally:
        await client.aclose()
    assert store.timeouts == [5 - DEADLINE_MARGIN, 2]
    assert answer.status_code == 409 and answer.json()["reason"] == PAST_DEADLINE


async def test_a_write_with_a_timeout_is_sent_once_and_waits_it_out() -> None:
    """No answer: no retry, and the call raises only once its ``timeout`` has passed, by
    when the API's own deadline has passed too."""
    asked: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(503, json={"type": "about:blank"}, headers=CRUD_HEADERS)

    client, blocking = _stand_in(answer)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        with pytest.raises(CrudUnavailableError):
            await RemoteSpoolStore(client).replace_records(
                "native", [], NOW, timeout=DEADLINE_MARGIN + 0.2
            )
        with pytest.raises(TimeoutError):
            await RemoteSpoolStore(client).replace_records(
                "native", [], NOW, timeout=DEADLINE_MARGIN
            )
    finally:
        await client.aclose()
        blocking.close()
    assert loop.time() - started >= DEADLINE_MARGIN + 0.2
    assert len(asked) == 1


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


def _tray(tag: str | None = TAG) -> SeenTray:
    return SeenTray(ams_id=0, tray_id=1, tag_uid=tag, material="PLA", colour="#FF0000")


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
    sql = SpoolStore(store.pool)
    native = NativeSpoolStore(store.pool)
    held = HeldPresetStore(store.pool)
    made = native.create(NativeSpoolCreate(material="PLA", colour="#FF0000", tag_uid=TAG))
    native.create(NativeSpoolCreate(material="PETG"))
    await sql.replace_records("native", [spool.snapshot() for spool in native.list()], NOW)
    await sql.replace_records(
        "bambuddy", [SpoolSnapshot(record_id="16", material="PLA", tag_uid=TAG)], NOW
    )
    await held.keep(_preset(7))
    await held.keep(_preset(9))

    trays = [_tray(), _tray(None)]
    ask: dict[str, Any] = {"now": NOW, "providers": ["native", "bambuddy"]}
    remote = RemoteSpoolStore(crud)
    assert await remote.overlay(PRINTER, trays, **ask) == await sql.overlay(PRINTER, trays, **ask)
    overlay = await remote.overlay(PRINTER, trays, **ask)
    assert overlay.trays[0].records["native"].record_id == str(made.id)

    remote_native = RemoteNativeSpools(blocking)
    assert await asyncio.to_thread(remote_native.list) == native.list()
    assert await asyncio.to_thread(
        lambda: remote_native.list(include_archived=True)
    ) == native.list(include_archived=True)
    assert await asyncio.to_thread(remote_native.any) is True

    remote_held = RemoteHeldPresets(crud)
    assert await remote_held.rows() == await held.rows()
    assert await remote_held.documents([7, 8]) == await held.documents([7, 8])
    assert set(await remote_held.documents([7, 9])) == {7, 9}


@pytest.mark.requires_postgres
async def test_natural_writes_sent_twice_leave_what_one_left(
    store: SettingsStore, crud: CrudClient, blocking: CrudSyncClient
) -> None:
    native = NativeSpoolStore(store.pool)
    spool = native.create(NativeSpoolCreate(material="PLA", remaining_g=100))
    remote = RemoteSpoolStore(crud)
    await remote.failed("bambuddy", NOW, "ConnectError")
    once = _rows(store, "spool_provider_refreshes")
    await remote.failed("bambuddy", NOW, "ConnectError")
    assert _rows(store, "spool_provider_refreshes") == once
    assert [(row["provider"], row["failure"]) for row in once] == [("bambuddy", "ConnectError")]

    uses = [SpoolUse(slot_id=1, spool_id=spool.id, grams=12.5)]
    remote_native = RemoteNativeSpools(blocking)
    await asyncio.to_thread(remote_native.record_uses, RUN, "p1", uses)
    once = _rows(store, "native_spool_uses")
    await asyncio.to_thread(remote_native.record_uses, RUN, "p1", uses)
    assert _rows(store, "native_spool_uses") == once and len(once) == 1


@pytest.mark.requires_postgres
async def test_a_providers_records_are_written_once_per_key(
    store: SettingsStore, crud: CrudClient
) -> None:
    """The records, their RFID links and the refresh's stamp, written whole under the
    store's lock (plan D6): a resend under its key answers the same and writes nothing
    more; a write with no key is 428 and writes nothing."""
    body = {
        "provider": "bambuddy",
        "snapshots": [SpoolSnapshot(record_id="16", tag_uid=TAG).model_dump(mode="json")],
        "at": NOW.isoformat(),
        "timeout": None,
    }
    url = f"{BASE}/spools/records"
    with pytest.raises(CrudRefusedError) as keyless:
        await crud.request("POST", url, json=body)
    assert keyless.value.status == 428
    assert keyless.value.problem["type"] == KEY_REQUIRED_PROBLEM
    assert _rows(store, "spool_provider_records") == []

    first = await crud.request("POST", url, json=body, idempotency_key="records-1")
    once = [_rows(store, table) for table in ("spool_provider_records", "physical_spools")]
    again = await crud.request("POST", url, json=body, idempotency_key="records-1")
    assert first.status_code == again.status_code == 204
    assert [_rows(store, t) for t in ("spool_provider_records", "physical_spools")] == once
    assert len(once[1]) == 1

    await RemoteSpoolStore(crud).replace_records("bambuddy", [], NOW)
    assert _rows(store, "spool_provider_records") == []


@pytest.mark.requires_postgres
async def test_a_runs_settle_answers_once_per_key(
    store: SettingsStore, crud: CrudClient, blocking: CrudSyncClient
) -> None:
    native = NativeSpoolStore(store.pool)
    spool = native.create(NativeSpoolCreate(material="PLA", remaining_g=100))
    native.record_uses(RUN, "p1", [SpoolUse(slot_id=1, spool_id=spool.id, grams=12.5)])
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, result)"
            " VALUES (%s, 'out', 'k', 'succeeded', %s)",
            (RUN, Jsonb({"lan": [{"subtask_name": "p1"}]})),
        )
    body = {"run_id": RUN, "stages": ["done"]}
    url = f"{BASE}/native-spools/uses/settle"
    with pytest.raises(CrudRefusedError) as keyless:
        await crud.request("POST", url, json=body)
    assert keyless.value.status == 428
    first = await crud.request("POST", url, json=body, idempotency_key="settle-1")
    again = await crud.request("POST", url, json=body, idempotency_key="settle-1")
    assert first.json() == again.json() == {"changed": [str(spool.id)]}
    after = native.get(spool.id)
    assert after is not None and after.remaining_g == 87.5
    # A new settle of a settled run changes nothing, as the SQL store's.
    assert await asyncio.to_thread(RemoteNativeSpools(blocking).settle_uses, RUN, ["done"]) == set()


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
            ("POST", f"{BASE}/spools/records"),
            ("POST", f"{BASE}/spools/refreshes/native/failed"),
            ("POST", f"{BASE}/spools/overlay"),
            ("GET", f"{BASE}/native-spools"),
            ("GET", f"{BASE}/native-spools/any"),
            ("POST", f"{BASE}/native-spools/uses"),
            ("POST", f"{BASE}/native-spools/uses/settle"),
            ("GET", f"{BASE}/held-presets"),
            ("GET", f"{BASE}/held-presets/documents?id=1"),
            ("POST", f"{BASE}/native-slices/start"),
        ):
            with pytest.raises(CrudRefusedError) as refused:
                client.request(method, path, json={})
            assert refused.value.status == 403, path
            assert refused.value.problem["reason"] == "wrong_caller", path
