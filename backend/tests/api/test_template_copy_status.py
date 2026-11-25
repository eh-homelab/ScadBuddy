"""`GET /model-records/copy-status` (#7317): the last run of the copy of the templates'
files to rows, at an API start or in a housekeeping sweep. The tests' apps skip the
start's background copy (`app_template_copy_off`) unless a test is marked
`template_copy`; the first copy before the app serves still runs (phase 3)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.template_rows import TemplateRows

STATUS = "/api/v1/model-records/copy-status"

#: Templates written on the volume before the app starts, with no row: a model that an
#: older build saved in the files only.
PLACED = ("placed-one", "placed-two")


def _rows(client: TestClient) -> TemplateRows:
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    refresh = state.catalogue.refresh
    assert refresh is not None
    rows: TemplateRows = refresh.rows
    return rows


@pytest.fixture
def placed_client(settings: Settings, app: FastAPI) -> Iterator[TestClient]:
    models = DataPaths(settings.data_dir).models
    for slug in PLACED:
        (models / slug).mkdir(parents=True)
        (models / slug / "model.scad").write_text("cube(10);\n")
    with TestClient(app) as test_client:
        yield test_client


def _finished(client: TestClient) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = client.get(STATUS)
        if response.status_code == 200 and response.json()["finished_at"] is not None:
            body: dict[str, Any] = response.json()
            return body
        time.sleep(0.1)
    raise AssertionError(f"no finished copy run: {client.get(STATUS).json()}")


def test_copy_status_reports_the_last_run(placed_client: TestClient) -> None:
    """The run is the start's first copy (phase 3, `main._first_template_copy`): the
    database had no finished run, and the background copy is off."""
    body = _finished(placed_client)

    assert datetime.fromisoformat(body["started_at"]) <= datetime.fromisoformat(body["finished_at"])
    assert body["incomplete"] is False
    # The boot's built-in sync wrote the built-ins' rows in its HEAD transaction, so the
    # copy adds the placed templates only.
    assert body["added"] == len(PLACED)
    assert (body["refreshed"], body["dropped"]) == (0, 0)
    assert body["unreadable"] == []
    assert "id" not in body
    rows = _rows(placed_client)
    assert set(PLACED) <= set(rows.ids())


def test_copy_status_reports_an_incomplete_run_and_its_unreadable(client: TestClient) -> None:
    """What the run table holds, as it holds it: no end, ``incomplete``, the counts and
    each walked template whose row has a ``meta_error``: its ``model.json`` is not JSON,
    cannot be stored, or does not validate."""
    rows = _rows(client)
    run = rows.start_run()
    unreadable = [{"template_id": "box", "error": "not JSON"}]
    rows.end_incomplete_run(run, added=2, refreshed=1, dropped=0, unreadable=unreadable)

    body = client.get(STATUS).json()
    assert body["finished_at"] is None
    assert body["incomplete"] is True
    assert (body["added"], body["refreshed"], body["dropped"]) == (2, 1, 0)
    assert body["unreadable"] == unreadable


def test_copy_status_404_before_any_run(client: TestClient) -> None:
    """The app's start ran the first copy (phase 3), so the runs go first: a database
    the copy never ran on."""
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    with state.projection.pool.connection() as conn:
        conn.execute("DELETE FROM template_copy_runs")
    response = client.get(STATUS)
    assert response.status_code == 404
    assert response.json()["detail"] == "no copy of the templates to rows has run"
