"""`GET /outputs/copy-status` (#7255): the last run of the start's copy of the saved
outputs' files to rows. The tests' apps skip the start's copy (`app_output_copy_off`),
so each test writes the runs it reads."""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.library.output_rows import OutputRows
from scadbuddy.library.outputs import OutputStore


def _rows(client: TestClient) -> OutputRows:
    store: OutputStore = getattr(client.app.state, STATE_ATTR).outputs  # type: ignore[attr-defined]
    assert store.rows is not None
    return store.rows


def test_copy_status_reports_last_run(client: TestClient) -> None:
    rows = _rows(client)
    first = rows.start_run()
    damaged = [{"output_id": "a" * 32, "slug": "box", "file": "bom.json", "error": "OSError"}]
    rows.finish_run(
        first, copied=3, refreshed=2, dropped=1, not_copied=["box/" + "b" * 32], damaged=damaged
    )

    response = client.get("/api/v1/outputs/copy-status")
    assert response.status_code == 200
    body = response.json()
    assert datetime.fromisoformat(body["started_at"]) <= datetime.fromisoformat(body["finished_at"])
    assert (body["copied"], body["refreshed"], body["dropped"]) == (3, 2, 1)
    assert body["not_copied"] == ["box/" + "b" * 32]
    assert body["damaged"] == damaged
    assert "id" not in body

    # A run that has not finished is the last run: it has no end and counts nothing yet.
    rows.start_run()
    later = client.get("/api/v1/outputs/copy-status").json()
    assert later["finished_at"] is None
    assert (later["copied"], later["refreshed"], later["dropped"]) == (0, 0, 0)
    assert later["not_copied"] == [] and later["damaged"] == []


def test_copy_status_404_before_any_run(client: TestClient) -> None:
    response = client.get("/api/v1/outputs/copy-status")
    assert response.status_code == 404
    assert "copy" in response.json()["detail"]
