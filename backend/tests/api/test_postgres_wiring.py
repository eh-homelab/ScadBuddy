"""The app wired to Postgres (`SCADBUDDY_DATABASE_URL`), end to end: the bus shares the
projection's listener, a failed start releases what did start, and the analyzer
decisions are kept there. A render through the projection is `test_temporal_path`'s."""

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

from scadbuddy.analyzers.decisions import PostgresDecisionStore
from scadbuddy.core.events import Event, SettingsChanged
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.submit import RenderService


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
            # The projection's listener, shared: one connection per process.
            assert app.state.scadbuddy.projection.pg_listener is bus.listener
            _wait(partial(_connected, bus))
            bus.add_listener(heard[name].append)

        assert first.put("/api/v1/settings", json={"pipeline_id": 3}).status_code == 200

        _wait(lambda: len(changed("one")) >= 1 and len(changed("other")) >= 1)
        time.sleep(0.5)  # a duplicate would have arrived by now
        assert len(changed("one")) == 1
        assert len(changed("other")) == 1
        assert changed("one")[0].id == changed("other")[0].id


@pytest.mark.requires_postgres
def test_a_bus_that_fails_to_start_releases_the_render_service_that_did(
    settings: Settings, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bus starts after the render service and before the lifespan's `try`, whose
    `finally` a failed start never reaches: the lifespan must close both itself."""
    app = create_app(settings.model_copy(update={"database_url": pg_conninfo}))
    state = app.state.scadbuddy
    bus, service, projection = state.events, state.queue, state.projection
    assert isinstance(bus, PgNotifyEventBus)
    assert isinstance(service, RenderService)
    assert isinstance(projection, JobProjection)
    started: list[asyncio.Task[None]] = []

    async def unreachable(*args: object, **kwargs: object) -> None:
        assert service._reconciler is not None  # the service is fully up by now
        started.append(service._reconciler)
        raise PoolTimeout("the database refused a second pool")

    monkeypatch.setattr(bus._pool, "open", unreachable)
    with pytest.raises(PoolTimeout), TestClient(app):
        pass

    assert started, "the render service had started before the bus failed"
    assert all(task.done() for task in started)
    assert service._reconciler is None
    assert projection.pool.closed
    assert bus._pool.closed
    assert projection.pg_listener.backend_pid is None


@pytest.mark.requires_postgres
def test_analyzer_decisions_are_kept_in_postgres(settings: Settings, pg_conninfo: str) -> None:
    app = create_app(settings.model_copy(update={"database_url": pg_conninfo}))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/analyzers/decisions",
            json={"diagnostic_id": "SB1003", "kind": "ignore", "scope": {"kind": "global"}},
        )
        assert created.status_code == 201, created.text
        listed = client.get("/api/v1/analyzers/decisions").json()

    assert isinstance(app.state.scadbuddy.decisions, PostgresDecisionStore)
    assert [row["id"] for row in listed] == [created.json()["id"]]
    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute("SELECT kind FROM analyzer_decisions").fetchone()
    assert row is not None and row[0] == "ignore"
