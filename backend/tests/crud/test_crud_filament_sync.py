"""The Bambuddy sync's CRUD routes (#6265 Task 6): the print worker plans, records what
Bambuddy answered and reports a pass through them, only with the print credential, and
`RemoteFilamentSync` and `LocalFilamentSync` give one answer. Invented ids only."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.core.settings import FilamentSync, Settings
from scadbuddy.crud.client import CrudClient, CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.print import BASE, RemoteFilamentSync
from scadbuddy.filament.bb_sync_models import (
    SYNC_BUSY,
    BbSnapshot,
    BbWrite,
    BbWriteResult,
    PlanRequest,
    ResultsReport,
    ResultsRequest,
    SyncPassResult,
    SyncPlan,
    without_empty_unconfirmed,
)
from scadbuddy.filament.bb_sync_plan import SYNC_LOCK, plan
from scadbuddy.filament.bb_sync_results import LocalFilamentSync, sync_status
from scadbuddy.filament.models import Material, SpoolCreate
from scadbuddy.library.settings_store import SettingsPatch, SettingsStore
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import (
    filament_on,
    print_crud_app,
    served_print_crud,
    threaded_print_crud,
)
from tests.filament import bb_sync_scale as scale

pg = pytest.mark.requires_postgres

INSTANCE = "0123456789ab"
#: Only the spools push.
SPOOLS = FilamentSync(
    spools="sb_to_bb", weights="off", tags="off", presets="off", suppliers="off", locations="off"
)
PLAN = f"{BASE}/filament/sync/plan"
RESULTS = f"{BASE}/filament/sync/results"
PASS = f"{BASE}/filament/sync/pass"
DIRECTIONS = f"{BASE}/filament/sync/directions"


def test_the_print_areas_list_filament_sync() -> None:
    """The routes are an area of their own, appended after the older ones, so a new worker waits
    for an API that serves them (`wait_for_areas`) rather than planning into a 404."""
    assert "filament-sync" in PRINT_CRUD_AREAS
    assert "filament-sync" in CRUD_AREAS["print"]
    assert CRUD_AREAS["print"].index("filament-pull") > CRUD_AREAS["print"].index("filament-sync")
    assert set(CRUD_AREAS["print"]) == PRINT_CRUD_AREAS


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
    opened.save(SettingsPatch(filament_sync=SPOOLS))
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
async def crud(store: SettingsStore) -> AsyncIterator[CrudClient]:
    async with served_print_crud(store) as client:
        yield client


def _spools(store: SettingsStore, count: int) -> list[UUID]:
    filament = filament_on(store.pool)
    material = next(
        r.id
        for r in filament.catalog.list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    return [
        filament.spools.create(SpoolCreate(material_id=material, grams_left=100.0), actor="t").id
        for _ in range(count)
    ]


def _snapshot() -> BbSnapshot:
    return BbSnapshot(
        instance=INSTANCE,
        full=True,
        spools=[],
        spools_complete=True,
        suppliers=None,
        locations=None,
        catalog=None,
        filaments=None,
        read_at=datetime.now(UTC),
    )


def _request(limit: int = 200) -> dict[str, object]:
    return PlanRequest(snapshot=_snapshot(), limit=limit).model_dump(mode="json")


def _state(store: SettingsStore) -> dict[str, object]:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT * FROM filament_bb_sync_state").fetchone()
    assert row is not None
    return dict(row)


def _attempts(store: SettingsStore) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute("SELECT attempts FROM filament_bb_push_errors").fetchall()
    return [row["attempts"] for row in rows]


def _pass(**over: object) -> SyncPassResult:
    fields: dict[str, object] = {
        "written": 2,
        "failed": 0,
        "deferred": 0,
        "more": False,
        "instance_changed": False,
        "interval_s": 300.0,
    }
    return SyncPassResult.model_validate(fields | over)


@pg
async def test_plan_needs_the_print_credential(store: SettingsStore) -> None:
    """No credential is 401, another caller's is 403; the print worker's plans."""
    _spools(store, 1)
    app = print_crud_app(store, ServiceCredentialStore(store.pool))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as bare:
        for path in (PLAN, RESULTS, PASS):
            assert (await bare.post(path, json={})).status_code == 401, path

    with threaded_print_crud(store, caller="render") as other:
        for path in (PLAN, RESULTS, PASS):
            with pytest.raises(CrudRefusedError) as wrong:
                other.request("POST", path, json={})
            assert wrong.value.status == 403, path
            assert wrong.value.problem["reason"] == "wrong_caller", path

    async with served_print_crud(store) as crud:
        response = await crud.request("POST", PLAN, json=_request())
        assert response.status_code == 200, response.text


@pg
async def test_plan_route_returns_the_plan(store: SettingsStore, crud: CrudClient) -> None:
    (spool,) = _spools(store, 1)
    response = await crud.request("POST", PLAN, json=_request())
    plan = SyncPlan.model_validate(response.json())
    assert [(w.kind, w.op, w.sb_id) for w in plan.writes] == [("spool", "create", spool)]
    assert plan.directions == SPOOLS
    # R-6688-11: nothing unconfirmed, so a worker from before the key takes the plan.
    assert "unconfirmed" not in response.json()
    # The body is what the model would serialize, so the schema and the wire agree.
    assert response.json() == plan.model_dump(mode="json", exclude={"unconfirmed"})
    # The route reads the timer's interval itself: `spool_overlay_refresh_seconds`.
    assert plan.interval_s == float(
        store.read({"spool_overlay_refresh_seconds"})["spool_overlay_refresh_seconds"]
    )


@pg
async def test_the_directions_route_answers_the_stored_directions(
    store: SettingsStore, crud: CrudClient
) -> None:
    """R-T8-2: the pass reads them before Bambuddy; local and remote agree."""
    assert await RemoteFilamentSync(crud).directions() == SPOOLS
    assert await LocalFilamentSync(filament_on(store.pool), store).directions() == SPOOLS
    app = print_crud_app(store, ServiceCredentialStore(store.pool))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as bare:
        assert (await bare.get(DIRECTIONS)).status_code == 401
    with threaded_print_crud(store, caller="render") as other:
        with pytest.raises(CrudRefusedError) as wrong:
            other.request("GET", DIRECTIONS)
        assert wrong.value.status == 403


@pg
async def test_plan_route_honours_the_limit(store: SettingsStore, crud: CrudClient) -> None:
    _spools(store, 3)
    plan = SyncPlan.model_validate((await crud.request("POST", PLAN, json=_request(1))).json())
    assert len(plan.writes) == 1
    assert plan.deferred == 2
    with pytest.raises(CrudRefusedError) as refused:
        await crud.request("POST", PLAN, json=_request() | {"limit": 0})
    assert refused.value.status == 422


@pg
async def test_results_route_links(store: SettingsStore, crud: CrudClient) -> None:
    (spool,) = _spools(store, 1)
    plan = await RemoteFilamentSync(crud).plan(_snapshot(), 200)
    (write,) = plan.writes
    assert isinstance(write.body, dict)
    answer = {"id": 77, **write.body}
    report = await RemoteFilamentSync(crud).results(
        INSTANCE,
        plan.writes,
        [BbWriteResult(key=write.key, ok=True, bb_id=77, answer=answer)],
        plan.pass_id,
    )
    assert report == ResultsReport(linked=1)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT bb_spool_id FROM filament_bb_links WHERE spool_id = %s", (spool,)
        ).fetchone()
    assert row is not None and row["bb_spool_id"] == 77


@pg
async def test_a_list_put_answer_names_its_field(store: SettingsStore, crud: CrudClient) -> None:
    """R-T5-2: the route's models take `{"k_profiles": rows}` as a list PUT's answer."""
    (spool,) = _spools(store, 1)
    write = BbWrite(
        key=f"spool:{spool}:k_profiles",
        kind="spool",
        sb_id=spool,
        op="k_profiles",
        bb_id=77,
        body=[],
        hashes={},
    )
    sent = {
        "instance": INSTANCE,
        "writes": [write.model_dump(mode="json")],
        "results": [
            {"key": write.key, "ok": True, "bb_id": 77, "answer": {"k_profiles": []}},
            {"key": "spool:x:y", "ok": True, "bb_id": 1, "answer": {"suppliers": []}},
        ],
    }
    response = await crud.request("POST", RESULTS, json=sent)
    assert response.status_code == 200, response.text
    ResultsReport.model_validate(response.json())


@pg
async def test_the_pass_records_when_it_started_and_the_status_returns_it(
    store: SettingsStore, crud: CrudClient
) -> None:
    """#6836: ``last_pass_started_at`` is the pass's own start through the pass route, not
    the record's time; without one (a build before it) it is the record's."""
    remote = RemoteFilamentSync(crud)
    started = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)
    await remote.record_pass(_pass(started_at=started))
    state = _state(store)
    assert state["last_pass_started_at"] == started
    assert state["last_pass_at"] > started  # type: ignore[operator]
    status = sync_status(filament_on(store.pool), FilamentSync(), None)
    assert status.last_pass_started_at == started

    await remote.record_pass(_pass())
    assert _state(store)["last_pass_started_at"] > started  # type: ignore[operator]


async def test_the_pass_body_leaves_out_a_missing_start() -> None:
    """#6846: ``SyncPassResult`` is ``extra="forbid"``, so a worker with no start to report
    sends no ``started_at`` key, and an API from before #6836 still takes the pass."""
    sent: list[dict[str, object]] = []

    class Recorder:
        async def request(self, method: str, path: str, **kw: object) -> None:
            sent.append(kw["json"])  # type: ignore[arg-type]

    remote = RemoteFilamentSync(Recorder())  # type: ignore[arg-type]
    await remote.record_pass(_pass())
    assert "started_at" not in sent[0]
    assert sent[0]["error"] is None  # only started_at is left out, not other Nones
    await remote.record_pass(_pass(started_at=datetime(2026, 11, 20, 7, 0, tzinfo=UTC)))
    assert sent[1]["started_at"] == "2026-11-20T07:00:00Z"


async def test_the_sync_bodies_leave_out_an_empty_unconfirmed() -> None:
    """R-6688-11: `SyncPlan` and `ResultsRequest` forbid extra keys, so an empty
    ``unconfirmed`` is left out of both, and a build before it takes the body."""
    sent: list[dict[str, object]] = []

    class Recorder:
        async def request(self, method: str, path: str, **kw: object) -> httpx.Response:
            sent.append(kw["json"])  # type: ignore[arg-type]
            return httpx.Response(200, json={})

    remote = RemoteFilamentSync(Recorder())  # type: ignore[arg-type]
    await remote.results(INSTANCE, [], [], None)
    assert "unconfirmed" not in sent[0]
    held = uuid4()
    await remote.results(INSTANCE, [], [], None, [("location", held)])
    assert sent[1]["unconfirmed"] == [["location", str(held)]]
    plan = SyncPlan(writes=[], deferred=0, directions=FilamentSync(), interval_s=300.0)
    assert "unconfirmed" not in without_empty_unconfirmed(plan)
    named = plan.model_copy(update={"unconfirmed": [("location", held)]})
    assert SyncPlan.model_validate(without_empty_unconfirmed(named)) == named


@pg
async def test_a_failed_pass_keeps_last_ok_at(store: SettingsStore, crud: CrudClient) -> None:
    remote = RemoteFilamentSync(crud)
    await remote.record_pass(_pass())
    first = _state(store)
    assert first["last_ok_at"] is not None and first["last_error"] is None
    assert first["last_pass_at"] is not None
    assert first["counts"] == {
        "written": 2,
        "failed": 0,
        "deferred": 0,
        "conflicts": 0,
        "unmapped_k": 0,
    }

    await remote.record_pass(_pass(written=0, error="timeout"))
    second = _state(store)
    assert second["last_ok_at"] == first["last_ok_at"]
    assert second["last_error"] == "timeout"
    assert second["last_pass_at"] >= first["last_pass_at"]  # type: ignore[operator]

    await remote.record_pass(_pass())
    third = _state(store)
    assert third["last_error"] is None
    assert third["last_ok_at"] >= first["last_ok_at"]  # type: ignore[operator]


@pg
async def test_remote_and_local_give_the_same_plan(store: SettingsStore, crud: CrudClient) -> None:
    """One fixture, both implementations: the API's in-process print queue plans as the
    print worker does through the route."""
    _spools(store, 3)
    snap = _snapshot()
    local = LocalFilamentSync(filament_on(store.pool), store)
    planned = await local.plan(snap, 2)
    # A plan records its creates' intents, which would defer the same spools a second
    # time: clear them so the route plans from the same state.
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM filament_bb_creates")
    remote = await RemoteFilamentSync(crud).plan(snap, 2)
    assert planned.pass_id and remote.pass_id and planned.pass_id != remote.pass_id
    assert planned.model_copy(update={"pass_id": None}) == remote.model_copy(
        update={"pass_id": None}
    )
    assert len(remote.writes) == 2 and remote.deferred == 1
    # Results go the same way: the same refused write answers the same non-empty report
    # either way, and the second does not count as another attempt (R-T6-1).
    refused = [BbWriteResult(key=remote.writes[0].key, ok=False, error="timeout")]
    expected = ResultsReport(failed=1)
    assert await local.results(INSTANCE, remote.writes, refused, remote.pass_id) == expected
    sync = RemoteFilamentSync(crud)
    assert await sync.results(INSTANCE, remote.writes, refused, remote.pass_id) == expected
    assert _attempts(store) == [1]
    await local.record_pass(_pass())
    assert _state(store)["last_error"] is None


@pg
async def test_a_resent_failed_result_is_one_attempt(
    store: SettingsStore, crud: CrudClient
) -> None:
    """R-T6-5: the route has no Idempotency-Key, so a failed result sent again with its
    plan's `pass_id` adds no attempt; another plan's, with the same error and no ageing,
    adds one. The local and remote paths agree."""
    _spools(store, 1)
    remote = RemoteFilamentSync(crud)
    local = LocalFilamentSync(filament_on(store.pool), store)
    first = await remote.plan(_snapshot(), 200)
    (write,) = first.writes
    assert first.pass_id
    refused = [BbWriteResult(key=write.key, ok=False, error="timeout")]
    assert await remote.results(INSTANCE, first.writes, refused, first.pass_id) == (
        ResultsReport(failed=1)
    )
    assert await remote.results(INSTANCE, first.writes, refused, first.pass_id) == (
        ResultsReport(failed=1)
    )
    assert await local.results(INSTANCE, first.writes, refused, first.pass_id) == (
        ResultsReport(failed=1)
    )
    assert _attempts(store) == [1]
    # The next pass plans the same write again (its intent is open: the pass is later).
    second = await local.plan(_snapshot(), 200)
    assert second.pass_id and second.pass_id != first.pass_id
    await local.results(INSTANCE, first.writes, refused, second.pass_id)
    assert _attempts(store) == [2]
    await remote.results(INSTANCE, first.writes, refused, second.pass_id)
    assert _attempts(store) == [2]
    # A result with no pass id is a pass of its own.
    await remote.results(INSTANCE, first.writes, refused, None)
    assert _attempts(store) == [3]


@pg
async def test_a_pass_of_another_bambuddy_plans_and_records_nothing(
    store: SettingsStore, crud: CrudClient
) -> None:
    """#6773: after a reset the state row names no instance, so a pass the old Bambuddy's
    workflow still runs must not create SB's spools there or link them again: a plan or
    results call for another instance than the configured Bambuddy's does nothing, by the
    route and in-process."""
    (spool,) = _spools(store, 1)
    store.save(SettingsPatch(bambuddy_url="https://new-bambuddy.test"))
    local = LocalFilamentSync(filament_on(store.pool), store)
    remote = RemoteFilamentSync(crud)
    for sync in (local, remote):
        planned = await sync.plan(_snapshot(), 200)
        assert planned.instance_changed and planned.writes == []
    with store.pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM filament_bb_creates").fetchone() == {"n": 0}
    write = BbWrite(
        key=f"spool:{spool}:create",
        kind="spool",
        sb_id=spool,
        op="create",
        bb_id=None,
        body={},
        hashes={},
    )
    made = [BbWriteResult(key=write.key, ok=True, bb_id=7, answer={"id": 7})]
    for sync in (local, remote):
        assert await sync.results(INSTANCE, [write], made, None) == ResultsReport()
    with store.pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM filament_bb_links").fetchone() == {"n": 0}
        state = conn.execute("SELECT instance FROM filament_bb_sync_state").fetchone()
    assert state == {"instance": None}


def _intents(store: SettingsStore) -> list[object]:
    with store.pool.connection() as conn:
        return list(conn.execute("SELECT kind, sb_id FROM filament_bb_creates").fetchall())


@pg
async def test_a_call_while_another_runs_waits_for_it(
    store: SettingsStore, crud: CrudClient
) -> None:
    """#6902: while another plan or results call of the instance holds its sync, the route
    answers 503 `sync_busy` and does no work; the worker's client tries again within its
    budget, and its call runs once the first has ended."""
    _spools(store, 1)
    release = threading.Event()
    held = threading.Event()

    def first_call() -> None:
        with store.pool.connection() as conn, conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (SYNC_LOCK + INSTANCE,)
            )
            held.set()
            release.wait(30)

    holder = threading.Thread(target=first_call)
    holder.start()
    held.wait(10)
    try:
        with pytest.raises(CrudUnavailableError) as busy:
            await crud.request("POST", PLAN, json=_request(), budget=0.5)
        assert busy.value.problem.get("reason") == SYNC_BUSY
        assert _intents(store) == []
        threading.Timer(1.0, release.set).start()
        plan = await RemoteFilamentSync(crud).plan(_snapshot(), 200)
    finally:
        release.set()
        holder.join(10)
    assert len(plan.writes) == 1


def _sync_free(store: SettingsStore) -> bool:
    with store.pool.connection() as conn, conn.transaction():
        row = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS held",
            (SYNC_LOCK + INSTANCE,),
        ).fetchone()
    return row is not None and bool(row["held"])


@pg
async def test_a_results_call_the_worker_gave_up_on_rolls_back(
    store: SettingsStore, crud: CrudClient
) -> None:
    """#6902, over a real connection: the worker's read timeout ends a results round that
    waits on a spool link another writer holds. The API notices the disconnect, cancels the
    round's statement and rolls the whole round back, and frees the sync's lock while that
    link is still held, so the round never finished: nothing of it is committed."""
    filament = filament_on(store.pool)
    made = scale.build(store.pool, filament, spools=8, archived=1, events=20)
    planned = plan(filament, made.snap, scale.TWO_WAY, interval_s=300.0)
    results = scale.ok_results(made.snap, planned.writes, datetime.now(UTC))
    body = ResultsRequest(
        instance=INSTANCE, writes=planned.writes, results=results, pass_id="p1"
    ).model_dump(mode="json")
    spool_writes = [w for w in planned.writes if w.kind == "spool"]
    held_spool = spool_writes[len(spool_writes) // 2].sb_id

    def committed() -> object:
        with store.pool.connection() as conn:
            return conn.execute(
                "SELECT spool_id, bb_seen, pushed_fields FROM filament_bb_links ORDER BY spool_id"
            ).fetchall()

    before = committed()
    with store.pool.connection() as conn, conn.transaction():
        # Its link, not the spool: the round locks its spools up front.
        conn.execute(
            "SELECT 1 FROM filament_bb_links WHERE spool_id = %s FOR UPDATE", (held_spool,)
        )
        with pytest.raises(CrudUnavailableError) as gave_up:
            await crud.request("POST", RESULTS, json=body, timeout=1.5, budget=0)
        assert gave_up.value.failure == "transport"
        for _ in range(200):
            if _sync_free(store):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the round still holds the sync after its caller left")
        assert committed() == before
    report = await RemoteFilamentSync(crud).results(
        INSTANCE, planned.writes, results, "p1", planned.unconfirmed
    )
    assert report.updated == len(planned.writes)
