"""A flow's typed inputs through the routes (#2483), on a real Temporal and Postgres:
the schema in git beside the script, checked at registration; a run's values checked
and filled in when it starts, recorded on the run, sealed in its history, and the same
after a Reset."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from temporalio.client import Client

from scadbuddy.core.settings import Settings
from tests.api.test_flows import register, script, settings, start, until

__all__ = ["settings"]

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "The template"},
        "copies": {"type": "integer", "minimum": 1, "default": 2},
    },
    "required": ["slug"],
}
READS = script("i = await inputs()", "return [i['slug'], i['copies'] * 10]")


def test_the_schema_is_committed_beside_the_script(client: TestClient, settings: Settings) -> None:
    created = register(client, READS, inputs_schema=SCHEMA)
    assert created["inputs_schema"] == SCHEMA
    fetched = client.get(f"/api/v1/workflows/{created['id']}").json()
    assert fetched["inputs_schema"] == SCHEMA
    on_disk = settings.data_dir / "flows" / "swap" / "inputs.json"
    assert json.loads(on_disk.read_text()) == SCHEMA
    # A version without one has none, and a flow that never had one reads None.
    plain = register(client, script("return 1"))
    assert plain["inputs_schema"] is None and not on_disk.exists()
    restored = client.post(f"/api/v1/workflows/names/swap/versions/{created['commit']}/restore")
    assert restored.status_code == 201, restored.text
    assert restored.json()["inputs_schema"] == SCHEMA


def test_a_bad_schema_or_a_misuse_is_refused_at_registration(client: TestClient) -> None:
    bad = client.post(
        "/api/v1/workflows",
        json={"name": "swap", "script": READS, "inputs_schema": {"type": "array"}},
    )
    assert bad.status_code == 422
    assert {p["file"] for p in bad.json()["problems"]} == {"inputs.json"}
    misuse = client.post(
        "/api/v1/workflows",
        json={
            "name": "swap",
            "script": script("i = await inputs()", "return i['slug'] + 1"),
            "inputs_schema": SCHEMA,
        },
    )
    assert misuse.status_code == 422
    assert [(p["file"], p["line"]) for p in misuse.json()["problems"]] == [("flow.py", 4)]
    # Without a schema, the script has no `inputs()`.
    assert (
        client.post("/api/v1/workflows", json={"name": "swap", "script": READS}).status_code == 422
    )
    assert client.get("/api/v1/workflows").json() == []


def test_the_check_route_checks_the_schema_and_the_script_against_it(client: TestClient) -> None:
    good = client.post(
        "/api/v1/workflow-script/check", json={"script": READS, "inputs_schema": SCHEMA}
    )
    assert good.json() == {"problems": []}
    bad = client.post(
        "/api/v1/workflow-script/check",
        json={"script": script("return (await inputs())['nope']"), "inputs_schema": SCHEMA},
    )
    assert [p["line"] for p in bad.json()["problems"]] == [3]


def test_a_run_gets_its_values_with_defaults_and_records_them(client: TestClient) -> None:
    definition = register(client, READS, inputs_schema=SCHEMA)
    response = start(client, definition["id"], "k1", inputs={"slug": "box"})
    assert response.status_code == 202, response.text
    view = until(client, response.json()["id"], lambda v: v["status"] == "succeeded")
    assert view["run"]["result"] == "result: ['box', 20]"
    assert view["run"]["inputs"] == {"slug": "box", "copies": 2}
    listed = client.get("/api/v1/workflow-runs").json()
    assert listed[0]["inputs"] == {"slug": "box", "copies": 2}


@pytest.mark.parametrize(
    "inputs",
    [None, {}, {"slug": 1}, {"slug": "box", "copies": 0}, {"slug": "box", "colour": "red"}],
)
def test_values_that_do_not_fit_are_refused_before_anything_starts(
    client: TestClient, inputs: dict[str, Any] | None
) -> None:
    definition = register(client, READS, inputs_schema=SCHEMA)
    body = {} if inputs is None else {"inputs": inputs}
    response = start(client, definition["id"], "k", **body)
    assert response.status_code == 422, response.text
    assert response.json()["type"].endswith("/flow-inputs")
    assert response.json()["problems"]
    assert client.get("/api/v1/workflow-runs").json() == []


def test_a_flow_without_a_schema_takes_no_inputs(client: TestClient) -> None:
    definition = register(client, script("return 1"))
    refused = start(client, definition["id"], "k1", inputs={"slug": "box"})
    assert refused.status_code == 422
    response = start(client, definition["id"], "k2", inputs={})
    assert response.status_code == 202
    view = until(client, response.json()["id"], lambda v: v["status"] == "succeeded")
    assert view["run"]["inputs"] is None


def test_the_inputs_are_sealed_in_the_runs_history(client: TestClient, settings: Settings) -> None:
    secret = "lid-only-9c1d"
    definition = register(client, READS, inputs_schema=SCHEMA)
    run_id = start(client, definition["id"], "s", inputs={"slug": secret}).json()["id"]
    until(client, run_id, lambda v: v["status"] == "succeeded")

    async def history() -> str:
        temporal = await Client.connect(settings.temporal_address, namespace="default")
        return (await temporal.get_workflow_handle(f"flow-{run_id}").fetch_history()).to_json()

    assert secret not in asyncio.run(history())


def test_a_reset_runs_again_with_the_runs_own_inputs(client: TestClient, pg_conninfo: str) -> None:
    definition = register(
        client,
        script(
            "i = await inputs()",
            "a = await wait_for_human('A?', 600)",
            "b = await wait_for_human('B?', 600)",
            "return [i['slug'], i['copies'], a['answer'], b['answer']]",
        ),
        inputs_schema=SCHEMA,
    )
    run_id = start(client, definition["id"], "r", inputs={"slug": "box", "copies": 5}).json()["id"]

    def answer(prompt: str, text: str, key: str) -> None:
        view = until(client, run_id, lambda v: [p["prompt"] for p in v["pending"]] == [prompt])
        answered = client.post(
            f"/api/v1/workflow-runs/{run_id}/answer",
            json={"call_id": view["pending"][0]["call_id"], "answer": text},
            headers={"Idempotency-Key": key},
        )
        assert answered.status_code == 200, answered.text

    answer("A?", "1", "a1")
    waiting = until(client, run_id, lambda v: [p["prompt"] for p in v["pending"]] == ["B?"])
    # Nothing outside the history feeds the replay: the row's copy goes.
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute("UPDATE workflow_runs SET inputs = NULL WHERE id = %s", (run_id,))

    def preview(event_id: int) -> dict[str, Any]:
        found: dict[str, Any] = client.get(
            f"/api/v1/workflow-runs/{run_id}/reset-preview", params={"event_id": event_id}
        ).json()
        return found

    last = preview(1)["as_of_event_id"]
    point = next(e for e in range(last - 1, 0, -1) if preview(e)["valid"])
    shown = preview(point)
    reset = client.post(
        f"/api/v1/workflow-runs/{run_id}/reset",
        json={
            "event_id": point,
            "as_of_event_id": shown["as_of_event_id"],
            "workflow_run_id": shown["workflow_run_id"],
        },
        headers={"Idempotency-Key": "reset-1"},
    )
    assert reset.status_code == 200, reset.text
    replaced = reset.json()["workflow_run_id"]
    assert replaced != waiting["run"]["workflow_run_id"]
    until(client, run_id, lambda v: v["run"]["workflow_run_id"] == replaced)
    answer("A?", "3", "a2")
    answer("B?", "2", "b2")
    done = until(client, run_id, lambda v: v["status"] == "succeeded")
    assert done["run"]["result"] == "result: ['box', 5, '3', '2']"
