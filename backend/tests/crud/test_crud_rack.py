"""The print worker's rack hotends' usage through its CRUD routes (#5298 Task 30,
inventory Q27): `RemoteRackUsage` answers as `RackUsageStore` does, ``seen`` sent twice
leaves what one left, every other write answers the rows it wrote once per
``Idempotency-Key`` (428 without one), a LAN start's picks replace an earlier pick in one
route (plan D6), the settles behave as the SQL path, another caller's credential is
refused, and the methods only the API calls are refused on the worker. Invented serials
only (spec 2026-10-01 §7)."""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.idempotency import KEY_REQUIRED_PROBLEM
from scadbuddy.crud.print import BASE, RACK_API_ONLY, ApiOnlyError, RemoteRackUsage
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from scadbuddy.rack.usage import PickedHotend, PickedSpool, RackUsageStore
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import FixedToken, served_print_crud, threaded_print_crud
from tests.printers.conftest import KEK
from tests.rack.helpers import serial

A, B, C = serial(17), serial(18), serial(19)
RUN = "run-5298"
FIRST, SECOND = "scadbuddy-run-5298-p1", "scadbuddy-run-5298-p1-c2"
GREEN = PickedSpool(slot_id=1, spool_id=7, label="Green", material="PLA", grams=10.0)
BLACK = PickedSpool(slot_id=2, spool_id=8, label="Black", material="PLA", grams=2.5)
#: The `RackUsageStore` methods that only call others of its own: none.
RACK_COMPOSED: set[str] = set()


def _picked(serial: str, *spools: PickedSpool, group_id: int = 0) -> PickedHotend:
    return PickedHotend(group_id=group_id, position=2, serial=serial, spools=list(spools))


def _public(cls: type) -> set[str]:
    """Its methods, but the ``_in`` ones, which run on a connection the worker has none of."""
    return {
        name
        for name, _ in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_") and not name.endswith("_in")
    }


# -- with no database --------------------------------------------------------------------


def test_the_area_is_served_and_awaited() -> None:
    assert "rack" in CRUD_AREAS["print"]
    assert "rack" in PRINT_CRUD_AREAS
    # Appended, so an older API's list is a prefix of a newer one's.
    assert CRUD_AREAS["print"][-1] == "rack"


def test_every_rack_method_is_remote_refused_or_named() -> None:
    """No `RackUsageStore` method reaches the worker's SQL unnoticed: each is overridden
    over the routes, refused as the API's, or named as one that only calls those."""
    public = _public(RackUsageStore)
    remote = {name for name in public if name in vars(RemoteRackUsage)}
    assert public - remote == RACK_COMPOSED
    assert set(RACK_API_ONLY) <= set(vars(RemoteRackUsage))
    # Every ``_in`` write is refused, so none runs on a connection of the worker's.
    in_writes = {
        name
        for name, _ in inspect.getmembers(RackUsageStore, inspect.isfunction)
        if name.endswith("_in")
    }
    assert in_writes <= set(RACK_API_ONLY)


@pytest.mark.parametrize("name", RACK_API_ONLY)
def test_a_rack_method_only_the_api_calls_is_refused(name: str) -> None:
    remote = RemoteRackUsage(None)  # type: ignore[arg-type]
    with pytest.raises(ApiOnlyError):
        getattr(remote, name)("x")


def test_the_worker_store_holds_no_pool() -> None:
    remote = RemoteRackUsage(None)  # type: ignore[arg-type]
    with pytest.raises(ApiOnlyError):
        _ = remote.pool
    remote.close()


async def test_nothing_to_ask_asks_nothing() -> None:
    """As the SQL store, an empty ask answers at once, with no call."""

    def answer(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"asked {request.url}")

    client = CrudClient(
        "http://api", "print", ServiceAuth(FixedToken()), transport=httpx.MockTransport(answer)
    )
    try:
        remote = RemoteRackUsage(client)
        assert await remote.usage(["", ""]) == {}
        assert await remote.picked_items([]) == set()
        assert await remote.recorded_archives([]) == set()
        assert await remote.record_picks(1, None, []) == 0
        assert await remote.record_lan_picks(RUN, FIRST, None, []) == 0
        await remote.seen(None, [""])
        await remote.seen("00000000-0000-4000-8000-0000000000ff", ["", ""])
    finally:
        await client.aclose()


async def test_a_retried_follow_attempt_s_settle_lan_replays_the_earlier_answer() -> None:
    """`WriteKeys` ``rack``: a retried attempt of one follow activity sends its same-body
    ``settle_lan`` under the key the earlier attempt used, so the API's ledger answers what
    that attempt's settle wrote, never runs it again. The ledger is a stand-in that keeps
    each key's body and reply, as `crud/idempotency.py` does."""
    ledger: dict[str, tuple[bytes, dict[str, Any]]] = {}
    ran: list[bytes] = []

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"{BASE}/rack/settle-lan"
        key = request.headers["Idempotency-Key"]
        if key not in ledger:
            ran.append(request.content)
            # The first run settles two picks; a second run would find none left.
            ledger[key] = (request.content, {"written": 2 if len(ran) == 1 else 0})
        body, reply = ledger[key]
        assert body == request.content
        return httpx.Response(200, json=reply, headers={"X-ScadBuddy-Crud": "1"})

    client = CrudClient(
        "http://api", "print", ServiceAuth(FixedToken()), transport=httpx.MockTransport(answer)
    )

    # One store, as the print worker holds one: `WriteKeys` counts each attempt apart.
    remote = RemoteRackUsage(client)

    async def settle() -> int:
        return await remote.settle_lan(RUN, FIRST)

    try:
        first = ActivityEnvironment()
        retried = ActivityEnvironment()
        retried.info = dataclasses.replace(first.info, attempt=first.info.attempt + 1)
        assert await first.run(settle) == 2
        assert await retried.run(settle) == 2
        assert len(ran) == 1 and len(ledger) == 1
    finally:
        await client.aclose()


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
def sql(pg_conninfo: str) -> Iterator[RackUsageStore]:
    """The API's own store, on the same schema."""
    opened = RackUsageStore(pg_conninfo)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
async def crud(store: SettingsStore, sql: RackUsageStore) -> AsyncIterator[CrudClient]:
    async with served_print_crud(store, rack=sql) as client:
        yield client


@pytest.fixture
def remote(crud: CrudClient) -> RemoteRackUsage:
    return RemoteRackUsage(crud)


@pytest.fixture
def printer(store: SettingsStore) -> str:
    return (
        PrinterStore(store.pool, KEK).create(PrinterCreate(serial="01P00A000005298", name="H2C")).id
    )


def _rows(store: SettingsStore, table: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return sorted((dict(row) for row in rows), key=lambda row: repr(sorted(row.items())))


def _all_rows(store: SettingsStore) -> list[list[dict[str, Any]]]:
    return [
        _rows(store, table)
        for table in (
            "rack_nozzle_seen",
            "rack_nozzle_picks",
            "rack_nozzle_pick_spools",
            "rack_nozzle_prints",
        )
    ]


def _picked_ago(conninfo: str, seconds: int) -> None:
    with psycopg.connect(conninfo) as conn:
        conn.execute(
            "UPDATE rack_nozzle_picks SET picked_at = now() - make_interval(secs => %s)"
            " WHERE lan_run_id IS NOT NULL",
            (seconds,),
        )


@pytest.mark.requires_postgres
async def test_the_reads_answer_as_the_sql_store(
    remote: RemoteRackUsage, sql: RackUsageStore, printer: str
) -> None:
    await sql.seen(printer, [A, B])
    await sql.record_picks(
        11, printer, [_picked(A, GREEN), _picked(B, BLACK, group_id=1)], copies=2
    )
    await sql.record_prints(
        archive_id=21, queue_item_id=11, settled_at=datetime.now(UTC), print_seconds=60, grams=4.0
    )
    await sql.record_lan_picks(RUN, FIRST, printer, [_picked(C, GREEN)])

    assert await remote.usage([A, B, C, "TEST-HOTEND-99"]) == await sql.usage(
        [A, B, C, "TEST-HOTEND-99"]
    )
    used = await remote.usage([A, C])
    assert (used[A].prints, used[A].pending, used[C].pending) == (1, 1, 1)
    assert used[A].first_seen_at is not None and used[A].last_used_at is not None
    assert await remote.picked_items([11, 12]) == await sql.picked_items([11, 12]) == {11}
    assert await remote.recorded_archives([21, 22]) == await sql.recorded_archives([21, 22])
    assert await remote.recorded_archives([21, 22]) == {21}


@pytest.mark.requires_postgres
async def test_seen_sent_twice_leaves_what_one_left(
    remote: RemoteRackUsage, store: SettingsStore, printer: str
) -> None:
    await remote.seen(printer, [A, B, ""])
    once = _all_rows(store)
    await remote.seen(printer, [A, B])
    assert _all_rows(store) == once
    assert {row["serial"] for row in once[0]} == {A, B}
    assert {str(row["printer"]) for row in once[0]} == {printer}


@pytest.mark.requires_postgres
async def test_each_keyed_write_answers_its_rows_once_per_key(
    crud: CrudClient, store: SettingsStore
) -> None:
    """A resend under its key, as the client sends one after a lost answer, answers the
    first write's rows and writes nothing more; a resend under a new key is a write of
    its own, which ``ON CONFLICT`` keeps from changing the rows."""
    picks = {
        "queue_item_id": 11,
        "printer": None,
        "picks": [_picked(A, GREEN, BLACK).model_dump(mode="json")],
        "copies": 1,
    }
    prints = {
        "archive_id": 21,
        "queue_item_id": 11,
        "settled_at": datetime.now(UTC).isoformat(),
        "print_seconds": 60,
        "grams": 4.0,
    }
    for path, body in (("picks", picks), ("prints", prints)):
        url = f"{BASE}/rack/{path}"
        for _ in range(2):
            answer = await crud.request("POST", url, json=body, idempotency_key=f"k-{path}")
            assert answer.json() == {"written": 1}, path
        once = _all_rows(store)
        assert (
            await crud.request("POST", url, json=body, idempotency_key=f"new-{path}")
        ).json() == {"written": 0}
        assert _all_rows(store) == once
        with pytest.raises(CrudRefusedError) as keyless:
            await crud.request("POST", url, json=body)
        assert keyless.value.status == 428, path
        assert keyless.value.problem["type"] == KEY_REQUIRED_PROBLEM
    assert len(_rows(store, "rack_nozzle_pick_spools")) == 2


@pytest.mark.requires_postgres
async def test_a_retried_activity_answers_what_its_lost_write_wrote(
    crud: CrudClient, sql: RackUsageStore
) -> None:
    """Inside an activity a write's key is the activity's (`WriteKeys` ``rack``): its
    retry, on a fresh store, answers what the attempt that lost its answer wrote; a
    retry whose write differs is sent again under a key of its own."""

    async def record(item: int) -> int:
        return await RemoteRackUsage(crud).record_picks(item, None, [_picked(A, GREEN)])

    assert await ActivityEnvironment().run(record, 11) == 1
    assert await ActivityEnvironment().run(record, 11) == 1
    assert await ActivityEnvironment().run(record, 12) == 1
    assert await sql.picked_items([11, 12]) == {11, 12}


@pytest.mark.requires_postgres
async def test_a_lan_start_s_picks_are_one_write_per_key(
    remote: RemoteRackUsage,
    crud: CrudClient,
    sql: RackUsageStore,
    store: SettingsStore,
    printer: str,
) -> None:
    """Resent with its key, a LAN start's picks answer the same and write once: the pick's
    time does not move. Picked again under a new key (a retried start, ranked again), the
    newer pick replaces the earlier one with its spools, in one route (plan D6)."""
    body = {
        "run_id": RUN,
        "subtask_name": FIRST,
        "printer": None,
        "picks": [
            _picked(A, GREEN, BLACK).model_dump(mode="json"),
            _picked(B, GREEN, group_id=1).model_dump(mode="json"),
        ],
    }
    url = f"{BASE}/rack/lan-picks"
    first = await crud.request("POST", url, json=body, idempotency_key="lan-1")
    once = _all_rows(store)
    again = await crud.request("POST", url, json=body, idempotency_key="lan-1")
    assert first.json() == again.json() == {"written": 2}
    assert _all_rows(store) == once
    with pytest.raises(CrudRefusedError) as keyless:
        await crud.request("POST", url, json=body)
    assert keyless.value.status == 428
    with pytest.raises(CrudRefusedError) as empty:
        await crud.request("POST", url, json={**body, "picks": []}, idempotency_key="lan-2")
    assert empty.value.status == 422
    assert _all_rows(store) == once

    assert await remote.record_lan_picks(RUN, FIRST, printer, [_picked(C, BLACK)]) == 1
    picks = _rows(store, "rack_nozzle_picks")
    assert [(row["serial"], row["group_id"], str(row["printer"])) for row in picks] == [
        (C, 0, printer)
    ]
    spools = _rows(store, "rack_nozzle_pick_spools")
    assert [(row["group_id"], row["slot_id"]) for row in spools] == [(0, 2)]
    assert (await sql.usage([A, C]))[C].pending == 1


@pytest.mark.requires_postgres
async def test_the_settles_behave_as_the_sql_path(
    remote: RemoteRackUsage, sql: RackUsageStore, pg_conninfo: str, printer: str
) -> None:
    """A run's settle and the watch's settle of a start, as `RackUsageStore`'s: timed
    from the pick, grams the group's, and a start settled already writes nothing."""
    await remote.record_lan_picks(RUN, FIRST, printer, [_picked(A, GREEN, BLACK)])
    await remote.record_lan_picks(
        RUN, SECOND, printer, [_picked(B, GREEN), _picked(C, BLACK, group_id=1)]
    )
    _picked_ago(pg_conninfo, 600)

    ended_at = datetime.now(UTC)
    assert await remote.settle_lan_ended(printer, FIRST, ended_at) == 1
    assert await remote.settle_lan_ended(printer, FIRST, ended_at) == 0
    used = (await sql.usage([A]))[A]
    assert (used.prints, used.pending, used.grams) == (1, 0, 12.5)
    assert 595 <= used.print_seconds <= 660

    assert await remote.settle_lan(RUN, timed=False) == 2
    assert await remote.settle_lan(RUN) == 0
    used = (await sql.usage([B]))[B]
    assert (used.prints, used.print_seconds, used.grams) == (1, 0, 10.0)
    assert await remote.settle_lan("another-run", FIRST) == 0


@pytest.mark.requires_postgres
async def test_a_settle_resent_under_its_key_answers_the_same(
    crud: CrudClient, printer: str, sql: RackUsageStore
) -> None:
    await sql.record_lan_picks(RUN, FIRST, printer, [_picked(A, GREEN)])
    ended = {
        "printer": printer,
        "subtask_name": FIRST,
        "ended_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        "timed": True,
    }
    run = {"run_id": RUN, "subtask_name": None, "timed": True}
    for path, body in (("settle-lan-ended", ended), ("settle-lan", run)):
        url = f"{BASE}/rack/{path}"
        first = await crud.request("POST", url, json=body, idempotency_key=f"s-{path}")
        again = await crud.request("POST", url, json=body, idempotency_key=f"s-{path}")
        assert first.json() == again.json(), path
        with pytest.raises(CrudRefusedError) as keyless:
            await crud.request("POST", url, json=body)
        assert keyless.value.status == 428, path
    assert (await sql.usage([A]))[A].prints == 1


@pytest.mark.requires_postgres
def test_another_callers_credential_is_refused(store: SettingsStore) -> None:
    with threaded_print_crud(store, caller="projects") as client:
        for method, path in (
            ("POST", f"{BASE}/rack/seen"),
            ("POST", f"{BASE}/rack/usage"),
            ("GET", f"{BASE}/rack/picked?id=1"),
            ("GET", f"{BASE}/rack/recorded-archives?id=1"),
            ("POST", f"{BASE}/rack/picks"),
            ("POST", f"{BASE}/rack/prints"),
            ("POST", f"{BASE}/rack/lan-picks"),
            ("POST", f"{BASE}/rack/settle-lan"),
            ("POST", f"{BASE}/rack/settle-lan-ended"),
        ):
            with pytest.raises(CrudRefusedError) as refused:
                client.request(method, path, json={})
            assert refused.value.status == 403, path
            assert refused.value.problem["reason"] == "wrong_caller", path
