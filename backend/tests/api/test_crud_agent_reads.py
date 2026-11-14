"""The agent's reads of the backend's tables (#5299 D7): under
``/api/v1/internal/crud/agent/``, only for the ``agent`` service credential, each route
answers one question the agent asked over SQL before: the event log's head and its gap,
a flow run's starter, its decisions, its grant, its queue, and the calls waiting at its
starter session."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from tests.api.test_crud_agent_cost import token

BASE = "/api/v1/internal/crud/agent"
RUN = "0b0e3c55-6f1e-4a39-9d0e-000000000001"
OTHER = "0b0e3c55-6f1e-4a39-9d0e-000000000002"
EXEC = "e0000000-0000-4000-8000-000000000001"
SESSION = "5e550000-0000-4000-8000-000000000001"


def _pool(client: TestClient) -> ConnectionPool[Connection[DictRow]]:
    app: Any = client.app
    pool: ConnectionPool[Connection[DictRow]] = app.state.scadbuddy.settings_store.pool
    return pool


def _sql(client: TestClient, query: str, params: tuple[Any, ...] = ()) -> None:
    with _pool(client).connection() as conn:
        conn.execute(query, params)


def _definition(client: TestClient) -> None:
    _sql(
        client,
        "INSERT INTO workflow_definitions (id, name, version, created_by)"
        " VALUES ('def-1', 'reads', 1, '{}') ON CONFLICT DO NOTHING",
    )


def _run(
    client: TestClient,
    run_id: str = RUN,
    *,
    status: str = "waiting",
    started_by: dict[str, Any] | None = None,
    waiting_on: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
    superseded: list[str] | None = None,
    updated_at: datetime | None = None,
) -> None:
    _definition(client)
    _sql(
        client,
        "INSERT INTO workflow_runs (id, definition_id, version, name, status, waiting_on,"
        " steps, workflow_id, workflow_run_id, started_by, superseded_run_ids, updated_at)"
        " VALUES (%s, 'def-1', 1, 'reads', %s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()))",
        (
            run_id,
            status,
            json.dumps(waiting_on or []),
            json.dumps(steps or []),
            f"flow-{run_id}",
            EXEC,
            json.dumps(started_by or {"kind": "agent", "principal": "browser", "session": SESSION}),
            superseded or [],
            updated_at,
        ),
    )


def _grant(
    client: TestClient,
    grant_id: str = "g1",
    *,
    run_id: str = RUN,
    status: str = "approved",
    revoked: bool = False,
    expires_in: int = 3600,
) -> None:
    _sql(
        client,
        "INSERT INTO flow_grants (id, run_id, status, spec, requested_by, revoked_at,"
        " expires_at) VALUES (%s, %s, %s, '{}', '{}', CASE WHEN %s THEN now() END,"
        " now() + make_interval(secs => %s))",
        (grant_id, run_id, status, revoked, expires_in),
    )


def _decision(
    client: TestClient,
    request_id: str,
    *,
    responder: str = "browser",
    input_hash: str = "h1",
    response: dict[str, Any] | None = None,
) -> None:
    _sql(
        client,
        "INSERT INTO workflow_run_decisions (request_id, run_id, workflow_run_id, call_id,"
        " kind, outcome, response, responder, input_hash)"
        " VALUES (%s, %s, %s, 'c1', 'approval', 'approved', %s, %s, %s)",
        (request_id, RUN, EXEC, json.dumps(response or {}), responder, input_hash),
    )


def _event(client: TestClient, event_id: str, *, age_s: int = 0) -> None:
    payload = {"id": event_id, "type": "test.event"}
    _sql(
        client,
        "INSERT INTO events (event_id, kind, at, logged_at, payload)"
        " VALUES (%s, 'test.event', now(), now() - make_interval(secs => %s), %s)",
        (event_id, age_s, json.dumps(payload)),
    )


def _waiting(call_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "call_id": call_id,
        "kind": "approval",
        "fn": "queue_print",
        "since": "2026-11-14T10:00:00.123456+00:00",
        "history_length": 7,
        **extra,
    }


# -- the event log -------------------------------------------------------------------


def test_the_event_log_head_is_its_last_seq_and_the_database_clock(client: TestClient) -> None:
    agent = token(client)
    empty = client.get(f"{BASE}/events/head", headers=agent)
    assert empty.status_code == 200, empty.text
    assert empty.headers["x-scadbuddy-crud"] == "1"
    before = int(empty.json()["seq"])
    _event(client, "e-1")
    _event(client, "e-2")
    head = client.get(f"{BASE}/events/head", headers=agent).json()
    assert int(head["seq"]) == before + 2
    assert set(head) == {"seq", "at"}
    datetime.fromisoformat(head["at"])


def test_the_gap_reads_newest_first_after_a_seq_up_to_the_limit(client: TestClient) -> None:
    agent = token(client)
    start = int(client.get(f"{BASE}/events/head", headers=agent).json()["seq"])
    for n in range(4):
        _event(client, f"g-{n}", age_s=10 - n)
    page = client.get(f"{BASE}/events", params={"after": start + 1, "limit": 2}, headers=agent)
    assert page.status_code == 200, page.text
    body = page.json()
    assert [e["event_id"] for e in body["events"]] == ["g-3", "g-2"]
    assert [int(e["seq"]) for e in body["events"]] == [start + 4, start + 3]
    assert body["events"][0]["payload"] == {"id": "g-3", "type": "test.event"}
    assert datetime.fromisoformat(body["events"][1]["logged_at"]) < datetime.fromisoformat(
        body["read_at"]
    )
    every = client.get(f"{BASE}/events", params={"after": start}, headers=agent).json()
    assert len(every["events"]) == 4


def test_the_gap_read_is_bounded(client: TestClient) -> None:
    agent = token(client)
    for params in ({"after": -1}, {"after": 0, "limit": 0}, {"after": 0, "limit": 5002}):
        assert client.get(f"{BASE}/events", params=params, headers=agent).status_code == 422
    assert client.get(f"{BASE}/events", params={"after": 2**63}, headers=agent).status_code == 422


# -- a flow run's starter ------------------------------------------------------------


def test_a_runs_starter_says_whether_the_execution_is_open(client: TestClient) -> None:
    agent = token(client)
    _run(client, status="running", started_by={"kind": "browser", "principal": "user%3Aa"})
    url = f"{BASE}/flow-runs/{RUN}/starter"
    own = client.get(url, params={"execution": EXEC}, headers=agent)
    assert own.status_code == 200, own.text
    assert own.json() == {
        "run": {
            "open": True,
            "unrecorded": False,
            "started_by": {"kind": "browser", "principal": "user%3Aa", "session": None},
        }
    }
    # Another execution, neither the row's nor one a Reset replaced: open, unrecorded.
    other = client.get(url, params={"execution": "x"}, headers=agent).json()["run"]
    assert (other["open"], other["unrecorded"]) == (True, True)
    missing = client.get(
        f"{BASE}/flow-runs/{OTHER}/starter", params={"execution": EXEC}, headers=agent
    )
    assert missing.json() == {"run": None}


def test_a_closed_run_or_a_superseded_execution_is_not_open(client: TestClient) -> None:
    agent = token(client)
    _run(
        client,
        status="succeeded",
        superseded=["old"],
        started_by={"kind": "agent", "principal": 7, "session": SESSION},
    )
    url = f"{BASE}/flow-runs/{RUN}/starter"
    closed = client.get(url, params={"execution": EXEC}, headers=agent).json()["run"]
    assert (closed["open"], closed["unrecorded"]) == (False, False)
    # A field that is not a string is not passed on.
    assert closed["started_by"] == {"kind": "agent", "principal": None, "session": SESSION}
    superseded = client.get(url, params={"execution": "old"}, headers=agent).json()["run"]
    assert (superseded["open"], superseded["unrecorded"]) == (False, False)
    unrecorded = client.get(url, params={"execution": "new"}, headers=agent).json()["run"]
    assert (unrecorded["open"], unrecorded["unrecorded"]) == (False, True)


# -- decisions and grants -------------------------------------------------------------


def test_a_decision_check_answers_the_four_questions(client: TestClient) -> None:
    agent = token(client)
    _run(client)
    _grant(client, "live")
    _grant(client, "gone", run_id=OTHER, revoked=True)
    _decision(client, "flow:a", responder="browser")
    _decision(client, "flow:b", responder="grant:live")
    _decision(client, "flow:c", responder="grant:gone")
    _decision(client, "flow:d", responder="browser", response={"narrowed_by": "s"})
    url = f"{BASE}/flow-decisions"

    def check(request_id: str, input_hash: str = "h1") -> dict[str, bool]:
        answer = client.get(
            url, params={"request_id": request_id, "input_hash": input_hash}, headers=agent
        )
        assert answer.status_code == 200, answer.text
        result: dict[str, bool] = answer.json()
        return result

    assert check("flow:a") == {
        "approved": True,
        "approved_unresolved": False,
        "by_person": True,
        "lapsed": False,
    }
    assert check("flow:b") == {
        "approved": True,
        "approved_unresolved": True,
        "by_person": False,
        "lapsed": False,
    }
    assert check("flow:c") == {
        "approved": False,
        "approved_unresolved": False,
        "by_person": False,
        "lapsed": True,
    }
    assert check("flow:d")["approved_unresolved"] is True
    # Another hash, or no such decision: nothing holds.
    nothing = {"approved": False, "approved_unresolved": False, "by_person": False, "lapsed": False}
    assert check("flow:a", "h2") == nothing
    assert check("flow:none") == nothing


def test_a_grant_is_in_force_only_approved_unrevoked_and_unexpired(client: TestClient) -> None:
    agent = token(client)
    _run(client)
    _grant(client, "live")
    _grant(client, "revoked", run_id="r2", revoked=True)
    _grant(client, "expired", run_id="r3", expires_in=-1)
    _grant(client, "asked", run_id="r4", status="requested")
    url = f"{BASE}/flow-grants/in-force"
    for grant_id, held in (
        ("live", True),
        ("revoked", False),
        ("expired", False),
        ("asked", False),
        ("none", False),
        ("", False),
    ):
        answer = client.get(url, params={"grant_id": grant_id}, headers=agent)
        assert answer.status_code == 200, answer.text
        assert answer.json() == {"in_force": held}, grant_id


# -- a run's queue and the calls waiting on it ------------------------------------------


def test_a_runs_queue_is_its_parked_calls_steps_and_requested_grant(client: TestClient) -> None:
    agent = token(client)
    hops = [{"hop": "parent", "action": "parked", "actor": "flow", "at": "2026-11-14T10:00:00Z"}]
    _run(
        client,
        waiting_on=[_waiting("c1", hop="parent", hop_until="2026-11-14T11:00:00Z", hops=hops)],
        steps=[
            {"seq": 1, "fn": "queue_print", "call_id": "c1", "status": "running", "x": 1},
            {"seq": 2, "fn": "agent", "call_id": "c2", "status": "running", "session_id": "s"},
        ],
    )
    _grant(client, "asked", status="requested")
    answer = client.get(f"{BASE}/flow-runs/{RUN}/queue", headers=agent)
    assert answer.status_code == 200, answer.text
    run = answer.json()["run"]
    assert (run["id"], run["name"], run["status"], run["workflow_run_id"]) == (
        RUN,
        "reads",
        "waiting",
        EXEC,
    )
    assert run["starter_session"] == SESSION
    assert run["waiting_on"] == [
        {
            "call_id": "c1",
            "kind": "approval",
            "fn": "queue_print",
            "prompt": None,
            "since": "2026-11-14T10:00:00.123456+00:00",
            "hop": "parent",
            "hop_until": "2026-11-14T11:00:00Z",
            "reason": None,
            "proposed": False,
            "hops": [{**hops[0], "reason": None}],
        }
    ]
    assert run["steps"] == [
        {"seq": 1, "fn": "queue_print", "call_id": "c1", "status": "running", "session_id": None},
        {"seq": 2, "fn": "agent", "call_id": "c2", "status": "running", "session_id": "s"},
    ]
    assert run["requested_grant"]["id"] == "asked"
    datetime.fromisoformat(run["requested_grant"]["created_at"])
    assert client.get(f"{BASE}/flow-runs/{OTHER}/queue", headers=agent).json() == {"run": None}


def test_a_runs_steps_are_empty_for_no_run(client: TestClient) -> None:
    agent = token(client)
    _run(client, steps=[{"seq": 1, "fn": "agent", "call_id": "c1", "status": "succeeded"}])
    steps = client.get(f"{BASE}/flow-runs/{RUN}/steps", headers=agent).json()["steps"]
    assert [s["call_id"] for s in steps] == ["c1"]
    assert client.get(f"{BASE}/flow-runs/{OTHER}/steps", headers=agent).json() == {"steps": []}


def test_the_waiting_runs_are_the_open_runs_with_parked_calls(client: TestClient) -> None:
    agent = token(client)
    now = datetime.now(UTC)
    _run(client, RUN, waiting_on=[_waiting("c1")], updated_at=now - timedelta(minutes=1))
    _run(client, OTHER, status="running", waiting_on=[_waiting("c2")], updated_at=now)
    _run(client, "done", status="succeeded", waiting_on=[_waiting("c3")])
    _run(client, "idle", status="waiting")
    answer = client.get(f"{BASE}/flow-runs/waiting", headers=agent)
    assert answer.status_code == 200, answer.text
    runs = answer.json()["runs"]
    assert [r["id"] for r in runs] == [OTHER, RUN]
    assert [w["call_id"] for w in runs[1]["waiting_on"]] == ["c1"]
    datetime.fromisoformat(runs[0]["updated_at"])


def test_the_parent_hop_approvals_are_live_ones_paged_oldest_first(client: TestClient) -> None:
    agent = token(client)
    now = datetime.now(UTC)
    live = (now + timedelta(hours=1)).isoformat()
    past = (now - timedelta(hours=1)).isoformat()
    _run(
        client,
        RUN,
        waiting_on=[
            _waiting("c1", hop="parent", hop_until=live, reason="outside"),
            _waiting("c2", hop="parent", hop_until=past),
            _waiting("c3", hop="person"),
            _waiting("c4", hop="parent", hop_until="not a time"),
            _waiting("c5", hop="parent", kind="answer", hop_until=live),
        ],
        updated_at=now - timedelta(minutes=2),
    )
    _run(
        client,
        OTHER,
        started_by={"kind": "agent", "principal": "token:t", "session": SESSION},
        waiting_on=[_waiting("d1", hop="parent", hop_until=live)],
        updated_at=now - timedelta(minutes=1),
    )
    # Only a timed-out call: nothing to deliver.
    _run(client, "stale", waiting_on=[_waiting("e1", hop="parent", hop_until=past)])
    url = f"{BASE}/flow-runs/parent-hop-approvals"
    first = client.get(url, params={"limit": 1}, headers=agent)
    assert first.status_code == 200, first.text
    page = first.json()
    assert [r["id"] for r in page["runs"]] == [RUN]
    run = page["runs"][0]
    assert [c["call_id"] for c in run["calls"]] == ["c1"]
    assert run["calls"][0]["reason"] == "outside"
    assert (run["session"], run["principal"], run["workflow_run_id"]) == (
        SESSION,
        "browser",
        EXEC,
    )
    assert page["next"] is not None
    rest = client.get(url, params={"limit": 1, **page["next"]}, headers=agent).json()
    assert [r["id"] for r in rest["runs"]] == [OTHER]
    assert rest["runs"][0]["principal"] == "token:t"
    last = client.get(url, params={"limit": 1, **rest["next"]}, headers=agent).json()
    assert last == {"runs": [], "next": None}


def test_still_at_parent_hop_names_the_calls_still_waiting_there(client: TestClient) -> None:
    agent = token(client)
    _run(client, RUN, waiting_on=[_waiting("c1", hop="parent"), _waiting("c2", hop="person")])
    _run(client, OTHER, status="running", waiting_on=[_waiting("d1", hop="parent")])
    ids = [
        f"flow:{RUN}:{EXEC}:c1",
        f"flow:{RUN}:{EXEC}:c2",
        f"flow:{RUN}:other-execution:c1",
        f"flow:{OTHER}:{EXEC}:d1",
        "flow:gone",
        "durable:x",
    ]
    answer = client.post(
        f"{BASE}/flow-runs/parent-hop/waiting", json={"request_ids": ids}, headers=agent
    )
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"waiting": [f"flow:{RUN}:{EXEC}:c1"]}


def test_still_at_parent_hop_is_bounded(client: TestClient) -> None:
    agent = token(client)
    url = f"{BASE}/flow-runs/parent-hop/waiting"
    for body in (
        {"request_ids": ["flow:x"] * 501},
        {"request_ids": ["x" * 1025]},
        {"request_ids": [], "extra": 1},
    ):
        assert client.post(url, json=body, headers=agent).status_code == 422
    assert client.post(url, json={"request_ids": []}, headers=agent).json() == {"waiting": []}


def test_the_inputs_are_bounded(client: TestClient) -> None:
    agent = token(client)
    long = "x" * 1025
    for path, params in (
        (f"/flow-runs/{RUN}/starter", {"execution": long}),
        (f"/flow-runs/{RUN}/starter", {}),
        ("/flow-decisions", {"request_id": long, "input_hash": "h"}),
        ("/flow-decisions", {"request_id": "flow:a", "input_hash": "h" * 257}),
        ("/flow-grants/in-force", {"grant_id": long}),
        (f"/flow-runs/{'r' * 201}/queue", {}),
        ("/flow-runs/parent-hop-approvals", {"limit": 201}),
        ("/flow-runs/parent-hop-approvals", {"after_id": "x"}),
    ):
        answer = client.get(f"{BASE}{path}", params=params, headers=agent)
        assert answer.status_code == 422, (path, params, answer.text)


def test_only_the_agent_credential_reaches_them(client: TestClient) -> None:
    for path in (
        "/events/head",
        "/events?after=0",
        f"/flow-runs/{RUN}/starter?execution=x",
        "/flow-decisions?request_id=flow:a&input_hash=h",
        "/flow-grants/in-force?grant_id=g",
        f"/flow-runs/{RUN}/queue",
        f"/flow-runs/{RUN}/steps",
        "/flow-runs/waiting",
        "/flow-runs/parent-hop-approvals",
    ):
        assert client.get(f"{BASE}{path}").status_code == 401, path
        assert client.get(f"{BASE}{path}", headers=token(client, "print")).status_code == 403
    url = f"{BASE}/flow-runs/parent-hop/waiting"
    assert client.post(url, json={"request_ids": []}).status_code == 401
    assert (
        client.post(url, json={"request_ids": []}, headers=token(client, "print")).status_code
        == 403
    )


def test_the_agent_areas_list_the_reads(client: TestClient) -> None:
    areas = client.get(f"{BASE}/areas", headers=token(client)).json()["areas"]
    assert {"events", "flows"} <= set(areas)
