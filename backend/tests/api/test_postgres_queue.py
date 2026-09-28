"""The app wired to Postgres (`SCADBUDDY_DATABASE_URL`), end to end: the lifespan
opens the store and migrates, renders are recorded in the table, `/metrics` reads
the queue from it."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

from scadbuddy.core.events import Event, InProcessEventBus, SettingsChanged
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.render.pg_store import PostgresJobStore
from tests.api.conftest import wait_for_job


@pytest.mark.requires_postgres
def test_the_app_queues_renders_in_postgres(
    settings: Settings, model: str, pg_conninfo: str
) -> None:
    app = create_app(settings.model_copy(update={"database_url": pg_conninfo}))
    with TestClient(app) as client:
        accepted = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        # The fake openscad may or may not produce geometry; either way the job
        # settles, and it is Postgres that says so.
        settled = wait_for_job(client, job_id)
        metrics = client.get("/metrics").text

    assert isinstance(app.state.scadbuddy.queue.store, PostgresJobStore)
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT state FROM render_jobs WHERE id = %s", (job_id,)).fetchone()
    assert row is not None and row[0] == settled["status"]
    assert "scadbuddy_render_queue_depth 0.0" in metrics


def test_without_a_database_url_the_queue_uses_files(settings: Settings) -> None:
    app = create_app(settings)
    assert not isinstance(app.state.scadbuddy.queue.store, PostgresJobStore)


def _wait(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def _connected(bus: PgNotifyEventBus) -> bool:
    return bus.listener.backend_pid is not None


@pytest.mark.requires_postgres
def test_two_replicas_each_hear_every_event_once(
    settings: Settings, pg_conninfo: str, tmp_path: Path
) -> None:
    """Spec §7 with replicas: a change on one app reaches the subscribers of both,
    once each, over the one LISTEN connection each process already holds."""
    one = create_app(settings.model_copy(update={"database_url": pg_conninfo}))
    other = create_app(
        settings.model_copy(update={"database_url": pg_conninfo, "data_dir": tmp_path / "other"})
    )
    heard: dict[str, list[Event]] = {"one": [], "other": []}

    def changed(name: str) -> list[Event]:
        return [e for e in heard[name] if isinstance(e, SettingsChanged)]

    with TestClient(one) as first, TestClient(other):
        for name, app in (("one", one), ("other", other)):
            bus = app.state.scadbuddy.events
            assert isinstance(bus, PgNotifyEventBus)
            # The render queue's listener, shared: one connection per process.
            assert app.state.scadbuddy.queue.listener is bus.listener
            _wait(partial(_connected, bus))
            bus.add_listener(heard[name].append)

        assert first.put("/api/v1/settings", json={"pipeline_id": 3}).status_code == 200

        _wait(lambda: len(changed("one")) >= 1 and len(changed("other")) >= 1)
        time.sleep(0.5)  # a duplicate would have arrived by now
        assert len(changed("one")) == 1
        assert len(changed("other")) == 1
        assert changed("one")[0].id == changed("other")[0].id


def test_without_a_database_url_events_stay_in_process(settings: Settings) -> None:
    app = create_app(settings)
    assert isinstance(app.state.scadbuddy.events, InProcessEventBus)
    heard: list[Event] = []
    app.state.scadbuddy.events.add_listener(heard.append)
    with TestClient(app) as client:
        assert client.put("/api/v1/settings", json={"pipeline_id": 3}).status_code == 200
    assert [e.kind for e in heard if isinstance(e, SettingsChanged)] == ["settings.changed"]


@pytest.mark.requires_postgres
def test_a_bus_that_fails_to_start_releases_the_queue_that_did(
    settings: Settings, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bus starts after the queue and before the lifespan's `try`, whose
    `finally` a failed start never reaches: the lifespan must close both itself."""
    app = create_app(settings.model_copy(update={"database_url": pg_conninfo}))
    state = app.state.scadbuddy
    bus, queue = state.events, state.queue
    assert isinstance(bus, PgNotifyEventBus)
    store = queue.store
    assert isinstance(store, PostgresJobStore)
    started: list[asyncio.Task[None]] = []

    async def unreachable(*args: object, **kwargs: object) -> None:
        started.extend(queue._tasks)  # the queue is fully up by now
        raise PoolTimeout("the database refused a second pool")

    monkeypatch.setattr(bus._pool, "open", unreachable)
    with pytest.raises(PoolTimeout), TestClient(app):
        pass

    assert started, "the queue had started before the bus failed"
    assert all(task.done() for task in started)
    assert queue._tasks == []
    assert store._pool.closed
    assert bus._pool.closed
    assert store.pg_listener.backend_pid is None
