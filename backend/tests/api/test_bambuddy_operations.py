"""The Bambuddy writes as operations (#1053, spec 2026-10-01 §4.2, §4.3): a key makes a
retry answer the first outcome without a second effect; no key keeps today's
behaviour; a refusal writes no record."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.bambuddy import output_reader
from scadbuddy.workflows.problems import OPERATION_UNEXPECTED_DETAIL
from tests.api.test_print_actions import TIMELAPSE, mock_enqueue
from tests.api.test_print_history import link, mock_archive
from tests.api.test_project_file import project_folder_routes, uploads
from tests.api.test_send import API, configure, make_output, plate_routes, upload_route
from tests.api.test_sidebar import PUBLIC

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]


@respx.mock
def test_reprint_with_a_key_twice_queues_once(client: TestClient, model: str) -> None:
    configure(client)
    link(client, make_output(client, model), 35)
    mock_archive(35, printer_id=3, plate_id=2)
    queue = mock_enqueue(51)

    headers = {"Idempotency-Key": uuid.uuid4().hex}
    first = client.post("/api/v1/prints/35/reprint", headers=headers)
    again = client.post("/api/v1/prints/35/reprint", headers=headers)

    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()
    assert queue.call_count == 1


@respx.mock
def test_reprint_without_a_key_queues_twice(client: TestClient, model: str) -> None:
    configure(client)
    link(client, make_output(client, model), 35)
    mock_archive(35, printer_id=3, plate_id=2)
    queue = mock_enqueue(51)

    assert client.post("/api/v1/prints/35/reprint").status_code == 201
    assert client.post("/api/v1/prints/35/reprint").status_code == 201
    assert queue.call_count == 2


@respx.mock
def test_a_reprint_refusal_writes_no_operation(
    client: TestClient, model: str, pg_conninfo: str
) -> None:
    configure(client)
    link(client, make_output(client, model), 35)
    mock_archive(35, printer_id=None)
    queue = mock_enqueue()

    response = client.post(
        "/api/v1/prints/35/reprint", headers={"Idempotency-Key": uuid.uuid4().hex}
    )

    assert response.status_code == 409
    assert not queue.called
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute(
            "SELECT count(*) FROM operations WHERE kind <> 'output_create'"
        ).fetchone() == (0,)


@respx.mock
def test_a_reprint_is_recorded_as_an_operation(
    client: TestClient, model: str, pg_conninfo: str
) -> None:
    configure(client)
    link(client, make_output(client, model), 35)
    mock_archive(35, printer_id=3, plate_id=2)
    mock_enqueue(51)

    client.post("/api/v1/prints/35/reprint", headers={"Idempotency-Key": uuid.uuid4().hex})

    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute(
            "SELECT kind, subject, status, result FROM operations WHERE kind <> 'output_create'"
        ).fetchone()
    assert row is not None and row[:3] == ("reprint", "archive:35", "succeeded")
    assert row[3]["queue_item_id"] == 51


@respx.mock
def test_a_slow_bambuddy_read_in_the_check_is_bambuddys_504_not_an_unexpected_500(
    client: TestClient, model: str
) -> None:
    """The check's Bambuddy calls time out before its activity does, so a slow Bambuddy
    is the problem the route answered before #1053, not ScadBuddy failing (review)."""
    configure(client)
    link(client, make_output(client, model), 35)
    mock_archive(35, printer_id=3, plate_id=2)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(9)
        return httpx.Response(200, json={})

    respx.get(f"{API}/archives/35").mock(side_effect=slow)
    queue = mock_enqueue()

    began = time.monotonic()
    response = client.post(
        "/api/v1/prints/35/reprint", headers={"Idempotency-Key": uuid.uuid4().hex}
    )

    assert response.status_code == 504, response.text
    assert response.json()["type"].endswith("/bambuddy-unavailable")
    assert time.monotonic() - began < 12
    assert not queue.called


@respx.mock
def test_a_slow_check_that_is_not_bambuddy_is_not_blamed_on_bambuddy(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file-into-project check never calls Bambuddy: a slow read of the template is
    ScadBuddy's own timeout, not ``bambuddy-unavailable`` (review #1063 1)."""
    configure(client)
    output_id = make_output(client, model)
    effect = uploads(41)

    async def slow_stem(*args: Any) -> str:
        await asyncio.sleep(9)
        return "never"

    monkeypatch.setattr(output_reader, "output_stem", slow_stem)

    response = client.post(
        f"/api/v1/outputs/{output_id}/project-file",
        json={"project_id": 7},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )

    assert response.status_code == 504, response.text
    problem = response.json()
    assert problem["type"] == "about:blank"
    assert problem["detail"] == "the check did not finish within 6s; nothing was done"
    assert not effect.called


# --- every Bambuddy kind (review #1063 7) ----------------------------------------


@dataclass
class Case:
    path: str
    body: dict[str, Any] | None
    #: The Bambuddy write that is the kind's effect.
    effect: respx.Route
    status: int = 200


def _send(client: TestClient, model: str) -> Case:
    output_id = make_output(client, model)
    plate_routes()
    return Case(f"/api/v1/outputs/{output_id}/send", {"mode": "library"}, upload_route())


def _project_file(client: TestClient, model: str) -> Case:
    output_id = make_output(client, model)
    project_folder_routes()
    return Case(f"/api/v1/outputs/{output_id}/project-file", {"project_id": 7}, uploads(41))


def _create_project(client: TestClient, model: str) -> Case:
    respx.get(f"{API}/library/folders/by-project/7").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(200, json={"id": 9, "name": "P", "project_id": 7})
    )
    effect = respx.post(f"{API}/projects/").mock(
        return_value=httpx.Response(200, json={"id": 7, "name": "P", "status": "active"})
    )
    return Case("/api/v1/print/projects", {"name": "P"}, effect)


def _attach_routes() -> respx.Route:
    respx.get(f"{API}/queue/71").mock(
        return_value=httpx.Response(200, json={"id": 71, "status": "completed", "archive_id": 88})
    )
    respx.post(f"{API}/projects/7/add-archives").mock(return_value=httpx.Response(200))
    return respx.post(f"{API}/projects/7/add-queue").mock(return_value=httpx.Response(200))


def _attach_project(client: TestClient, model: str) -> Case:
    output_id = make_output(client, model)
    return Case(
        f"/api/v1/print/outputs/{output_id}/project",
        {"project_id": 7, "queue_item_ids": [71]},
        _attach_routes(),
    )


def _reprint(client: TestClient, model: str) -> Case:
    link(client, make_output(client, model), 35)
    mock_archive(35, printer_id=3, plate_id=2)
    return Case("/api/v1/prints/35/reprint", None, mock_enqueue(51), status=201)


def _timelapse_pull(client: TestClient, model: str) -> Case:
    link(client, make_output(client, model), 35)
    mock_archive(35)
    effect = respx.post(f"{API}/archives/35/timelapse/select").mock(
        return_value=httpx.Response(200, json={"status": "attached", "filename": TIMELAPSE})
    )
    return Case("/api/v1/prints/35/timelapse/pull", {"filename": TIMELAPSE}, effect, status=204)


def _register_sidebar(client: TestClient, model: str) -> Case:
    configure(client, public_url=PUBLIC)
    respx.get(f"{API}/external-links/").mock(return_value=httpx.Response(200, json=[]))
    effect = respx.post(f"{API}/external-links/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 3,
                "name": "ScadBuddy",
                "url": PUBLIC,
                "icon": "shapes",
                "open_in_new_tab": False,
                "sort_order": 0,
                "created_at": "2026-09-23T01:00:00Z",
                "updated_at": "2026-09-23T01:00:00Z",
            },
        )
    )
    return Case("/api/v1/settings/register-sidebar", None, effect)


KINDS: dict[str, Callable[[TestClient, str], Case]] = {
    "send": _send,
    "project_file": _project_file,
    "create_project": _create_project,
    "attach_project": _attach_project,
    "reprint": _reprint,
    "timelapse_pull": _timelapse_pull,
    "register_sidebar": _register_sidebar,
}


def _post(client: TestClient, case: Case, key: str) -> httpx.Response:
    response: httpx.Response = client.post(
        case.path, json=case.body, headers={"Idempotency-Key": key}
    )
    return response


@pytest.mark.parametrize("kind", list(KINDS))
@respx.mock
def test_a_key_sent_twice_is_one_effect(client: TestClient, model: str, kind: str) -> None:
    configure(client)
    case = KINDS[kind](client, model)
    key = uuid.uuid4().hex

    first = _post(client, case, key)
    again = _post(client, case, key)

    assert first.status_code == again.status_code == case.status, first.text
    assert first.content == again.content
    assert case.effect.call_count == 1


@pytest.mark.parametrize("kind", list(KINDS))
@respx.mock
def test_a_bambuddy_error_in_the_effect_is_a_failed_operation_with_its_problem(
    client: TestClient, model: str, pg_conninfo: str, kind: str
) -> None:
    configure(client)
    case = KINDS[kind](client, model)
    case.effect.mock(return_value=httpx.Response(500, json={"detail": "Bambuddy broke"}))

    response = _post(client, case, uuid.uuid4().hex)

    with psycopg.connect(pg_conninfo) as conn:
        row = conn.execute(
            "SELECT kind, status, error FROM operations WHERE kind <> 'output_create'"
        ).fetchone()
    assert row is not None and row[:2] == (kind, "failed"), row
    assert row[2]["detail"] != OPERATION_UNEXPECTED_DETAIL
    assert response.status_code == row[2]["status"] != 500, response.text
    assert response.json()["detail"] == row[2]["detail"]


@respx.mock
def test_attach_without_a_project_id_files_under_the_remembered_project(
    client: TestClient, model: str, pg_conninfo: str
) -> None:
    """``exclude_unset`` carries an omitted ``project_id`` into the operation as
    omitted, so the check resolves the remembered project (#317), not "No project"."""
    configure(client, last_project_id=7)
    output_id = make_output(client, model)
    add_queue = _attach_routes()

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/project",
        json={"queue_item_ids": [71]},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )

    assert response.status_code == 200, response.text
    assert response.json()["project_id"] == 7
    assert add_queue.call_count == 1
