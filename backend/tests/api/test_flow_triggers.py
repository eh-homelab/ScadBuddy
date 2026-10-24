"""Flow triggers through the routes (#2484, spec 2026-10-11 §12): a schedule is a
Temporal Schedule whose fire starts a normal run as the trigger's creator; an event
trigger fires once per event; a run carries a grant only from a standing grant a person
approved; who made or changed a trigger, and its grant, is the server's record."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from temporalio.client import Client, ScheduleActionStartWorkflow, ScheduleOverlapPolicy
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.api import flow_triggers as routes
from scadbuddy.api.deps import STATE_ATTR, transactional_events
from scadbuddy.bambuddy.follow import Follower
from scadbuddy.bambuddy.outcomes import PrintOutcomeStore
from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.events import EventBus, JobEvent, VersionCommitted
from scadbuddy.flow_triggers.component import FLOW_TRIGGERS
from scadbuddy.flow_triggers.fire import ensure_trigger_schedules
from scadbuddy.workflows.flow_triggers import schedule_id
from tests.api.conftest import FakeAgentTools
from tests.api.test_flows import AGENT, register, script, settings, until
from tests.support.temporal import WorkflowReaper

__all__ = ["settings"]

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

OTHER = {"X-ScadBuddy-Agent-Author": "token:p2", "X-ScadBuddy-Agent-Author-Session": "s2"}
WAITS = script("await wait_for_human('Go on?', 600)")
QUICK = script("return 1")
DAILY = {"interval_s": 86400}


@pytest.fixture
def triggers(client: TestClient, workflow_reaper: WorkflowReaper) -> Iterator[None]:
    """Every trigger's Schedule goes with the test: one left would fire on a queue
    nobody serves."""
    yield
    for trigger in client.get("/api/v1/flow-triggers", params={"limit": 200}).json():
        workflow_reaper.delete_schedules(schedule_id(trigger["id"]))


def grant(*rules: dict[str, Any], days: float = 7, **fields: Any) -> dict[str, Any]:
    expires = datetime.now(UTC) + timedelta(days=days)
    return {"expires_at": expires.isoformat(), "rules": list(rules), **fields}


def create(
    client: TestClient, definition_id: str, headers: dict[str, str] | None = None, **body: Any
) -> dict[str, Any]:
    body = {"definition_id": definition_id, "schedule": DAILY, **body}
    response = client.post("/api/v1/flow-triggers", json=body, headers=headers or {})
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def view(client: TestClient, trigger_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/flow-triggers/{trigger_id}")
    assert response.status_code == 200, response.text
    shown: dict[str, Any] = response.json()
    return shown


def attach(
    client: TestClient, trigger_id: str, spec: dict[str, Any], headers: dict[str, str] | None = None
) -> Any:
    """Attach a standing grant, bound to the trigger as the caller last saw it."""
    body = {"spec": spec, "seen": view(client, trigger_id)["seen"]}
    return client.post(
        f"/api/v1/flow-triggers/{trigger_id}/grant", json=body, headers=headers or {}
    )


def approve(client: TestClient, trigger_id: str, spec: dict[str, Any] | None = None) -> Any:
    body: dict[str, Any] = {"seen": view(client, trigger_id)["seen"]}
    if spec is not None:
        body["spec"] = spec
    return client.post(f"/api/v1/flow-triggers/{trigger_id}/grant/approve", json=body)


def fire(reaper: WorkflowReaper, client: TestClient, trigger_id: str) -> dict[str, Any]:
    """Trigger the Schedule now, and the fire it made once it is decided."""
    before = len(view(client, trigger_id)["fires"])
    temporal = reaper.client
    assert isinstance(temporal, Client)
    handle = temporal.get_schedule_handle(schedule_id(trigger_id))
    reaper.run(handle.trigger(overlap=ScheduleOverlapPolicy.ALLOW_ALL))
    return decided(client, trigger_id, before + 1)


def decided(client: TestClient, trigger_id: str, count: int, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        fires = view(client, trigger_id)["fires"]
        if len(fires) >= count and all(f["outcome"] not in ("pending", "admitted") for f in fires):
            newest: dict[str, Any] = fires[0]
            return newest
        if time.monotonic() > deadline:
            raise AssertionError(f"no fire decided: {fires}")
        time.sleep(0.2)


def run_view(client: TestClient, run_id: str) -> dict[str, Any]:
    return until(client, run_id, lambda v: v["status"] not in ("starting",))


@pytest.mark.usefixtures("triggers")
def test_a_schedule_fires_a_run_as_its_browser_creator(
    client: TestClient, workflow_reaper: WorkflowReaper
) -> None:
    definition = register(client, QUICK)
    trigger = create(
        client, definition["id"], schedule={"cron": ["0 9 * * 1"], "time_zone": "Europe/London"}
    )
    assert trigger["created_by"] == {"kind": "browser"}
    assert trigger["version"] == definition["version"] and trigger["paused"] is False
    shown = view(client, trigger["id"])
    assert shown["next_fires"], "the Schedule names its next fire"
    fired = fire(workflow_reaper, client, trigger["id"])
    assert fired["outcome"] == "started", fired
    run = until(client, fired["run_id"], lambda v: v["status"] == "succeeded")
    assert run["run"]["started_by"] == {"kind": "browser"}
    assert run["trigger"]["trigger_id"] == trigger["id"]
    assert run["trigger"]["kind"] == "schedule"
    assert run["grant"] is None


@pytest.mark.usefixtures("triggers")
def test_an_agents_trigger_runs_as_the_agent_without_its_session(
    client: TestClient, workflow_reaper: WorkflowReaper
) -> None:
    definition = register(client, QUICK)
    trigger = create(client, definition["id"], AGENT)
    assert trigger["created_by"] == {"kind": "agent", "principal": "token:p1", "session": "s1"}
    fired = fire(workflow_reaper, client, trigger["id"])
    run = until(client, fired["run_id"], lambda v: v["status"] == "succeeded")
    assert run["run"]["started_by"] == {"kind": "agent", "principal": "token:p1"}


@pytest.mark.usefixtures("triggers")
def test_the_body_never_says_who_a_trigger_runs_as(client: TestClient) -> None:
    definition = register(client, QUICK)
    for forged in (
        {"created_by": {"kind": "browser"}},
        {"started_by": {"kind": "browser"}},
        {"grant": grant({"fn": "queue_print"}), "grant_status": "approved"},
    ):
        response = client.post(
            "/api/v1/flow-triggers",
            json={"definition_id": definition["id"], "schedule": DAILY, **forged},
            headers=AGENT,
        )
        assert response.status_code == 422, response.text
    assert client.get("/api/v1/flow-triggers").json() == []


@pytest.mark.usefixtures("triggers")
def test_a_trigger_is_checked_when_it_is_made(client: TestClient) -> None:
    typed = register(
        client,
        script("return (await inputs())['copies']"),
        name="typed",
        inputs_schema={
            "type": "object",
            "properties": {"copies": {"type": "integer"}},
            "required": ["copies"],
        },
    )
    for body in (
        {"definition_id": typed["id"], "schedule": DAILY},
        {"definition_id": typed["id"], "schedule": DAILY, "inputs": {"copies": "two"}},
        {"definition_id": typed["id"], "schedule": {"interval_s": 5}, "inputs": {"copies": 1}},
        {
            "definition_id": typed["id"],
            "schedule": {"cron": ["not a cron"]},
            "inputs": {"copies": 1},
        },
        {
            "definition_id": typed["id"],
            "schedule": {**DAILY, "time_zone": "Mars/Base"},
            "inputs": {"copies": 1},
        },
        {
            "definition_id": typed["id"],
            "event": {"kind": "print.exploded"},
            "inputs": {"copies": 1},
        },
        {"definition_id": typed["id"], "inputs": {"copies": 1}},
    ):
        assert client.post("/api/v1/flow-triggers", json=body).status_code == 422, body
    missing = client.post(
        "/api/v1/flow-triggers", json={"definition_id": "nope", "schedule": DAILY}
    )
    assert missing.status_code == 404
    made = create(client, typed["id"], inputs={"copies": 2})
    assert made["inputs"] == {"copies": 2}


@pytest.mark.usefixtures("triggers")
def test_a_trigger_pins_its_version(client: TestClient, workflow_reaper: WorkflowReaper) -> None:
    one = register(client, script("return 'one'"))
    trigger = create(client, one["id"])
    register(client, script("return 'two'"))
    fired = fire(workflow_reaper, client, trigger["id"])
    run = until(client, fired["run_id"], lambda v: v["status"] == "succeeded")
    assert (run["version"], run["run"]["result"]) == (1, "result: 'one'")


@pytest.mark.usefixtures("triggers")
def test_a_fire_without_a_standing_grant_carries_none(
    client: TestClient, workflow_reaper: WorkflowReaper
) -> None:
    definition = register(client, WAITS)
    trigger = create(client, definition["id"])
    fired = fire(workflow_reaper, client, trigger["id"])
    assert run_view(client, fired["run_id"])["grant"] is None


@pytest.mark.usefixtures("triggers")
def test_a_fire_under_an_approved_standing_grant_gets_a_run_grant_from_it(
    client: TestClient, workflow_reaper: WorkflowReaper
) -> None:
    definition = register(client, WAITS)
    trigger = create(client, definition["id"])
    attached = attach(
        client,
        trigger["id"],
        grant({"fn": "queue_print", "printers": [3], "max_prints": 2}, days=20),
    )
    assert attached.status_code == 200, attached.text
    standing = attached.json()
    assert (standing["status"], standing["approved_by"], standing["in_force"]) == (
        "approved",
        "browser",
        True,
    )
    fired = fire(workflow_reaper, client, trigger["id"])
    run_grant = run_view(client, fired["run_id"])["grant"]
    assert run_grant["status"] == "approved" and run_grant["in_force"] is True
    assert run_grant["approved_by"] == f"trigger-grant:{standing['id']}"
    assert run_grant["spec"]["rules"][0]["printers"] == [3]
    assert run_grant["spec"]["ask_starter_session"] is False
    # A run grant never outlives §3.1's 7 days, whatever the standing grant's expiry.
    expires = datetime.fromisoformat(run_grant["expires_at"])
    assert expires <= datetime.now(UTC) + timedelta(days=7)


@pytest.mark.usefixtures("triggers")
def test_an_agents_standing_grant_is_requested_and_only_the_browser_decides_it(
    client: TestClient, workflow_reaper: WorkflowReaper
) -> None:
    definition = register(client, WAITS)
    trigger = create(client, definition["id"], AGENT, overlap="allow_all")
    requested = attach(client, trigger["id"], grant({"fn": "queue_print"}), AGENT)
    assert requested.status_code == 200, requested.text
    assert (requested.json()["status"], requested.json()["in_force"]) == ("requested", False)
    assert requested.json()["requested_by"] == {
        "kind": "agent",
        "principal": "token:p1",
        "session": "s1",
    }
    # Requested only: a fire meanwhile carries no grant.
    fired = fire(workflow_reaper, client, trigger["id"])
    assert run_view(client, fired["run_id"])["grant"] is None
    for action in ("approve", "decline"):
        refused = client.post(
            f"/api/v1/flow-triggers/{trigger['id']}/grant/{action}", headers=AGENT
        )
        assert refused.status_code == 403
        assert refused.json()["type"] == "https://scadbuddy.dev/problems/flow-grant-browser-only"
    approved = approve(client, trigger["id"], grant({"fn": "queue_print", "max_prints": 1}))
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["spec"]["rules"][0]["max_prints"] == 1
    again = fire(workflow_reaper, client, trigger["id"])
    assert run_view(client, again["run_id"])["grant"]["status"] == "approved"


@pytest.mark.usefixtures("triggers")
def test_a_standing_grant_expires_within_30_days(client: TestClient) -> None:
    definition = register(client, WAITS)
    trigger = create(client, definition["id"])
    too_long = attach(client, trigger["id"], grant({"fn": "queue_print"}, days=31))
    assert too_long.status_code == 422
    assert too_long.json()["type"] == "https://scadbuddy.dev/problems/flow-grant-invalid"
    ok = attach(client, trigger["id"], grant({"fn": "queue_print"}, days=29))
    assert ok.status_code == 200, ok.text
    # One live standing grant per trigger.
    second = attach(client, trigger["id"], grant({"fn": "queue_print"}))
    assert second.status_code == 409


@pytest.mark.usefixtures("triggers")
def test_another_principal_cannot_change_a_trigger(client: TestClient) -> None:
    definition = register(client, WAITS)
    mine = create(client, definition["id"], AGENT)
    base = f"/api/v1/flow-triggers/{mine['id']}"
    attempts = [
        ("PATCH", base, {"schedule": {"interval_s": 3600}}),
        ("POST", f"{base}/pause", None),
        ("POST", f"{base}/resume", None),
        ("POST", f"{base}/grant", {"spec": grant({"fn": "queue_print"})}),
        ("POST", f"{base}/grant/revoke", None),
        ("DELETE", base, None),
    ]
    for method, url, body in attempts:
        response = client.request(method, url, json=body, headers=OTHER)
        assert response.status_code == 403, (method, url, response.text)
        assert response.json()["type"] == "https://scadbuddy.dev/problems/flow-trigger-creator-only"
    # Nor can an agent change one the browser made.
    browsers = create(client, definition["id"])
    refused = client.post(f"/api/v1/flow-triggers/{browsers['id']}/resume", headers=AGENT)
    assert refused.status_code == 403
    # Its creator can.
    paused = client.post(f"{base}/pause", headers=AGENT)
    assert paused.status_code == 200 and paused.json()["paused"] is True
    assert paused.json()["paused_by"] == {"kind": "agent", "principal": "token:p1", "session": "s1"}


@pytest.mark.usefixtures("triggers")
def test_an_edit_returns_the_standing_grant_to_requested(
    client: TestClient, workflow_reaper: WorkflowReaper
) -> None:
    definition = register(client, WAITS)
    trigger = create(client, definition["id"], overlap="allow_all")
    assert attach(client, trigger["id"], grant({"fn": "queue_print"})).status_code == 200
    fired = fire(workflow_reaper, client, trigger["id"])
    edited = client.patch(
        f"/api/v1/flow-triggers/{trigger['id']}", json={"schedule": {"interval_s": 7200}}
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["schedule"]["interval_s"] == 7200
    assert body["updated_by"] == {"kind": "browser"}
    assert (body["grant"]["status"], body["grant"]["in_force"]) == ("requested", False)
    # A run already started keeps its run grant's row (it spends only under the approval
    # that seeded it, which the edit ended).
    assert run_view(client, fired["run_id"])["grant"]["in_force"] is True
    # The next fire gets none until a person approves it again.
    later = fire(workflow_reaper, client, trigger["id"])
    assert run_view(client, later["run_id"])["grant"] is None
    assert approve(client, trigger["id"]).json()["in_force"] is True
    # A pause and a resume leave it alone.
    client.post(f"/api/v1/flow-triggers/{trigger['id']}/pause")
    client.post(f"/api/v1/flow-triggers/{trigger['id']}/resume")
    assert view(client, trigger["id"])["grant"]["in_force"] is True


@pytest.mark.usefixtures("triggers")
def test_an_edit_temporal_refuses_saves_nothing(
    client: TestClient, workflow_reaper: WorkflowReaper
) -> None:
    """#2729 review: the Schedule is set before the row is saved. A cron our check lets
    through but Temporal refuses is a 422 that leaves the row, the Schedule and the
    approval as they were."""
    definition = register(client, WAITS)
    trigger = create(client, definition["id"])
    assert attach(client, trigger["id"], grant({"fn": "queue_print"})).status_code == 200
    before = view(client, trigger["id"])
    assert before["grant"]["in_force"] is True
    temporal = workflow_reaper.client
    assert isinstance(temporal, Client)
    handle = temporal.get_schedule_handle(schedule_id(trigger["id"]))
    spec = workflow_reaper.run(handle.describe()).schedule.spec
    refused = client.patch(
        f"/api/v1/flow-triggers/{trigger['id']}", json={"schedule": {"cron": ["99 * * * *"]}}
    )
    assert refused.status_code == 422, refused.text
    after = view(client, trigger["id"])
    assert after["schedule"] == before["schedule"]
    assert after["updated_at"] == before["updated_at"]
    assert (after["grant"]["status"], after["grant"]["in_force"]) == ("approved", True)
    assert workflow_reaper.run(handle.describe()).schedule.spec == spec


@pytest.mark.usefixtures("triggers")
def test_a_pause_or_resume_saved_while_temporal_is_away_answers_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2729 review: the row took effect, so the route does not say "try again"; the
    worker's reconcile puts the Schedule right."""
    definition = register(client, QUICK)
    trigger = create(client, definition["id"])

    async def away(*_: Any) -> None:
        raise RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")

    monkeypatch.setattr(routes, "ensure_trigger_schedule", away)
    paused = client.post(f"/api/v1/flow-triggers/{trigger['id']}/pause")
    assert paused.status_code == 200, paused.text
    assert view(client, trigger["id"])["paused"] is True
    resumed = client.post(f"/api/v1/flow-triggers/{trigger['id']}/resume")
    assert resumed.status_code == 200, resumed.text
    assert view(client, trigger["id"])["paused"] is False
    # An edit is still checked first: with Temporal away it saves nothing (503).
    edited = client.patch(f"/api/v1/flow-triggers/{trigger['id']}", json={"overlap": "allow_all"})
    assert edited.status_code == 503, edited.text
    assert view(client, trigger["id"])["overlap"] == trigger["overlap"]


@pytest.mark.usefixtures("triggers")
def test_the_list_builds_a_few_views_at_once_in_order(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2729 review: up to 200 triggers, each a Temporal describe and three queries, are
    not read one after another; at most `LIST_VIEWS_AT_ONCE` at a time."""
    definition = register(client, QUICK)
    made = [create(client, definition["id"])["id"] for _ in range(12)]
    real = routes._view
    running = peak = 0

    async def counted(triggers: Any, trigger: Any) -> Any:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return await real(triggers, trigger)

    monkeypatch.setattr(routes, "_view", counted)
    listed = client.get("/api/v1/flow-triggers", params={"limit": 200})
    assert listed.status_code == 200, listed.text
    assert [t["id"] for t in listed.json()] == list(reversed(made))
    assert 1 < peak <= routes.LIST_VIEWS_AT_ONCE


@pytest.mark.usefixtures("triggers")
def test_an_approval_is_bound_to_what_the_person_saw(client: TestClient) -> None:
    definition = register(client, WAITS)
    other = register(client, script("await wait_for_human('Other?', 600)"), name="other")
    trigger = create(client, definition["id"], AGENT)
    assert attach(client, trigger["id"], grant({"fn": "queue_print"}), AGENT).status_code == 200
    seen = view(client, trigger["id"])["seen"]
    for change in (
        {"definition_id": other["id"]},
        {"schedule": {"interval_s": 600}},
        {"approval_timeout": 3600},
    ):
        # The creator changes the trigger between the person's look and their click.
        assert (
            client.patch(
                f"/api/v1/flow-triggers/{trigger['id']}", json=change, headers=AGENT
            ).status_code
            == 200
        )
        stale = client.post(
            f"/api/v1/flow-triggers/{trigger['id']}/grant/approve", json={"seen": seen}
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["type"] == "https://scadbuddy.dev/problems/flow-trigger-changed"
        seen = view(client, trigger["id"])["seen"]
    # Nor may the approval come without saying what it saw.
    blind = client.post(f"/api/v1/flow-triggers/{trigger['id']}/grant/approve", json={})
    assert blind.status_code == 422
    assert view(client, trigger["id"])["grant"]["status"] == "requested"
    # A browser attach is approved as sent, so it is bound the same way.
    browsers = create(client, definition["id"])
    seen_browser = view(client, browsers["id"])["seen"]
    client.patch(f"/api/v1/flow-triggers/{browsers['id']}", json={"schedule": {"interval_s": 600}})
    refused = client.post(
        f"/api/v1/flow-triggers/{browsers['id']}/grant",
        json={"spec": grant({"fn": "queue_print"}), "seen": seen_browser},
    )
    assert refused.status_code == 409
    # Seen as it is now, it is approved.
    assert approve(client, trigger["id"]).status_code == 200


@pytest.mark.usefixtures("triggers")
def test_revoking_the_standing_grant_revokes_the_run_grants_it_seeded(
    client: TestClient, workflow_reaper: WorkflowReaper
) -> None:
    definition = register(client, WAITS)
    trigger = create(client, definition["id"], AGENT, overlap="allow_all")
    attach(client, trigger["id"], grant({"fn": "queue_print"}))
    fired = fire(workflow_reaper, client, trigger["id"])
    assert run_view(client, fired["run_id"])["grant"]["in_force"] is True
    # Its creator may revoke it: revoking only takes authority away.
    revoked = client.post(f"/api/v1/flow-triggers/{trigger['id']}/grant/revoke", headers=AGENT)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_by"] == "agent token:p1"
    run_grant = run_view(client, fired["run_id"])["grant"]
    assert run_grant["in_force"] is False and run_grant["revoked_at"] is not None
    later = fire(workflow_reaper, client, trigger["id"])
    assert run_view(client, later["run_id"])["grant"] is None


@pytest.mark.usefixtures("triggers")
@pytest.mark.parametrize("action", ["archive", "delete"])
def test_archiving_or_deleting_the_flow_pauses_its_triggers(
    client: TestClient, workflow_reaper: WorkflowReaper, action: str
) -> None:
    definition = register(client, WAITS, name="paused-flow")
    trigger = create(client, definition["id"])
    if action == "archive":
        response = client.post("/api/v1/workflows/names/paused-flow/archive")
    else:
        response = client.delete("/api/v1/workflows/names/paused-flow")
    assert response.status_code == 200, response.text
    shown = view(client, trigger["id"])
    reason = "flow archived" if action == "archive" else "flow deleted"
    assert (shown["paused"], shown["paused_reason"]) == (True, reason)
    temporal = workflow_reaper.client
    assert isinstance(temporal, Client)
    described = workflow_reaper.run(
        temporal.get_schedule_handle(schedule_id(trigger["id"])).describe()
    )
    assert described.schedule.state.paused is True
    resumed = client.post(f"/api/v1/flow-triggers/{trigger['id']}/resume")
    assert resumed.status_code == 409
    # A fire that reaches it anyway starts nothing.
    fired = fire(workflow_reaper, client, trigger["id"])
    assert fired["outcome"] == "skipped"


@pytest.mark.usefixtures("triggers")
def test_a_stale_schedule_never_fires_an_event_trigger(
    app: FastAPI, client: TestClient, workflow_reaper: WorkflowReaper
) -> None:
    """A schedule trigger edited into an event trigger while Temporal was away keeps its
    old Schedule: its fires start nothing, and the next reconcile removes it."""
    definition = register(client, QUICK)
    trigger = create(client, definition["id"], overlap="allow_all")
    temporal = workflow_reaper.client
    assert isinstance(temporal, Client)
    handle = temporal.get_schedule_handle(schedule_id(trigger["id"]))
    described = workflow_reaper.run(handle.describe())
    action = described.schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    queue = action.task_queue
    # The row becomes an event trigger, but the Schedule's delete never happened.
    store = getattr(app.state, STATE_ATTR).components.get(FLOW_TRIGGERS).store
    workflow_reaper.run(
        store.update(
            trigger["id"],
            {"kind": "event", "schedule": None, "event": {"kind": "model.changed"}},
            {"kind": "browser"},
            "edit",
        )
    )
    workflow_reaper.run(handle.trigger(overlap=ScheduleOverlapPolicy.ALLOW_ALL))
    fired = decided(client, trigger["id"], 1)
    assert (fired["outcome"], fired["reason"]) == ("skipped", "not a schedule trigger")
    # The worker's reconcile removes the Schedule of every non-schedule trigger.
    workflow_reaper.run(ensure_trigger_schedules(temporal, queue, store))
    assert not workflow_reaper.run(_schedule_exists(temporal, schedule_id(trigger["id"])))


@pytest.mark.usefixtures("triggers")
def test_a_fire_waits_for_the_agents_tools_and_admits_nothing_meanwhile(
    client: TestClient, workflow_reaper: WorkflowReaper, agent_tools: FakeAgentTools
) -> None:
    """#2704: a run starts with its tools' tiers as the agent says them now. With the
    agent unreachable the fire is retried, never started on a guess."""
    definition = register(client, QUICK)
    trigger = create(client, definition["id"])
    agent_tools.down = True
    before = len(view(client, trigger["id"])["fires"])
    temporal = workflow_reaper.client
    assert isinstance(temporal, Client)
    handle = temporal.get_schedule_handle(schedule_id(trigger["id"]))
    workflow_reaper.run(handle.trigger(overlap=ScheduleOverlapPolicy.ALLOW_ALL))
    deadline = time.monotonic() + 30
    while len(view(client, trigger["id"])["fires"]) <= before:
        assert time.monotonic() < deadline, "the fire was never claimed"
        time.sleep(0.2)
    time.sleep(3)
    assert view(client, trigger["id"])["fires"][0]["outcome"] == "pending"
    assert client.get("/api/v1/workflow-runs").json() == []
    agent_tools.down = False
    fired = decided(client, trigger["id"], before + 1, timeout=60)
    assert fired["outcome"] == "started", fired


def test_a_standing_grant_is_refused_while_the_agent_cannot_say_its_tools(
    client: TestClient, agent_tools: FakeAgentTools
) -> None:
    definition = register(client, QUICK)
    trigger = create(client, definition["id"])
    agent_tools.down = True
    attached = attach(client, trigger["id"], grant({"fn": "queue_print", "printers": [3]}))
    assert attached.status_code == 503, attached.text


@pytest.mark.usefixtures("triggers")
def test_a_paused_trigger_fires_nothing(
    client: TestClient, workflow_reaper: WorkflowReaper
) -> None:
    definition = register(client, QUICK)
    trigger = create(client, definition["id"])
    client.post(f"/api/v1/flow-triggers/{trigger['id']}/pause")
    fired = fire(workflow_reaper, client, trigger["id"])
    assert (fired["outcome"], fired["reason"]) == ("skipped", "paused")
    assert client.get("/api/v1/workflow-runs").json() == []


@pytest.mark.usefixtures("triggers")
def test_overlap_skip_waits_for_the_previous_run(
    client: TestClient, workflow_reaper: WorkflowReaper
) -> None:
    definition = register(client, WAITS)
    skip = create(client, definition["id"])
    first = fire(workflow_reaper, client, skip["id"])
    until(client, first["run_id"], lambda v: v["status"] == "waiting")
    second = fire(workflow_reaper, client, skip["id"])
    assert (second["outcome"], second["reason"]) == ("skipped", "overlap")
    allow = create(client, definition["id"], overlap="allow_all")
    fire(workflow_reaper, client, allow["id"])
    assert fire(workflow_reaper, client, allow["id"])["outcome"] == "started"


@pytest.fixture
def bus(app: FastAPI) -> EventBus:
    events: EventBus = getattr(app.state, STATE_ATTR).events
    return events


@pytest.mark.usefixtures("triggers")
def test_an_event_trigger_fires_once_per_event(client: TestClient, bus: EventBus) -> None:
    definition = register(client, QUICK)
    changed = create(
        client,
        definition["id"],
        AGENT,
        schedule=None,
        event={"kind": "model.changed", "slug": "coaster"},
    )
    assert changed["next_fires"] == []
    rendered = create(client, definition["id"], schedule=None, event={"kind": "render.finished"})
    # Another template, and another kind of event: nothing.
    bus.publish(VersionCommitted(slug="other", commit="c0"))
    bus.publish(JobEvent(kind="job.failed", job_id="j0", slug="coaster"))
    # Heard twice (two replicas, or a repeat): one fire.
    bus.publish(VersionCommitted(slug="coaster", commit="c1"))
    bus.publish(VersionCommitted(slug="coaster", commit="c1"))
    bus.publish(JobEvent(kind="job.done", job_id="j1", slug="anything"))
    fired = decided(client, changed["id"], 1)
    assert fired["outcome"] == "started"
    assert fired["event"] == {"kind": "model.changed", "slug": "coaster", "commit": "c1"}
    run = until(client, fired["run_id"], lambda v: v["status"] == "succeeded")
    assert run["run"]["started_by"] == {"kind": "agent", "principal": "token:p1"}
    assert run["trigger"]["kind"] == "event"
    assert decided(client, rendered["id"], 1)["event"]["job_id"] == "j1"
    time.sleep(1)
    assert len(view(client, changed["id"])["fires"]) == 1
    assert len(view(client, rendered["id"])["fires"]) == 1


READS_EVENT = script(
    "t = await trigger()",
    "if t is None:",
    "    return 'none'",
    "if t['kind'] == 'model.changed':",
    "    return [t['kind'], t['slug'], t['commit']]",
    "if t['kind'] == 'render.finished':",
    "    return [t['kind'], t['slug'], t['job_id']]",
    "return [t['kind'], t['slug'], t['run_id']]",
)


@pytest.mark.usefixtures("triggers")
def test_a_run_reads_the_event_that_started_it(
    client: TestClient, bus: EventBus, workflow_reaper: WorkflowReaper
) -> None:
    """#2666: an event's run reads it with `trigger()`; a scheduled run reads None."""
    definition = register(client, READS_EVENT)
    changed = create(
        client, definition["id"], schedule=None, event={"kind": "model.changed", "slug": "box"}
    )
    rendered = create(client, definition["id"], schedule=None, event={"kind": "render.finished"})
    scheduled = create(client, definition["id"])
    bus.publish(VersionCommitted(slug="box", commit="c9"))
    bus.publish(JobEvent(kind="job.done", job_id="j9", slug="lid"))
    results = {}
    for trigger, fired in (
        (changed, decided(client, changed["id"], 1)),
        (rendered, decided(client, rendered["id"], 1)),
        (scheduled, fire(workflow_reaper, client, scheduled["id"])),
    ):
        assert fired["outcome"] == "started", fired
        run = until(client, fired["run_id"], lambda v: v["status"] == "succeeded")
        results[trigger["id"]] = run["run"]["result"]
    assert results[changed["id"]] == "result: ['model.changed', 'box', 'c9']"
    assert results[rendered["id"]] == "result: ['render.finished', 'lid', 'j9']"
    assert results[scheduled["id"]] == "result: 'none'"


READS_PRINT = script(
    "t = await trigger()",
    "if t is not None and (t['kind'] == 'print.finished' or t['kind'] == 'print.failed'):",
    "    return [t['kind'], t['slug'], t['run_id'], t['output_id']]",
    "return 'none'",
)


@pytest.mark.usefixtures("triggers")
def test_one_print_failing_on_two_replicas_starts_one_print_failed_run(
    app: FastAPI, client: TestClient
) -> None:
    """#2665: two replicas follow one print to its failed settle. Its outcome is recorded
    once, so one ``print.outcome`` is heard, and it fires ``print.failed``'s trigger once
    and ``print.finished``'s not at all."""
    state = getattr(app.state, STATE_ATTR)
    subject = PrintSubject.library(7)
    runs = state.print_runs.store

    async def queue_the_print() -> None:
        await runs.insert_accepted(
            "pr1",
            subject=subject,
            key="k-pr1",
            slug="library-7",
            workflow_id="print-pr1",
            workflow_run_id="w1",
            retention=None,
        )
        await runs.succeed(
            "pr1", "library-7", PrintRunResult(library_file_id=7, copies=1, queue_item_ids=[51])
        )

    asyncio.run(queue_the_print())
    definition = register(client, READS_PRINT)
    failed = create(
        client,
        definition["id"],
        schedule=None,
        event={"kind": "print.failed", "slug": "library-7"},
    )
    finished = create(client, definition["id"], schedule=None, event={"kind": "print.finished"})
    read = PrintProgress(
        route="slice_queue",
        stage="failed",
        settled=True,
        queue_item_id=51,
        error_message="Nozzle clog",
        bambuddy_url="http://bambuddy.test/queue",
    )

    async def read_library(_: PrintSubject) -> PrintProgress:
        return read

    async def no_output(_: Any) -> None:
        raise AssertionError("an output read")

    def replica() -> Follower:
        return Follower(
            outputs=cast(Any, None),
            observer=ProgressObserver(None),
            read=no_output,
            read_library=read_library,
            events=None,
            record_outcome=PrintOutcomeStore(
                state.projection.pool, transactional_events(state.events)
            ).record,
        )

    async def both() -> list[str]:
        replicas = [replica(), replica()]
        return list(
            await asyncio.gather(
                *(r.follow(subject.run_subject, r.now(), read_now=True) for r in replicas)
            )
        )

    assert asyncio.run(both()) == ["settled", "settled"]
    fired = decided(client, failed["id"], 1)
    assert fired["outcome"] == "started", fired
    assert fired["event"] == {
        "kind": "print.failed",
        "slug": "library-7",
        "run_id": "pr1",
        "output_id": "library:7",
        "printer": None,
    }
    run = until(client, fired["run_id"], lambda v: v["status"] == "succeeded")
    assert run["run"]["result"] == "result: ['print.failed', 'library-7', 'pr1', 'library:7']"
    time.sleep(1)
    assert len(view(client, failed["id"])["fires"]) == 1
    assert view(client, finished["id"])["fires"] == []


@pytest.mark.usefixtures("triggers")
def test_a_deleted_trigger_loses_its_schedule_and_its_grants(
    client: TestClient, workflow_reaper: WorkflowReaper
) -> None:
    definition = register(client, WAITS)
    trigger = create(client, definition["id"])
    attach(client, trigger["id"], grant({"fn": "queue_print"}))
    fired = fire(workflow_reaper, client, trigger["id"])
    assert run_view(client, fired["run_id"])["grant"]["in_force"] is True
    assert client.delete(f"/api/v1/flow-triggers/{trigger['id']}").status_code == 204
    run_grant = run_view(client, fired["run_id"])["grant"]
    assert (run_grant["in_force"], run_grant["revoked_by"]) == (False, "delete by browser")
    assert client.get(f"/api/v1/flow-triggers/{trigger['id']}").status_code == 404
    temporal = workflow_reaper.client
    assert isinstance(temporal, Client)
    assert not workflow_reaper.run(_schedule_exists(temporal, schedule_id(trigger["id"])))


async def _schedule_exists(client: Client, schedule: str) -> bool:
    """Whether Temporal holds `schedule`, read from the Schedule itself. Not
    `list_schedules`: that reads the visibility store, which lags a delete, so a Schedule
    removed a moment ago can still be listed."""
    try:
        await client.get_schedule_handle(schedule).describe()
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return False
        raise
    return True


@pytest.mark.usefixtures("triggers")
def test_the_list_is_newest_first_and_bounded(client: TestClient) -> None:
    definition = register(client, WAITS)
    made = [create(client, definition["id"]) for _ in range(3)]
    query = {"name": made[0]["name"], "limit": 2}
    listed = client.get("/api/v1/flow-triggers", params=query)
    assert listed.status_code == 200, listed.text
    assert [t["id"] for t in listed.json()] == [t["id"] for t in made[::-1][:2]]
    assert client.get("/api/v1/flow-triggers", params={"limit": 201}).status_code == 422
