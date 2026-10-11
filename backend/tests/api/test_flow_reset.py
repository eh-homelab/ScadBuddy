"""The reset routes (#1057, plan 2026-10-09-durable-phase-6-flows.md Task E2) on a real
Temporal and Postgres. The outward-call cases are `tests/flows/test_flow_reset.py`'s,
against the fake routes: the API test app has no `SCADBUDDY_API_INTERNAL_URL`."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.api.test_flows import register, script, settings, start, until

__all__ = ["settings"]

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]


def preview(client: TestClient, run_id: str, event_id: int) -> dict[str, Any]:
    response = client.get(
        f"/api/v1/workflow-runs/{run_id}/reset-preview", params={"event_id": event_id}
    )
    assert response.status_code == 200, response.text
    found: dict[str, Any] = response.json()
    return found


def test_a_reset_undoes_the_answer_and_the_dropped_question_is_stale(
    client: TestClient,
) -> None:
    definition = register(
        client,
        script(
            "a = await wait_for_human('A?', 600)",
            "b = await wait_for_human('B?', 600)",
            "return [a['answer'], b['answer']]",
        ),
    )
    run_id = start(client, definition["id"], "r").json()["id"]
    first = until(client, run_id, lambda v: [p["prompt"] for p in v["pending"]] == ["A?"])
    answered = client.post(
        f"/api/v1/workflow-runs/{run_id}/answer",
        json={"call_id": first["pending"][0]["call_id"], "answer": "1"},
        headers={"Idempotency-Key": "a"},
    )
    assert answered.status_code == 200, answered.text
    second = until(client, run_id, lambda v: [p["prompt"] for p in v["pending"]] == ["B?"])
    old_call = second["pending"][0]["call_id"]
    # The latest point a Reset takes: the task that took the answer and asked 'B?'. It
    # is done again without the answer, so 'A?' waits again under its own call id.
    last = preview(client, run_id, 1)["as_of_event_id"]
    point = next(e for e in range(last - 1, 0, -1) if preview(client, run_id, e)["valid"])
    shown = preview(client, run_id, point)
    assert shown["calls"] == []
    response = client.post(
        f"/api/v1/workflow-runs/{run_id}/reset",
        json={
            "event_id": point,
            "as_of_event_id": shown["as_of_event_id"],
            "workflow_run_id": shown["workflow_run_id"],
        },
        headers={"Idempotency-Key": "reset-1"},
    )
    assert response.status_code == 200, response.text
    reset = response.json()
    assert reset["workflow_run_id"] != second["run"]["workflow_run_id"]
    again = until(
        client,
        run_id,
        lambda v: (
            v["run"]["workflow_run_id"] == reset["workflow_run_id"]
            and [p["prompt"] for p in v["pending"]] == ["A?"]
        ),
    )
    assert again["pending"][0]["call_id"] == first["pending"][0]["call_id"]
    stale = client.post(
        f"/api/v1/workflow-runs/{run_id}/answer",
        json={"call_id": old_call, "answer": "x"},
        headers={"Idempotency-Key": "b-old"},
    )
    assert stale.status_code == 409
    assert stale.json()["type"].endswith("/stale-entry")
    for key, prompt, answer in (("a-2", "A?", "3"), ("b-2", "B?", "2")):
        view = until(client, run_id, lambda v, p=prompt: [e["prompt"] for e in v["pending"]] == [p])
        response = client.post(
            f"/api/v1/workflow-runs/{run_id}/answer",
            json={"call_id": view["pending"][0]["call_id"], "answer": answer},
            headers={"Idempotency-Key": key},
        )
        assert response.status_code == 200, response.text
    done = until(client, run_id, lambda v: v["status"] == "succeeded")
    assert done["run"]["result"] == "result: ['3', '2']"


def test_a_point_before_the_script_is_refused(client: TestClient) -> None:
    definition = register(client, script("await wait_for_human('q?', 600)"))
    run_id = start(client, definition["id"], "p").json()["id"]
    until(client, run_id, lambda v: v["status"] == "waiting")
    early = preview(client, run_id, 4)
    assert early["valid"] is False
    response = client.post(
        f"/api/v1/workflow-runs/{run_id}/reset",
        json={"event_id": 4, "as_of_event_id": 0, "workflow_run_id": early["workflow_run_id"]},
        headers={"Idempotency-Key": "early"},
    )
    assert response.status_code == 422
    assert response.json()["type"].endswith("/flow-reset-point")


def test_a_run_parked_before_resets_recorded_where_is_refused(
    client: TestClient, pg_conninfo: str
) -> None:
    definition = register(client, script("await wait_for_human('q?', 600)"))
    run_id = start(client, definition["id"], "legacy").json()["id"]
    until(client, run_id, lambda v: v["status"] == "waiting")
    # As a row written before 6e: the parked call has no history length.
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute(
            "UPDATE workflow_runs SET waiting_on = (SELECT jsonb_agg(w - 'history_length')"
            " FROM jsonb_array_elements(waiting_on) w) WHERE id = %s",
            (run_id,),
        )
    response = client.post(
        f"/api/v1/workflow-runs/{run_id}/reset",
        json={"event_id": 5, "as_of_event_id": 0, "workflow_run_id": "x"},
        headers={"Idempotency-Key": "legacy"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["type"].endswith("/flow-reset-unrecorded")


def test_an_unknown_run_has_no_preview(client: TestClient) -> None:
    response = client.get(
        "/api/v1/workflow-runs/00000000-0000-0000-0000-000000000000/reset-preview",
        params={"event_id": 5},
    )
    assert response.status_code == 404
