"""`GET /filament/sync/status` and `POST /filament/sync/run` (#6265 Task 10, plan
Decision 19): the sync's state for a person and the agent, and "Sync now"."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.filament.bb_sync_nudge import FilamentSyncNudger
from tests.api.test_filament import _spool
from tests.api.test_filament_purchasing import _supplier
from tests.api.test_send import configure

pytestmark = pytest.mark.requires_postgres

SYNC = "/api/v1/filament/sync"


#: The real nudge, kept before `filament_nudges` (autouse) silences it.
REAL_NUDGE = FilamentSyncNudger.nudge


class FakeTemporal:
    """The Temporal client the nudger gets: it records each start's signal args, or
    raises ``fail``."""

    def __init__(self) -> None:
        self.starts: list[list[Any]] = []
        self.fail: BaseException | None = None

    async def start_workflow(self, *args: Any, **kwargs: Any) -> None:
        if self.fail is not None:
            raise self.fail
        self.starts.append(kwargs["start_signal_args"])


@pytest.fixture
def temporal(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> FakeTemporal:
    """The app's real `FilamentSyncNudger.nudge`, over a fake Temporal client."""
    fake = FakeTemporal()
    monkeypatch.setattr(FilamentSyncNudger, "nudge", REAL_NUDGE)
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    monkeypatch.setattr(state.filament_sync, "client_getter", lambda: fake)
    return fake


def test_status_lists_errors_and_directions(client: TestClient, pg_conninfo: str) -> None:
    configure(client, filament_sync={"tags": "two_way", "suppliers": "sb_to_bb"})
    older, newer = uuid4(), uuid4()
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "UPDATE filament_bb_sync_state SET instance = 'abcdef012345',"
            " last_pass_at = %s, last_pass_started_at = %s, last_ok_at = %s,"
            " last_error = 'bambuddy-down', counts = %s",
            (
                datetime(2026, 11, 20, 10, tzinfo=UTC),
                datetime(2026, 11, 20, 9, 59, tzinfo=UTC),
                datetime(2026, 11, 20, 9, tzinfo=UTC),
                Jsonb({"written": 2, "failed": 1, "deferred": 0, "conflicts": 0, "unmapped_k": 0}),
            ),
        )
        conn.execute(
            "INSERT INTO filament_bb_push_errors (kind, sb_id, error, attempts, at)"
            " VALUES ('spool', %s, 'http-500', 3, %s), ('supplier', %s, 'name-taken', 1, %s)",
            (
                older,
                datetime(2026, 11, 20, 8, tzinfo=UTC),
                newer,
                datetime(2026, 11, 20, 10, tzinfo=UTC),
            ),
        )

    got = client.get(f"{SYNC}/status")

    assert got.status_code == 200, got.text
    body = got.json()
    assert body["instance"] == "abcdef012345"
    assert body["last_error"] == "bambuddy-down"
    assert body["last_pass_at"].startswith("2026-11-20T10:00")
    assert body["last_pass_started_at"].startswith("2026-11-20T09:59")
    assert body["last_ok_at"].startswith("2026-11-20T09:00")
    assert body["counts"] == {
        "written": 2,
        "failed": 1,
        "deferred": 0,
        "conflicts": 0,
        "unmapped_k": 0,
    }
    assert [(e["kind"], e["sb_id"], e["error"], e["attempts"]) for e in body["errors"]] == [
        ("supplier", str(newer), "name-taken", 1),
        ("spool", str(older), "http-500", 3),
    ]
    assert body["directions"] == {
        "spools": "bb_to_sb",
        "weights": "bb_to_sb",
        "tags": "two_way",
        "presets": "bb_to_sb",
        "suppliers": "sb_to_bb",
        "locations": "off",
    }
    assert body["skipped"] is None


def test_status_labels_each_error_row(client: TestClient, pg_conninfo: str) -> None:
    """R-T11-1: each error row names its item as the UI shows it, a spool by its `#N`, a
    supplier by its name; a row whose item is gone has no label."""
    spool = _spool(client)
    supplier = _supplier(client, "Micro Center")
    gone = uuid4()
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "INSERT INTO filament_bb_push_errors (kind, sb_id, error, attempts, at)"
            " VALUES ('spool', %s, 'http-500', 1, %s), ('supplier', %s, 'name_taken', 1, %s),"
            " ('location', %s, 'http-500', 1, %s)",
            (
                spool["id"],
                datetime(2026, 11, 20, 10, tzinfo=UTC),
                supplier,
                datetime(2026, 11, 20, 9, tzinfo=UTC),
                gone,
                datetime(2026, 11, 20, 8, tzinfo=UTC),
            ),
        )

    got = client.get(f"{SYNC}/status")

    assert got.status_code == 200, got.text
    assert [(e["kind"], e["label"]) for e in got.json()["errors"]] == [
        ("spool", f"#{spool['number']}"),
        ("supplier", "Micro Center"),
        ("location", None),
    ]


def test_status_says_why_no_pass_runs(client: TestClient) -> None:
    """R-T8-2: the nudge starts nothing with no Bambuddy, or when nothing pushes; the
    status names which, in the pass's own ``skipped`` words."""
    fresh = client.get(f"{SYNC}/status").json()
    assert fresh["skipped"] == "no_bambuddy"
    assert fresh["instance"] is None
    assert fresh["last_pass_at"] is None
    assert fresh["errors"] == []

    configure(client)
    assert client.get(f"{SYNC}/status").json()["skipped"] == "nothing_pushes"

    # The engine never pushes tags (Decision 6): tags alone is still nothing.
    configure(client, filament_sync={"tags": "two_way"})
    assert client.get(f"{SYNC}/status").json()["skipped"] == "nothing_pushes"

    configure(client, filament_sync={"locations": "two_way"})
    assert client.get(f"{SYNC}/status").json()["skipped"] is None


def test_run_nudges_full(
    client: TestClient, filament_nudges: list[bool], temporal: FakeTemporal
) -> None:
    configure(client, filament_sync={"spools": "two_way"})
    filament_nudges.clear()

    before = datetime.now(UTC)
    ran = client.post(f"{SYNC}/run")
    after = datetime.now(UTC)

    assert ran.status_code == 202, ran.text
    body = ran.json()
    assert body["started"] is True
    # #6836: the server's own time, taken before it asked for the pass.
    assert before <= datetime.fromisoformat(body["requested_at"]) <= after
    assert temporal.starts == [[True]]
    # The route nudges itself: `nudge_after_write` adds no plain nudge after it.
    assert filament_nudges == []


def test_run_that_cannot_reach_temporal_is_503(
    client: TestClient, filament_nudges: list[bool], temporal: FakeTemporal
) -> None:
    """R-T10-2: no 202 when nothing started; the detail is the label, never the message."""
    configure(client, filament_sync={"spools": "two_way"})
    filament_nudges.clear()
    temporal.fail = RuntimeError("connection refused to 10.0.0.9:7233")

    ran = client.post(f"{SYNC}/run")

    assert ran.status_code == 503, ran.text
    assert ran.json()["detail"] == "could not ask the sync workflow for a pass: internal"
    assert "10.0.0.9" not in ran.text
    assert filament_nudges == []


def test_run_without_bambuddy_is_409(
    client: TestClient, filament_nudges: list[bool], temporal: FakeTemporal
) -> None:
    ran = client.post(f"{SYNC}/run")

    assert ran.status_code == 409, ran.text
    assert ran.json()["code"] == "no_bambuddy"
    assert temporal.starts == []
    assert filament_nudges == []


@pytest.mark.parametrize("sync", [{}, {"tags": "two_way"}], ids=["defaults", "tags-only"])
def test_run_with_nothing_pushing_is_409(
    client: TestClient,
    filament_nudges: list[bool],
    temporal: FakeTemporal,
    sync: dict[str, str],
) -> None:
    configure(client, filament_sync=sync)
    filament_nudges.clear()

    ran = client.post(f"{SYNC}/run")

    assert ran.status_code == 409, ran.text
    assert ran.json()["code"] == "nothing_pushes"
    assert temporal.starts == []
    assert filament_nudges == []
