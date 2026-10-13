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

from tests.api.test_flows import register, script, settings, start, until
from tests.flows.flows_support import parent_session

__all__ = ["settings"]

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

PRINT = script(
    "try:",
    "    await queue_print({'output_id': 'o1'}, {'choices': {}, 'copies': 3})",
    "except Exception as e:",
    "    return str(e)",
)
TOKEN = "0f0e0d0c-0b0a-4908-8706-050403020100"
SESSION = "5e551011-0000-4000-8000-000000000001"
AGENT = {"X-ScadBuddy-Agent-Author": f"token:{TOKEN}", "X-ScadBuddy-Agent-Author-Session": SESSION}
ACTOR = f"session:{SESSION}"
OTHER_SESSION = {
    **AGENT,
    "X-ScadBuddy-Agent-Author-Session": "5e551011-0000-4000-8000-000000000002",
}
OTHER_PRINCIPAL = {
    **AGENT,
    "X-ScadBuddy-Agent-Author": "token:0f0e0d0c-0b0a-4908-8706-0504030201ff",
}
WITH_PERSON = "https://scadbuddy.dev/problems/flow-escalation-with-person"
PROPOSAL_CHANGED = "https://scadbuddy.dev/problems/flow-escalation-proposal-changed"
NOT_NARROWER = "https://scadbuddy.dev/problems/flow-escalation-not-a-narrowing"
NOT_PARENT = "https://scadbuddy.dev/problems/flow-escalation-not-parent"


@pytest.fixture(autouse=True)
def parent(pg_conninfo: str) -> None:
    """The starter session, owned by a token that holds `outward`."""
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        parent_session(conn, SESSION, f"token:{TOKEN}")


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
        ("pass_up", ACTOR),
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
    assert (outcome, responder) == ("denied", ACTOR)
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


def proposed(client: TestClient, run_id: str, entry: Any) -> Any:
    """The parent narrows the parked print to one copy; no grant admits it, so it goes up."""
    narrowed = {"choices": {}, "copies": 1}
    answer = escalate(
        client, run_id, entry["call_id"], {"action": "narrow", "input": narrowed}, **AGENT
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["outcome"] == "with_person"
    return narrowed


def proposal_view(client: TestClient, run_id: str, call_id: str, **headers: str) -> Any:
    return client.get(f"/api/v1/workflow-runs/{run_id}/proposals/{call_id}", headers=headers)


def decide_proposal(client: TestClient, run_id: str, call_id: str, **body: Any) -> Any:
    return client.post(
        f"/api/v1/workflow-runs/{run_id}/decide",
        json={"call_id": call_id, "approved": True, "proposal": True, **body},
        headers={"Idempotency-Key": f"d-{body.get('proposal_hash')}"},
    )


def test_a_narrow_outside_the_grant_reaches_the_person_as_a_proposal(
    client: TestClient, pg_conninfo: str
) -> None:
    """No grant admits anything, so the narrowed input goes up as a proposal. The run's
    reads never return it; only the person's own read shows it beside the call's input,
    and the person approves the very proposal that read showed."""
    run_id, entry = parked(client, AGENT)
    narrowed = proposed(client, run_id, entry)
    [moved] = until(client, run_id, lambda v: v["pending"][0]["hop"] == "person")["pending"]
    assert moved["proposed"] is True
    assert [(h["action"], h.get("reason")) for h in moved["hops"]][-1] == (
        "narrow",
        "no grant in force",
    )
    assert "copies" not in str(client.get(f"/api/v1/workflow-runs/{run_id}").json())
    shown = proposal_view(client, run_id, entry["call_id"])
    assert shown.status_code == 200, shown.text
    view = shown.json()
    assert view["original"]["request"] == {"choices": {}, "copies": 3}
    assert view["proposed"]["request"] == narrowed
    assert view["proposed_by"] == ACTOR
    decided = decide_proposal(client, run_id, entry["call_id"], proposal_hash=view["proposal_hash"])
    assert decided.status_code == 200, decided.text
    with psycopg.connect(pg_conninfo) as conn:
        [(responder, response, hops)] = conn.execute(
            "SELECT responder, response, hops FROM workflow_run_decisions"
        ).fetchall()
    assert responder == "browser"
    assert response["args"]["request"] == narrowed
    assert response["narrowed_by"] == ACTOR
    assert [h["action"] for h in hops] == ["parked", "narrow", "approve"]


def test_only_the_person_reads_a_proposal(client: TestClient) -> None:
    run_id, entry = parked(client, AGENT)
    proposed(client, run_id, entry)
    refused = proposal_view(client, run_id, entry["call_id"], **AGENT)
    assert refused.status_code == 403


@pytest.mark.parametrize("given", [None, "0" * 64])
def test_a_proposal_approved_without_the_hash_it_was_shown_is_refused(
    client: TestClient, given: str | None
) -> None:
    run_id, entry = parked(client, AGENT)
    proposed(client, run_id, entry)
    body = {} if given is None else {"proposal_hash": given}
    refused = decide_proposal(client, run_id, entry["call_id"], **body)
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"] == PROPOSAL_CHANGED


def test_a_proposal_changed_after_it_was_shown_is_refused(
    client: TestClient, pg_conninfo: str
) -> None:
    run_id, entry = parked(client, AGENT)
    proposed(client, run_id, entry)
    shown = proposal_view(client, run_id, entry["call_id"]).json()
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        # However it changed (a stale row, a write this code never makes): what the
        # person saw is no longer what would run.
        conn.execute(
            "UPDATE workflow_run_proposals SET args = jsonb_set(args, '{request,copies}', '2'),"
            " input_hash = 'changed'"
        )
    refused = decide_proposal(
        client, run_id, entry["call_id"], proposal_hash=shown["proposal_hash"]
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"] == PROPOSAL_CHANGED


@pytest.mark.parametrize(
    "narrowed",
    [
        {"choices": {}, "copies": 5},
        {"choices": {}, "copies": 1, "printer_id": 9},
        {"choices": {"quality": "fine"}, "copies": 1},
        {"choices": {}, "copies": 1, "all_plates": True},
        {"choices": {}},
    ],
)
def test_a_proposal_that_widens_or_substitutes_is_refused_and_nothing_is_stored(
    client: TestClient, pg_conninfo: str, narrowed: dict[str, Any]
) -> None:
    run_id, entry = parked(client, AGENT)
    refused = escalate(
        client, run_id, entry["call_id"], {"action": "narrow", "input": narrowed}, **AGENT
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["type"] == NOT_NARROWER
    assert until(client, run_id, lambda v: v["pending"])["pending"][0]["hop"] == "parent"
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_run_proposals").fetchone() == (0,)


def test_a_parent_whose_tiers_stop_short_of_the_call_cannot_narrow_it(
    client: TestClient, pg_conninfo: str
) -> None:
    """Its token holds `write` now: it may still deny or pass up, never narrow an outward
    call into running."""
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        parent_session(conn, SESSION, f"token:{TOKEN}", tier="write")
    run_id, entry = parked(client, AGENT)
    refused = escalate(
        client,
        run_id,
        entry["call_id"],
        {"action": "narrow", "input": {"choices": {}, "copies": 1}},
        **AGENT,
    )
    assert refused.status_code == 403, refused.text
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM workflow_run_proposals").fetchone() == (0,)
    passed = escalate(client, run_id, entry["call_id"], {"action": "pass_up"}, **AGENT)
    assert passed.status_code == 200, passed.text


def test_a_session_handed_to_another_owner_is_no_parent(
    client: TestClient, pg_conninfo: str
) -> None:
    run_id, entry = parked(client, AGENT)
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        parent_session(conn, SESSION, "browser")
    refused = escalate(client, run_id, entry["call_id"], {"action": "pass_up"}, **AGENT)
    assert refused.status_code == 403, refused.text
    assert refused.json()["type"] == NOT_PARENT


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
