"""#284: a print applies the accepted analyzer fixes it names, checked again first.

The fix is SB9001's (``tests/api/test_analyzers.py`` ``VERIFIED``): timelapse on, in
the verified ``print_options`` target. Accepted through the fix routes, named by its
decision id on the run, it reaches the queue item; removed, the same request is refused
before anything is uploaded.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.analyzers import builtin
from scadbuddy.core.settings import Settings
from tests.api.test_analyzers import VERIFIED, _ok
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import run_print, run_request, run_routes
from tests.api.test_send import configure, make_output, upload_route

pytestmark = pytest.mark.requires_postgres

FIX = {"diagnostic_key": "SB9001", "fix_id": "timelapse"}


@pytest.fixture
def settings(settings: Settings, pg_conninfo: str) -> Settings:
    return settings.model_copy(update={"database_url": pg_conninfo})


@pytest.fixture(autouse=True)
def verified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtin, "BUILTIN", (*builtin.BUILTIN, VERIFIED))


def _analysis(request: dict[str, Any]) -> dict[str, Any]:
    """The run's request as the analyzers take it, as the dialog sends it."""
    return {key: request[key] for key in ("printer_id", "filament_plan", "choices", "copies")}


def _accept(client: TestClient, output_id: str, request: dict[str, Any]) -> str:
    body = {"target": {"output_id": output_id}, "request": _analysis(request), **FIX}
    preview = _ok(client.post("/api/v1/analyzers/fixes/preview", json=body))
    decision = _ok(
        client.post(
            "/api/v1/analyzers/fixes/apply",
            json={**body, "fingerprint": preview["fingerprint"], "confirm": True},
        )
    )
    decision_id: str = decision["id"]
    return decision_id


@respx.mock
@pytest.mark.usefixtures("slots_from_bambuddy")
def test_a_named_accepted_fix_reaches_the_queue_item_and_a_removed_one_refuses(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload = upload_route()
    run_routes()
    slice_routes()
    queued = queue_route()
    request = run_request()
    decision_id = _accept(client, output_id, request)

    # The report names the decision a print sends to apply it.
    report = _ok(
        client.post(
            "/api/v1/analyzers/run",
            json={"target": {"output_id": output_id}, "request": _analysis(request)},
        )
    )
    assert {row["decision_id"] for row in report["accepted_changes"]} == {decision_id}

    response = run_print(client, output_id, json={**request, "accepted_fixes": [decision_id]})
    assert response.status_code == 200, response.text
    sent: dict[str, Any] = json.loads(queued.calls.last.request.content)
    assert sent["timelapse"] is True

    # Removed, the fix no longer holds: the print is refused, naming it, before any upload.
    _ok(client.delete(f"/api/v1/analyzers/decisions/{decision_id}"), 204)
    uploads = upload.call_count
    again = {**request, "accepted_fixes": [decision_id], "request_id": "after-removal"}
    problem = _ok(client.post(f"/api/v1/print/outputs/{output_id}/run", json=again), 409)
    assert problem["type"].endswith("/analyzer-fix-stale")
    (stale,) = problem["stale"]
    assert stale.startswith(decision_id)
    assert upload.call_count == uploads
    assert queued.call_count == 1


@respx.mock
@pytest.mark.usefixtures("slots_from_bambuddy")
def test_a_print_naming_no_fix_applies_none(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queued = queue_route()
    request = run_request()
    _accept(client, output_id, request)

    response = run_print(client, output_id, json=request)
    assert response.status_code == 200, response.text
    sent: dict[str, Any] = json.loads(queued.calls.last.request.content)
    assert sent.get("timelapse") is not True
