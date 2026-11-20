"""`GET /filament/sync/status` and `POST /filament/sync/run` (#6265 Task 10, plan
Decision 19): the sync's state for a person and the agent, and "Sync now"."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from scadbuddy.filament.bb_sync_nudge import FilamentSyncNudger
from tests.api.test_send import configure

pytestmark = pytest.mark.requires_postgres

SYNC = "/api/v1/filament/sync"


@pytest.fixture
def full_nudges(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Each awaited `FilamentSyncNudger.nudge`'s ``full``; `filament_nudges` silences it."""
    nudges: list[bool] = []

    async def nudge(self: FilamentSyncNudger, *, full: bool = False) -> None:
        nudges.append(full)

    monkeypatch.setattr(FilamentSyncNudger, "nudge", nudge)
    return nudges


def test_status_lists_errors_and_directions(client: TestClient, pg_conninfo: str) -> None:
    configure(client, filament_sync={"tags": "two_way", "suppliers": "sb_to_bb"})
    older, newer = uuid4(), uuid4()
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "UPDATE filament_bb_sync_state SET instance = 'abcdef012345',"
            " last_pass_at = %s, last_ok_at = %s, last_error = 'bambuddy-down', counts = %s",
            (
                datetime(2026, 11, 20, 10, tzinfo=UTC),
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

    configure(client, filament_sync={"locations": "two_way"})
    assert client.get(f"{SYNC}/status").json()["skipped"] is None


def test_run_nudges_full(
    client: TestClient, filament_nudges: list[bool], full_nudges: list[bool]
) -> None:
    configure(client, filament_sync={"spools": "two_way"})
    filament_nudges.clear()
    full_nudges.clear()

    ran = client.post(f"{SYNC}/run")

    assert ran.status_code == 202, ran.text
    assert ran.json() == {"started": True}
    assert full_nudges == [True]
    # The route nudges itself: `nudge_after_write` adds no plain nudge after it.
    assert filament_nudges == []


def test_run_without_bambuddy_is_409(
    client: TestClient, filament_nudges: list[bool], full_nudges: list[bool]
) -> None:
    ran = client.post(f"{SYNC}/run")

    assert ran.status_code == 409, ran.text
    assert ran.json()["code"] == "no_bambuddy"
    assert full_nudges == []
    assert filament_nudges == []


def test_run_with_nothing_pushing_is_409(
    client: TestClient, filament_nudges: list[bool], full_nudges: list[bool]
) -> None:
    configure(client)
    filament_nudges.clear()

    ran = client.post(f"{SYNC}/run")

    assert ran.status_code == 409, ran.text
    assert ran.json()["code"] == "nothing_pushes"
    assert full_nudges == []
    assert filament_nudges == []
