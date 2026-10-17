"""`/store/migrate`: one `StoreMigration` per pair of backends, and its progress."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Collection
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.store import migrate
from scadbuddy.store.migrate import MigrationBatch

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

    def progressed() -> bool:
        answer = client.get("/api/v1/store/migrate")
        return answer.status_code == 200 and bool(answer.json()["moved"] == 2)

    _until(progressed)
    assert client.get("/api/v1/store/migrate").json() == {
        "moved": 2,
        "remaining": 3,
        "failed": ["k"],
    }
    release.set()
    _until(lambda: client.get("/api/v1/store/migrate").status_code == 404)
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
