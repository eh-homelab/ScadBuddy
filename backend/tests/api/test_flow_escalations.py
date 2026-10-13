"""A flow-own call outside the run's grant goes to the starter session first (#2482,
spec 2026-10-11 §4.1, §4.2, §4.4, plan Task B3): the call parks at hop `parent`, and
`POST /workflow-runs/{id}/escalations/{call_id}` lets that session deny it, narrow it
into the grant or pass it up. It never approves, and nobody else acts at that hop: the
starter session is read from the run's record, the call from the run's own parked
entry, never from the request."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.api.test_flows import AGENT, register, script, settings, start, until

__all__ = ["settings"]

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

PRINT = script(
    "try:",
    "    await queue_print({'output_id': 'o1'}, {'choices': {}})",
    "except Exception as e:",
    "    return str(e)",
)
OTHER_SESSION = {**AGENT, "X-ScadBuddy-Agent-Author-Session": "s2"}
OTHER_PRINCIPAL = {**AGENT, "X-ScadBuddy-Agent-Author": "token:p2"}
WITH_PERSON = "https://scadbuddy.dev/problems/flow-escalation-with-person"
NOT_PARENT = "https://scadbuddy.dev/problems/flow-escalation-not-parent"


def parked(client: TestClient, headers: dict[str, str], key: str = "p") -> tuple[str, Any]:
    definition = register(client, PRINT)
    response = client.post(
        f"/api/v1/workflows/{definition['id']}/runs",
        headers={"Idempotency-Key": key, **headers},
    )
    assert response.status_code == 202, response.text
    run_id: str = response.json()["id"]
    view = until(client, run_id, lambda v: v["status"] == "waiting" and v["pending"])
    [entry] = view["pending"]
    return run_id, entry


def escalate(
    client: TestClient, run_id: str, call_id: str, body: dict[str, Any], **headers: str
) -> Any:
    return client.post(
        f"/api/v1/workflow-runs/{run_id}/escalations/{call_id}",
        json=body,
        headers={"Idempotency-Key": f"e-{body.get('action')}", **headers},
    )


def test_an_agent_started_runs_outside_call_parks_with_the_starter_session(
    client: TestClient,
) -> None:
    _, entry = parked(client, AGENT)
    assert (entry["kind"], entry["fn"], entry["hop"]) == ("approval", "queue_print", "parent")
    assert entry["hop_until"] is not None
    assert entry["reason"] == "no grant in force"
    assert [h["action"] for h in entry["hops"]] == ["parked"]


def test_a_browser_started_run_has_no_parent_hop(client: TestClient) -> None:
    definition = register(client, PRINT)
    run_id = start(client, definition["id"], "b").json()["id"]
    view = until(client, run_id, lambda v: v["status"] == "waiting" and v["pending"])
    [entry] = view["pending"]
    assert entry["hop"] == "person"
    refused = escalate(client, run_id, entry["call_id"], {"action": "pass_up"}, **AGENT)
    assert refused.status_code == 403
    assert refused.json()["type"] == NOT_PARENT


def test_a_token_started_run_with_no_session_has_no_parent_hop(client: TestClient) -> None:
    token_only = {"X-ScadBuddy-Agent-Author": "token:p1"}
    _, entry = parked(client, token_only)
    assert entry["hop"] == "person"


def test_the_starter_session_passes_it_up(client: TestClient) -> None:
    run_id, entry = parked(client, AGENT)
    answer = escalate(client, run_id, entry["call_id"], {"action": "pass_up"}, **AGENT)
    assert answer.status_code == 200, answer.text
    assert answer.json()["outcome"] == "passed_up"
    [moved] = until(client, run_id, lambda v: v["pending"][0]["hop"] == "person")["pending"]
    assert moved["hop_until"] is None
    assert [(h["action"], h["actor"]) for h in moved["hops"]] == [
        ("parked", "flow"),
        ("pass_up", "session:s1"),
    ]
    # Passed up once: at hop person, the starter session has nothing left to do.
    again = escalate(client, run_id, entry["call_id"], {"action": "deny"}, **AGENT)
    assert again.status_code == 409
    assert again.json()["type"] == WITH_PERSON


def test_the_starter_session_denies_it_and_the_script_reads_why(
    client: TestClient, pg_conninfo: str
) -> None:
    run_id, entry = parked(client, AGENT)
    answer = escalate(
        client, run_id, entry["call_id"], {"action": "deny", "reason": "wrong printer"}, **AGENT
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["outcome"] == "denied"
    done = until(client, run_id, lambda v: v["status"] == "succeeded")
    assert "ToolApprovalDenied" in done["run"]["result"]
    assert "wrong printer" in done["run"]["result"]
    with psycopg.connect(pg_conninfo) as conn:
        [(outcome, responder, hops)] = conn.execute(
            "SELECT outcome, responder, hops FROM workflow_run_decisions"
        ).fetchall()
    assert (outcome, responder) == ("denied", "session:s1")
    assert [h["action"] for h in hops] == ["parked", "deny"]


@pytest.mark.parametrize("headers", [OTHER_SESSION, OTHER_PRINCIPAL, {}])
def test_nobody_but_the_starter_session_acts_at_the_parent_hop(
    client: TestClient, headers: dict[str, str]
) -> None:
    """Another session of the same principal, another principal claiming the session,
    and the browser (which decides through `/decide`): none of them is the parent."""
    run_id, entry = parked(client, AGENT)
    for action in ("deny", "pass_up", "narrow"):
        body: dict[str, Any] = {"action": action}
        if action == "narrow":
            body["input"] = {"choices": {}}
        refused = escalate(client, run_id, entry["call_id"], body, **headers)
        assert refused.status_code == 403, refused.text
        assert refused.json()["type"] == NOT_PARENT
    view = until(client, run_id, lambda v: v["pending"])
    assert view["pending"][0]["hop"] == "parent"


def test_approve_is_not_an_action(client: TestClient) -> None:
    run_id, entry = parked(client, AGENT)
    for body in ({"action": "approve"}, {"action": "approved"}, {"approved": True}):
        refused = escalate(client, run_id, entry["call_id"], body, **AGENT)
        assert refused.status_code == 422, refused.text
    assert until(client, run_id, lambda v: v["pending"])["pending"][0]["hop"] == "parent"


def test_a_call_that_is_not_waiting_is_stale(client: TestClient) -> None:
    run_id, _ = parked(client, AGENT)
    refused = escalate(client, run_id, "not-a-call", {"action": "pass_up"}, **AGENT)
    assert refused.status_code == 409
    assert refused.json()["type"].endswith("/stale-entry")


def test_a_narrow_outside_the_grant_reaches_the_person_as_a_proposal(
    client: TestClient, pg_conninfo: str
) -> None:
    """No grant admits anything, so the narrowed input goes up as a proposal; it is
    never shown raw, and the call still runs only on the person's word."""
    run_id, entry = parked(client, AGENT)
    narrowed = {"choices": {}, "copies": 1}
    answer = escalate(
        client, run_id, entry["call_id"], {"action": "narrow", "input": narrowed}, **AGENT
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["outcome"] == "with_person"
    [moved] = until(client, run_id, lambda v: v["pending"][0]["hop"] == "person")["pending"]
    assert moved["proposed"] is True
    assert [(h["action"], h.get("reason")) for h in moved["hops"]][-1] == (
        "narrow",
        "no grant in force",
    )
    assert "copies" not in str(client.get(f"/api/v1/workflow-runs/{run_id}").json())
    # The person approves the proposal: the print is sent with the narrowed request.
    decided = client.post(
        f"/api/v1/workflow-runs/{run_id}/decide",
        json={"call_id": entry["call_id"], "approved": True, "proposal": True},
        headers={"Idempotency-Key": "d"},
    )
    assert decided.status_code == 200, decided.text
    with psycopg.connect(pg_conninfo) as conn:
        [(responder, response, hops)] = conn.execute(
            "SELECT responder, response, hops FROM workflow_run_decisions"
        ).fetchall()
    assert responder == "browser"
    assert response["args"]["request"] == narrowed
    assert response["narrowed_by"] == "session:s1"
    assert [h["action"] for h in hops] == ["parked", "narrow", "approve"]


def test_the_person_may_decide_while_it_is_with_the_starter_session(
    client: TestClient, pg_conninfo: str
) -> None:
    run_id, entry = parked(client, AGENT)
    decided = client.post(
        f"/api/v1/workflow-runs/{run_id}/decide",
        json={"call_id": entry["call_id"], "approved": False, "reason": "no"},
        headers={"Idempotency-Key": "d"},
    )
    assert decided.status_code == 200, decided.text
    until(client, run_id, lambda v: v["status"] == "succeeded")
    refused = escalate(client, run_id, entry["call_id"], {"action": "pass_up"}, **AGENT)
    assert refused.status_code == 409
    with psycopg.connect(pg_conninfo) as conn:
        [(hops,)] = conn.execute("SELECT hops FROM workflow_run_decisions").fetchall()
    assert [(h["hop"], h["action"]) for h in hops] == [("parent", "parked"), ("parent", "deny")]


def test_a_proposal_cannot_be_approved_when_there_is_none(client: TestClient) -> None:
    run_id, entry = parked(client, AGENT)
    refused = client.post(
        f"/api/v1/workflow-runs/{run_id}/decide",
        json={"call_id": entry["call_id"], "approved": True, "proposal": True},
        headers={"Idempotency-Key": "d"},
    )
    assert refused.status_code == 409, refused.text
