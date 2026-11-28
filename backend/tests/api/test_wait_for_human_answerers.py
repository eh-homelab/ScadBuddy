"""An assistant answers a new run's `wait_for_human` task (#8062, #7471) at
`POST /tasks/{id}/answer`, when the setting `flow_answerers` or the run's grant admits it.
These take no `waits_before_8062`: the call opens a task."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api.test_flow_answerers_routes import (
    MINE,
    answered,
    answerers,
    answers_grant,
    as_agent,
    client,
    credential,
    decision,
    refused,
    settings,
)
from tests.api.test_flows import register, script, until

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]
#: The fixtures this module takes from the answerers' routes.
__all__ = ["client", "credential", "settings"]


def asked(
    client: TestClient, call: str, start: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
    """A run parked on `call`, which returns the answer, and its entry."""
    definition = register(client, script(f"a = await {call}", "return a['answer']"))
    started = client.post(
        f"/api/v1/workflows/{definition['id']}/runs",
        headers={"Idempotency-Key": "w"},
        json=start or {},
    )
    assert started.status_code in (200, 201, 202), started.text
    run_id = started.json()["id"]
    view = until(client, run_id, lambda v: v["status"] == "waiting" and v["live"] and v["pending"])
    [entry] = view["pending"]
    assert entry["fn"] == "wait_for_human" and entry["task"] is not None
    return run_id, entry


def give(client: TestClient, entry: dict[str, Any], headers: dict[str, str], key: str) -> Any:
    return client.post(
        f"/api/v1/tasks/{entry['call_id']}/answer",
        json={"answer": {"answer": "pink"}},
        headers={"Idempotency-Key": key, **headers},
    )


@pytest.mark.parametrize("by", ["setting", "grant"])
def test_an_admitted_assistant_answers_the_task(
    app: FastAPI, client: TestClient, credential: str, by: str
) -> None:
    if by == "setting":
        answerers(client, "sessions")
    run_id, entry = asked(
        client, "wait_for_human('Swap?')", answers_grant("sessions") if by == "grant" else None
    )
    answered(give(client, entry, as_agent(credential), "a"))
    done = until(client, run_id, lambda v: v["status"] == "succeeded")
    assert done["run"]["result"] == "result: 'pink'"
    recorded = decision(app, run_id)
    assert recorded is not None and recorded["responder"] == f"session:{MINE}"
    task = client.get(f"/api/v1/tasks/{entry['call_id']}").json()
    assert (task["state"], task["answer"], task["answered_by"]) == (
        "answered",
        {"answer": "pink"},
        f"session:{MINE}",
    )


@pytest.mark.parametrize("by", ["setting", "grant"])
def test_a_question_for_people_only_refuses_the_assistant(
    app: FastAPI, client: TestClient, credential: str, by: str
) -> None:
    if by == "setting":
        answerers(client, "sessions")
    run_id, entry = asked(
        client,
        "wait_for_human('Swap?', people_only=True)",
        answers_grant("sessions") if by == "grant" else None,
    )
    assert entry["people_only"] is True
    response = give(client, entry, as_agent(credential), "a")
    refused(response)
    assert response.json()["people_only"] is True
    assert decision(app, run_id) is None
    answered(give(client, entry, {}, "b"))
    assert until(client, run_id, lambda v: v["status"] == "succeeded")["run"]["result"] == (
        "result: 'pink'"
    )
