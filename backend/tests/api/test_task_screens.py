"""Task screens (#8062): the filters that find a task by its scan code and its assignee,
the server's check of scan and photo answers, and the entity kind `task`."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from scadbuddy.tasks.widgets import expects_entries
from tests.api.test_entity_attachments import upload
from tests.api.test_flows import settings
from tests.api.test_order_attachments import png
from tests.api.test_tasks import Harness, Rows, harness, parked, post, rows

__all__ = ["harness", "rows", "settings"]

# `parked` opens no task of its own: the tests add the task they need.
pytestmark = [
    pytest.mark.requires_postgres,
    pytest.mark.requires_temporal,
    pytest.mark.usefixtures("waits_before_8062"),
]

LOCATIONS = "/api/v1/filament/locations"
INVALID = "https://scadbuddy.dev/problems/tasks/invalid-answer"


def box_schema(**expects: Any) -> dict[str, Any]:
    box: dict[str, Any] = {"type": "string", "x-sb-widget": "scan"}
    if expects:
        box["expects"] = expects
    return {"type": "object", "required": ["box"], "properties": {"box": box}}


def add(
    rows: Rows,
    run_id: str,
    schema: dict[str, Any],
    *,
    task_id: str | None = None,
    assignee: str | None = None,
) -> str:
    task_id = task_id or str(uuid.uuid4())
    rows.conn.execute(
        "INSERT INTO tasks (id, run_id, title, schema, expects, assignee)"
        " VALUES (%s, %s, 'Scan', %s, %s, %s)",
        (task_id, run_id, Jsonb(schema), expects_entries(schema), assignee),
    )
    return task_id


def location(client: TestClient, name: str, **over: object) -> dict[str, Any]:
    kinds = client.get("/api/v1/filament/catalog/location-kinds").json()
    made = client.post(LOCATIONS, json={"name": name, "kind_id": kinds[0]["id"]} | over)
    assert made.status_code == 201, made.text
    row: dict[str, Any] = made.json()
    return row


def listed(client: TestClient, query: str) -> list[str]:
    response = client.get(f"/api/v1/tasks?{query}")
    assert response.status_code == 200, response.text
    return [t["id"] for t in response.json()["tasks"]]


def test_list_by_code(client: TestClient, rows: Rows) -> None:
    run_id, _ = parked(client)
    by_kind = add(rows, run_id, box_schema(kinds=["location"]))
    by_code = add(rows, run_id, box_schema(codes=["S-3"]))
    assert listed(client, "code=L-7") == [by_kind]
    assert listed(client, "code=s-3") == [by_code]
    assert listed(client, "code=X-1") == []
    assert client.get("/api/v1/tasks?code=nonsense").status_code == 422
    rows.conn.execute("UPDATE tasks SET state = 'cancelled' WHERE id = %s", (by_kind,))
    assert listed(client, "code=L-7") == []


def test_list_by_assignee(client: TestClient, rows: Rows) -> None:
    run_id, _ = parked(client)
    mine = add(rows, run_id, box_schema(), assignee="browser")
    add(rows, run_id, box_schema(), assignee="someone-else")
    add(rows, run_id, box_schema())
    assert listed(client, "assignee=me") == [mine]
    assert client.get("/api/v1/tasks?assignee=you").status_code == 422
    assert client.get(f"/api/v1/tasks/{mine}").json()["assignee"] == "browser"


def test_scan_answer_checked(client: TestClient, rows: Rows, harness: Harness) -> None:
    run_id, call_id = parked(client)
    add(rows, run_id, box_schema(kinds=["location"]), task_id=call_id)
    place = location(client, "Bench")
    code = f"L-{place['number']}"

    wrong_kind = post(client, call_id, {"box": "S-1"}, "k1")
    assert wrong_kind.status_code == 422, wrong_kind.text
    assert wrong_kind.json()["type"] == INVALID
    assert wrong_kind.json()["problems"][0]["field"] == "box"
    assert "S-1" not in wrong_kind.text

    nothing = post(client, call_id, {"box": "L-99999"}, "k2")
    assert nothing.status_code == 422, nothing.text
    assert nothing.json()["problems"][0]["field"] == "box"
    assert "99999" not in nothing.text

    nonsense = post(client, call_id, {"box": "nonsense"}, "k3")
    assert nonsense.status_code == 422
    assert "nonsense" not in nonsense.text

    client.patch(f"{LOCATIONS}/{place['id']}", json={"archived": True})
    archived = post(client, call_id, {"box": code}, "k4")
    assert archived.status_code == 422, archived.text
    assert code not in archived.text
    assert harness.sends == []

    client.patch(f"{LOCATIONS}/{place['id']}", json={"archived": False})
    good = post(client, call_id, {"box": code.lower()}, "k5")
    assert good.status_code == 200, good.text


def test_scan_answer_in_codes_passes_whatever_its_kind(
    client: TestClient, rows: Rows, harness: Harness
) -> None:
    run_id, call_id = parked(client)
    place = location(client, "Bench")
    code = f"L-{place['number']}"
    add(rows, run_id, box_schema(kinds=["spool"], codes=[code]), task_id=call_id)
    assert post(client, call_id, {"box": code}).status_code == 200


def test_photo_answer_must_be_this_tasks_attachment(
    client: TestClient, rows: Rows, harness: Harness
) -> None:
    run_id, call_id = parked(client)
    schema = {
        "type": "object",
        "required": ["shot"],
        "properties": {"shot": {"type": "string", "x-sb-widget": "photo"}},
    }
    add(rows, run_id, schema, task_id=call_id)
    other = add(rows, run_id, schema)
    elsewhere = upload(client, f"task:{other}", png(), "a.png", role="photo")
    assert elsewhere.status_code == 200, elsewhere.text
    refused = post(client, call_id, {"shot": elsewhere.json()["id"]}, "k1")
    assert refused.status_code == 422, refused.text
    assert refused.json()["problems"][0]["field"] == "shot"
    missing = post(client, call_id, {"shot": "no-such-id"}, "k2")
    assert missing.status_code == 422, missing.text
    assert harness.sends == []

    mine = upload(client, f"task:{call_id}", png(), "b.png", role="photo")
    assert mine.status_code == 200, mine.text
    assert post(client, call_id, {"shot": mine.json()["id"]}, "k3").status_code == 200


def test_task_entity_kind(client: TestClient, rows: Rows) -> None:
    run_id, call_id = parked(client)
    add(rows, run_id, box_schema(), task_id=call_id)
    stored = upload(client, f"task:{call_id}", png(), "a.png")
    assert stored.status_code == 200, stored.text
    gone = upload(client, f"task:{uuid.uuid4()}", png(), "a.png")
    assert gone.status_code == 404, gone.text


def entity_schema(kind: str) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["where"],
        "properties": {"where": {"type": "string", "x-sb-widget": "entity", "kind": kind}},
    }


def test_entity_answer_of_a_location_is_checked_as_a_scan(
    client: TestClient, rows: Rows, harness: Harness
) -> None:
    run_id, call_id = parked(client)
    add(rows, run_id, entity_schema("location"), task_id=call_id)
    place = location(client, "Bench")
    code = f"L-{place['number']}"

    for key, bad in (("k1", "S-1"), ("k2", "L-99999"), ("k3", "anything")):
        refused = post(client, call_id, {"where": bad}, key)
        assert refused.status_code == 422, refused.text
        assert refused.json()["problems"][0]["field"] == "where"
    client.patch(f"{LOCATIONS}/{place['id']}", json={"archived": True})
    assert post(client, call_id, {"where": code}, "k4").status_code == 422
    assert harness.sends == []

    client.patch(f"{LOCATIONS}/{place['id']}", json={"archived": False})
    assert post(client, call_id, {"where": code}, "k5").status_code == 200


def test_an_entity_field_is_offered_no_scan(client: TestClient, rows: Rows) -> None:
    run_id, _ = parked(client)
    add(rows, run_id, entity_schema("spool"))
    assert expects_entries(entity_schema("spool")) == []
    assert listed(client, "code=S-1") == []


def test_a_library_file_entity_answer_is_not_a_code(
    client: TestClient, rows: Rows, harness: Harness
) -> None:
    run_id, call_id = parked(client)
    add(rows, run_id, entity_schema("library_file"), task_id=call_id)
    assert post(client, call_id, {"where": str(uuid.uuid4())}).status_code == 200


def test_list_by_place_folds_a_location_code(client: TestClient, rows: Rows) -> None:
    run_id, _ = parked(client)
    task_id = add(rows, run_id, box_schema())
    rows.conn.execute("UPDATE tasks SET place = 'L-7' WHERE id = %s", (task_id,))
    assert listed(client, "place=l07") == [task_id]
    assert listed(client, "place=L-7") == [task_id]


def test_list_by_place_finds_a_place_kept_as_it_was_given(client: TestClient, rows: Rows) -> None:
    """A task opened before #8062 kept its place as it was given."""
    run_id, _ = parked(client)
    task_id = add(rows, run_id, box_schema())
    rows.conn.execute("UPDATE tasks SET place = 'l7' WHERE id = %s", (task_id,))
    assert listed(client, "place=l7") == [task_id]
    assert listed(client, "place=L-7") == []
