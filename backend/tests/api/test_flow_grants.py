"""A run's grant through the routes (#2481, plan 2026-10-11-flow-grants-and-git.md Task
A2): a browser start's grant is approved, an agent's is requested; only the browser
approves or declines one; the browser or the starter revokes it, once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.api.test_flows import AGENT, register, script, settings, start, until

__all__ = ["settings"]

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

WAITS = script("await wait_for_human('Go on?', 600)")


def grant(*rules: dict[str, Any], hours: float = 24, **fields: Any) -> dict[str, Any]:
    expires = datetime.now(UTC) + timedelta(hours=hours)
    return {"expires_at": expires.isoformat(), "rules": list(rules), **fields}


def started(client: TestClient, key: str, body: dict[str, Any], **headers: str) -> str:
    definition = register(client, WAITS)
    response = client.post(
        f"/api/v1/workflows/{definition['id']}/runs",
        headers={"Idempotency-Key": key, **headers},
        json=body,
    )
    assert response.status_code == 202, response.text
    run_id: str = response.json()["id"]
    until(client, run_id, lambda v: v["status"] == "waiting")
    return run_id


def view_grant(client: TestClient, run_id: str) -> Any:
    return client.get(f"/api/v1/workflow-runs/{run_id}").json()["grant"]


def test_a_browser_start_approves_its_grant(client: TestClient) -> None:
    run_id = started(client, "b", {"grant": grant({"fn": "queue_print", "printers": [3]})})
    shown = view_grant(client, run_id)
    assert (shown["status"], shown["approved_by"]) == ("approved", "browser")
    assert shown["spec"]["rules"] == [
        {
            "fn": "queue_print",
            "tool": None,
            "printers": [3],
            "models": None,
            "library_files": None,
            "max_prints": None,
            "max_filament_g": None,
            "max_calls": None,
            "args": None,
        }
    ]
    assert shown["in_force"] is True and shown["uses"] == []


def test_a_run_started_without_a_grant_has_none(client: TestClient) -> None:
    run_id = started(client, "none", {})
    assert view_grant(client, run_id) is None


def test_an_invalid_grant_is_refused_and_starts_nothing(client: TestClient) -> None:
    definition = register(client, WAITS)
    for bad in (
        grant({"fn": "tool", "tool": "get_model"}),
        grant({"fn": "queue_print"}, hours=24 * 7 + 1),
        grant({"fn": "arrange", "printers": [3]}),
    ):
        response = client.post(
            f"/api/v1/workflows/{definition['id']}/runs",
            headers={"Idempotency-Key": "bad"},
            json={"grant": bad},
        )
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["type"] == "https://scadbuddy.dev/problems/flow-grant-invalid"
        assert body["problems"]
    assert client.get("/api/v1/workflow-runs").json() == []


def test_an_agents_grant_is_requested_and_only_the_browser_decides_it(
    client: TestClient,
) -> None:
    run_id = started(client, "a", {"grant": grant({"fn": "queue_print"})}, **AGENT)
    shown = view_grant(client, run_id)
    assert (shown["status"], shown["in_force"]) == ("requested", False)
    assert shown["requested_by"] == {"kind": "agent", "principal": "token:p1", "session": "s1"}
    for action in ("approve", "decline"):
        refused = client.post(f"/api/v1/workflow-runs/{run_id}/grant/{action}", headers=AGENT)
        assert refused.status_code == 403
        assert refused.json()["type"] == "https://scadbuddy.dev/problems/flow-entry-browser-only"
    # The person edits it as they approve it: the request was only a proposal.
    edited = grant({"fn": "queue_print", "max_prints": 2}, ask_starter_session=True)
    approved = client.post(
        f"/api/v1/workflow-runs/{run_id}/grant/approve",
        json={"spec": edited, "ask_starter_session": False},
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert (body["status"], body["approved_by"]) == ("approved", "browser")
    assert body["spec"]["rules"][0]["max_prints"] == 2
    assert body["spec"]["ask_starter_session"] is False
    again = client.post(f"/api/v1/workflow-runs/{run_id}/grant/approve")
    assert again.status_code == 409
    assert again.json()["type"] == "https://scadbuddy.dev/problems/flow-grant-state"


def test_an_approval_rechecks_the_expiry(client: TestClient) -> None:
    run_id = started(client, "x", {"grant": grant({"fn": "queue_print"})}, **AGENT)
    late = grant({"fn": "queue_print"}, hours=24 * 8)
    response = client.post(f"/api/v1/workflow-runs/{run_id}/grant/approve", json={"spec": late})
    assert response.status_code == 422
    assert view_grant(client, run_id)["status"] == "requested"


def test_a_declined_grant_stays_declined(client: TestClient) -> None:
    run_id = started(client, "d", {"grant": grant({"fn": "queue_print"})}, **AGENT)
    assert client.post(f"/api/v1/workflow-runs/{run_id}/grant/decline").status_code == 200
    assert view_grant(client, run_id)["status"] == "declined"
    assert client.post(f"/api/v1/workflow-runs/{run_id}/grant/approve").status_code == 409


def test_the_starter_or_the_browser_revokes_once(client: TestClient) -> None:
    run_id = started(client, "r", {"grant": grant({"fn": "queue_print"})}, **AGENT)
    other = {"X-ScadBuddy-Agent-Author": "token:p2"}
    refused = client.post(f"/api/v1/workflow-runs/{run_id}/grant/revoke", headers=other)
    assert refused.status_code == 403
    revoked = client.post(f"/api/v1/workflow-runs/{run_id}/grant/revoke", headers=AGENT)
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["revoked_by"] == "agent token:p1" and body["in_force"] is False
    assert client.post(f"/api/v1/workflow-runs/{run_id}/grant/revoke").status_code == 409
    # Revoked is final: it is never approved after.
    assert client.post(f"/api/v1/workflow-runs/{run_id}/grant/approve").status_code == 409


def test_an_agent_cannot_revoke_a_browser_runs_grant(client: TestClient) -> None:
    run_id = started(client, "rb", {"grant": grant({"fn": "queue_print"})})
    response = client.post(f"/api/v1/workflow-runs/{run_id}/grant/revoke", headers=AGENT)
    assert response.status_code == 403
    assert client.post(f"/api/v1/workflow-runs/{run_id}/grant/revoke").status_code == 200


def test_grant_routes_for_a_run_with_no_grant_are_404(client: TestClient) -> None:
    run_id = started(client, "n", {})
    for action in ("approve", "decline", "revoke"):
        assert client.post(f"/api/v1/workflow-runs/{run_id}/grant/{action}").status_code == 404


def test_the_run_view_lists_the_grants_uses(client: TestClient, pg_conninfo: str) -> None:
    run_id = started(client, "u", {"grant": grant({"fn": "queue_print", "max_prints": 4})})
    grant_id = view_grant(client, run_id)["id"]
    with psycopg.connect(pg_conninfo) as conn:
        conn.execute(
            "INSERT INTO flow_grant_uses (request_id, grant_id, run_id, fn, input_hash,"
            " rule_index, prints) VALUES ('flow:x:y:z', %s, %s, 'queue_print', 'h', 0, 2)",
            (grant_id, run_id),
        )
        conn.execute(
            """UPDATE flow_grants SET used = '{"0": {"prints": 2, "filament_g": 0, "calls": 1}}'"""
            " WHERE id = %s",
            (grant_id,),
        )
    shown = view_grant(client, run_id)
    assert shown["used"] == {"0": {"prints": 2, "filament_g": 0, "calls": 1}}
    [use] = shown["uses"]
    assert (use["fn"], use["prints"], use["rule_index"]) == ("queue_print", 2, 0)
    assert "input_hash" not in use


def test_a_repeated_start_keeps_the_first_grant(client: TestClient) -> None:
    definition = register(client, WAITS)
    first = start(client, definition["id"], "same", grant=grant({"fn": "queue_print"}))
    run_id = first.json()["id"]
    until(client, run_id, lambda v: v["status"] == "waiting")
    again = start(client, definition["id"], "same", grant=grant({"fn": "arrange"}))
    assert again.status_code == 200
    assert view_grant(client, run_id)["spec"]["rules"][0]["fn"] == "queue_print"
