"""`/store/migrate`: one `StoreMigration` per pair of backends, and its progress."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Collection
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.store import migrate
from scadbuddy.store.index import BlobIndex
from scadbuddy.store.migrate import MigrationBatch
from tests.support.store import store_pool

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]


class _Backend:
    def __init__(self, name: str) -> None:
        self.backend = name

    async def aclose(self) -> None:
        pass


def _until(check: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.1)


def test_post_starts_once_and_get_reports_progress(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, int, list[str]]] = []
    release = threading.Event()

    def build(name: str, *_: Any) -> _Backend:
        return _Backend(name)

    async def batch(
        _index: Any,
        source: _Backend,
        target: _Backend,
        *,
        limit: int,
        heartbeat: Callable[[str], None],
        skip: Collection[str] = (),
    ) -> MigrationBatch:
        calls.append((source.backend, target.backend, limit, sorted(skip)))
        if len(calls) == 1:
            return MigrationBatch(moved=2, remaining=3, failed=["k"])
        while not release.is_set():
            heartbeat("waiting")
            await asyncio.sleep(0.05)
        return MigrationBatch(moved=3, remaining=0, failed=[])

    monkeypatch.setattr(migrate, "build_backend", build)
    monkeypatch.setattr(migrate, "migrate_batch", batch)

    assert client.get("/api/v1/store/migrate").status_code == 404
    body = {"source": "bambuddy", "target": "s3", "batch": 7}
    first = client.post("/api/v1/store/migrate", json=body)
    assert first.status_code == 202, first.text
    assert first.json() == {"workflow_id": "store-migrate-bambuddy-s3"}
    again = client.post("/api/v1/store/migrate", json=body)
    assert again.status_code == 202
    assert again.json() == first.json()

    def status() -> dict[str, Any]:
        answer = client.get("/api/v1/store/migrate", params={"source": "bambuddy", "target": "s3"})
        assert answer.status_code == 200, answer.text
        body: dict[str, Any] = answer.json()
        return body

    _until(lambda: status()["failed"] == ["k"])
    # The counts are the table's: no row is on Bambuddy here.
    assert status() == {
        "source": "bambuddy",
        "target": "s3",
        "remaining": 0,
        "copied": 0,
        "failed": ["k"],
        "running": True,
    }
    release.set()
    _until(lambda: not status()["running"])
    # Closed, its result still names what it could not copy.
    assert status()["failed"] == ["k"]
    assert (
        client.get(
            "/api/v1/store/migrate", params={"source": "s3", "target": "bambuddy"}
        ).status_code
        == 404
    )
    # One execution: the second POST joined the first, which skipped what failed.
    assert calls == [("bambuddy", "s3", 7, []), ("bambuddy", "s3", 7, ["k"])]


@pytest.mark.parametrize(
    "body",
    [
        {"source": "s3", "target": "s3"},
        {"source": "local", "target": "s3"},
        {"source": "bambuddy", "target": "local"},
    ],
)
def test_a_migration_needs_two_remote_backends(client: TestClient, body: dict[str, str]) -> None:
    assert client.post("/api/v1/store/migrate", json=body).status_code == 422
    assert client.get("/api/v1/store/migrate", params=body).status_code == 422


def test_a_start_switches_the_rows_copied_to_its_backend(
    settings: Settings, pg_conninfo: str
) -> None:
    with store_pool(pg_conninfo) as pool, pool.connection() as conn:
        conn.execute(
            "INSERT INTO store_blobs (key, sha256, kind, backend, backend_id, size,"
            " migrated_backend, migrated_backend_id) VALUES"
            " ('copied', 'a', 'piece', 'bambuddy', '7', 1, 'local', 'piece/a'),"
            " ('elsewhere', 'b', 'piece', 'bambuddy', '8', 1, 's3', 't/piece/b')"
        )
    with TestClient(create_app(settings)), store_pool(pg_conninfo) as pool:
        index = BlobIndex(pool)
        copied, elsewhere = index.get("copied"), index.get("elsewhere")
    assert copied is not None and elsewhere is not None
    assert (copied.ref.backend, copied.ref.backend_id) == ("local", "piece/a")
    assert (elsewhere.ref.backend, elsewhere.ref.backend_id) == ("bambuddy", "8")
