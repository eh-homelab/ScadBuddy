"""The API's first copy of the templates' files to rows (#7317 phase 3, plan D4): when no
copy run has finished, the start copies before the app serves; when another replica
holds the copy lock, it waits for that replica's run, then serves. The background start
copy stays off here (`app_template_copy_off`): only `_first_template_copy` copies."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import scadbuddy.main as scadbuddy_main
from scadbuddy.api.deps import AppState
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.template_copy import COPY_LOCK, copy_templates
from scadbuddy.library.template_rows import TemplateRows
from tests.conftest import PgPool

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

#: Written on the volume before the start, with no row: only a copy writes their rows.
PLACED = ("placed-one", "placed-two")


@pytest.fixture
def placed(settings: Settings, seed_dir: Path) -> None:
    bundled = seed_dir / "bundled"
    bundled.mkdir()
    (bundled / "model.scad").write_text("cube(10);\n", encoding="utf-8")
    models = DataPaths(settings.data_dir).models
    for slug in PLACED:
        (models / slug).mkdir(parents=True)
        (models / slug / "model.scad").write_text("cube(10);\n", encoding="utf-8")


def _finished_runs(conninfo: str) -> int:
    with psycopg.connect(conninfo) as conn:
        found = conn.execute(
            "SELECT count(*) FROM template_copy_runs WHERE finished_at IS NOT NULL"
        ).fetchone()
    assert found is not None
    count: int = found[0]
    return count


def _insert_finished_run(conninfo: str) -> None:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("INSERT INTO template_copy_runs (finished_at) VALUES (clock_timestamp())")


def _state_on(pool: PgPool) -> AppState:
    """What `_first_template_copy` reads of the app's state, on ``pool``: a run that does
    not get the copy lock touches nothing else."""
    rows = TemplateRows(pool)
    state = SimpleNamespace(
        catalogue=SimpleNamespace(refresh=SimpleNamespace(rows=rows)),
        projection=SimpleNamespace(pool=pool),
    )
    return cast(AppState, state)


@pytest.fixture
def copy_lock(pg_conninfo: str) -> Iterator[None]:
    """The copy lock, held by "another replica" (a session of its own)."""
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (COPY_LOCK,))
        yield


def test_a_start_with_no_finished_run_copies_before_serving(
    app: FastAPI, placed: None, pg_conninfo: str
) -> None:
    assert _finished_runs(pg_conninfo) == 0
    with TestClient(app) as client:
        listed = {model["slug"] for model in client.get("/api/v1/models").json()}
        assert {*PLACED, "builtin:bundled"} <= listed
        assert _finished_runs(pg_conninfo) == 1


def test_a_start_after_a_finished_run_does_not_copy_first(
    app: FastAPI, placed: None, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_finished_run(pg_conninfo)
    calls: list[object] = []
    copy = copy_templates

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return copy(*args, **kwargs)

    monkeypatch.setattr(scadbuddy_main, "copy_templates", counted)
    with TestClient(app) as client:
        assert client.get("/api/v1/models").status_code == 200
    assert calls == []


def test_first_copy_waits_for_another_replicas_run(
    pg_pool: PgPool, pg_conninfo: str, copy_lock: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scadbuddy_main, "FIRST_COPY_POLL", 0.05)

    async def scenario() -> None:
        task = asyncio.create_task(
            scadbuddy_main._first_template_copy(_state_on(pg_pool), threading.Event())
        )
        await asyncio.sleep(0.3)
        assert not task.done()
        await asyncio.to_thread(_insert_finished_run, pg_conninfo)
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


def test_first_copy_gives_up_after_the_wait(
    pg_pool: PgPool,
    copy_lock: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(scadbuddy_main, "FIRST_COPY_POLL", 0.05)
    monkeypatch.setattr(scadbuddy_main, "FIRST_COPY_WAIT", 0.2)
    with caplog.at_level(logging.ERROR, logger=scadbuddy_main.logger.name):
        asyncio.run(
            asyncio.wait_for(
                scadbuddy_main._first_template_copy(_state_on(pg_pool), threading.Event()), 5
            )
        )
    assert any(
        record.levelno == logging.ERROR and "first copy" in record.getMessage()
        for record in caplog.records
    )


def test_a_failed_first_copy_still_serves(
    app: FastAPI, placed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError("the volume went away")

    monkeypatch.setattr(scadbuddy_main, "copy_templates", broken)
    with TestClient(app) as client:
        assert client.get("/api/v1/models").status_code == 200
