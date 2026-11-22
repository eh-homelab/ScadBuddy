"""#6905: the Bambuddy sync's pass history. `record_pass` adds a row for every pass in the
transaction of the state row, `GET /filament/sync/passes` pages them newest first, the
prune deletes rows older than the setting `filament_sync_pass_ttl_days`, and the setting
round-trips through `PUT /settings`. Invented ids only."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy import main
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.filament.bb_sync_models import SyncPassResult
from scadbuddy.filament.bb_sync_results import record_pass
from scadbuddy.filament.component import FILAMENT, Filament

pytestmark = pytest.mark.requires_postgres

PASSES = "/api/v1/filament/sync/passes"
INSTANCE = "0123456789ab"


def _filament(client: TestClient) -> Filament:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state.components.get(FILAMENT)


def _pass(**over: Any) -> SyncPassResult:
    fields: dict[str, Any] = {
        "written": 2,
        "failed": 1,
        "deferred": 3,
        "conflicts": 4,
        "unmapped_k": 5,
        "more": True,
        "instance_changed": False,
        "interval_s": 300.0,
        "started_at": datetime(2026, 11, 21, 22, 28, tzinfo=UTC),
        "pass_id": uuid4(),
        "instance": INSTANCE,
        "trigger": "timer",
    }
    return SyncPassResult.model_validate(fields | over)


def _rows(pg_conninfo: str) -> list[dict[str, Any]]:
    with psycopg.connect(pg_conninfo, row_factory=psycopg.rows.dict_row) as conn:
        return conn.execute("SELECT * FROM filament_bb_sync_passes ORDER BY ended_at").fetchall()


def _state(pg_conninfo: str) -> dict[str, Any]:
    with psycopg.connect(pg_conninfo, row_factory=psycopg.rows.dict_row) as conn:
        row = conn.execute("SELECT * FROM filament_bb_sync_state").fetchone()
    assert row is not None
    return row


def _age(pg_conninfo: str, pass_id: UUID, days: float) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "UPDATE filament_bb_sync_passes SET ended_at = now() - make_interval(secs => %s)"
            " WHERE id = %s",
            (days * 86400, pass_id),
        )


def test_each_pass_is_a_row_beside_the_state(client: TestClient, pg_conninfo: str) -> None:
    filament = _filament(client)
    ok = _pass()
    skipped = _pass(written=0, failed=0, skipped="nothing_pushes", trigger="change")
    bambuddy_down = _pass(error="https://scadbuddy.dev/problems/bambuddy-unreachable")
    crud_down = _pass(error="crud_unavailable:transport", trigger="full")
    for result in (ok, skipped, bambuddy_down, crud_down):
        record_pass(filament, result)

    rows = _rows(pg_conninfo)
    assert [row["id"] for row in rows] == [
        ok.pass_id,
        skipped.pass_id,
        bambuddy_down.pass_id,
        crud_down.pass_id,
    ]
    first = rows[0]
    assert first["instance"] == INSTANCE and first["trigger"] == "timer"
    assert first["started_at"] == ok.started_at and first["ended_at"] > ok.started_at
    assert (
        first["written"],
        first["failed"],
        first["deferred"],
        first["conflicts"],
        first["unmapped_k"],
    ) == (2, 1, 3, 4, 5)
    assert first["error"] is None and first["skipped"] is None
    assert rows[1]["skipped"] == "nothing_pushes"
    assert rows[3]["error"] == "crud_unavailable:transport"
    # The state row is the last pass's, as before.
    state = _state(pg_conninfo)
    assert state["last_error"] == "crud_unavailable:transport"


def test_a_resent_pass_adds_no_row(client: TestClient, pg_conninfo: str) -> None:
    """The CRUD client sends a record again after a lost answer: the id keeps it one."""
    filament = _filament(client)
    result = _pass()
    record_pass(filament, result)
    record_pass(filament, result)
    assert len(_rows(pg_conninfo)) == 1
    # An older worker's result has no id: each record is a row of its own.
    record_pass(filament, _pass(pass_id=None, instance=None, trigger=None, started_at=None))
    rows = _rows(pg_conninfo)
    assert len(rows) == 2 and rows[1]["trigger"] is None and rows[1]["started_at"] is not None


def test_an_instance_changed_pass_leaves_the_state_alone(
    client: TestClient, pg_conninfo: str
) -> None:
    filament = _filament(client)
    record_pass(filament, _pass(error=None))
    before = _state(pg_conninfo)
    record_pass(filament, _pass(written=0, instance_changed=True, instance="fedcba987654"))
    assert _state(pg_conninfo) == before
    rows = _rows(pg_conninfo)
    assert [row["instance_changed"] for row in rows] == [False, True]


def test_the_route_pages_newest_first(client: TestClient) -> None:
    filament = _filament(client)
    made = [_pass(written=n) for n in range(5)]
    for result in made:
        record_pass(filament, result)

    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        got = client.get(PASSES, params=params)
        assert got.status_code == 200, got.text
        body = got.json()
        seen.extend(body["passes"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert pages == 3
    assert [row["written"] for row in seen] == [4, 3, 2, 1, 0]
    assert len({row["id"] for row in seen}) == 5
    newest = seen[0]
    assert newest["trigger"] == "timer" and newest["instance"] == INSTANCE
    assert set(newest) == {
        "id",
        "instance",
        "trigger",
        "started_at",
        "ended_at",
        "written",
        "failed",
        "deferred",
        "conflicts",
        "unmapped_k",
        "skipped",
        "instance_changed",
        "error",
    }


def test_the_route_refuses_a_cursor_no_page_gave(client: TestClient) -> None:
    assert client.get(PASSES, params={"cursor": "zz"}).status_code == 422
    assert client.get(PASSES, params={"cursor": "6869"}).status_code == 422
    assert client.get(PASSES, params={"limit": 0}).status_code == 422
    assert client.get(PASSES, params={"limit": 201}).status_code == 422
    empty = client.get(PASSES)
    assert empty.status_code == 200 and empty.json() == {"passes": [], "next_cursor": None}


@pytest.fixture
def no_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The housekeeping workflow's activity sweep prunes these rows too, and it can run in
    the middle of a test (#6962, #6966). Its activity calls `main._sweep_activity_logged`
    by name at each run, so this no-op, set before `client` builds the app, keeps every
    sweep out of the test."""

    async def skip(state: AppState) -> None:
        return None

    monkeypatch.setattr(main, "_sweep_activity_logged", skip)


def test_the_prune_keeps_the_settings_days(
    no_sweep: None, client: TestClient, pg_conninfo: str
) -> None:
    filament = _filament(client)
    fresh, ten, forty = _pass(), _pass(), _pass()
    for result in (fresh, ten, forty):
        record_pass(filament, result)
    _age(pg_conninfo, ten.pass_id, 10)  # type: ignore[arg-type]
    _age(pg_conninfo, forty.pass_id, 40)  # type: ignore[arg-type]

    # The default is 30 days.
    assert filament.sync_passes.prune(None) == 1
    assert {row["id"] for row in _rows(pg_conninfo)} == {fresh.pass_id, ten.pass_id}
    assert filament.sync_passes.prune(5) == 1
    assert [row["id"] for row in _rows(pg_conninfo)] == [fresh.pass_id]


def test_the_prune_is_bounded(no_sweep: None, client: TestClient, pg_conninfo: str) -> None:
    filament = _filament(client)
    old = [_pass() for _ in range(3)]
    for result in old:
        record_pass(filament, result)
        _age(pg_conninfo, result.pass_id, 31)  # type: ignore[arg-type]
    assert filament.sync_passes.prune(None, batch=2) == 2
    assert filament.sync_passes.prune(None, batch=2) == 1
    assert _rows(pg_conninfo) == []


def test_the_ttl_setting_round_trips(client: TestClient) -> None:
    """Stored only, empty by default (30 days), at least one day."""
    name = "filament_sync_pass_ttl_days"
    body = client.get("/api/v1/settings").json()
    assert body[name] is None
    assert name not in body["sources"]
    assert body["docs"][name]["title"] == "Keep sync history for (days)"
    assert client.put("/api/v1/settings", json={name: 7}).status_code == 200
    assert client.get("/api/v1/settings").json()[name] == 7
    assert client.put("/api/v1/settings", json={name: 0}).status_code == 422
    assert client.put("/api/v1/settings", json={name: 3651}).status_code == 422
    assert client.put("/api/v1/settings", json={name: None}).status_code == 200
    assert client.get("/api/v1/settings").json()[name] is None
