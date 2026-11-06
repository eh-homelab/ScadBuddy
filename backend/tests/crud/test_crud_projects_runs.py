"""The projects worker's flow-run routes (#5298 Task 4, inventory J6-J15, J21-J23):
`RemoteFlowStore` answers as `FlowStore` does for the same input, each write answers the
same when it is sent twice (D5), and a write that announces the run still announces it
in its own transaction (D7)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI

from scadbuddy.api import crud_projects
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import SESSION_COOKIE, AuthGate
from scadbuddy.auth.models import AuthSession, Service
from scadbuddy.auth.service_credentials import ServiceAuth
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.crud.client import CrudClient
from scadbuddy.crud.projects import RemoteFlowStore
from scadbuddy.flows.component import FLOWS
from scadbuddy.flows.models import Decision, Proposal
from scadbuddy.flows.store import FlowStore, HopMovedError
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flow_models import FlowRecord, FlowWaiting, HopRecord, ProjectionWrite
from tests.flows.flows_support import define, parent_session
from tests.test_flow_store import NOW, Events, parked, started, step

pytestmark = pytest.mark.requires_postgres

TOKEN = "sbsvc_projects"
BASE = "/api/v1/internal/crud/projects"
SESSION = "a-live-session"


class _Credentials:
    def resolve(self, token: str) -> Service | None:
        return Service("projects", frozenset({"crud.projects"})) if token == TOKEN else None


#: The session tokens resolved, to show a person's request reached the guard as one.
RESOLVED: list[str] = []


class _Sessions:
    def resolve(self, token: str) -> AuthSession | None:
        RESOLVED.append(token)
        if token != SESSION:
            return None
        return AuthSession(
            id_hash=b"h",
            principal="user:https://idp.test#alice",
            issuer="https://idp.test",
            subject="alice",
            name=None,
            email=None,
            kind="login",
            created_at=NOW,
            last_seen_at=NOW,
            idle_expires_at=NOW + timedelta(days=3650),
            absolute_expires_at=NOW + timedelta(days=3650),
        )


class _Source:
    def token(self) -> str | None:
        return TOKEN

    def invalidate(self, token: str) -> None: ...


class _Noted:
    def note(self, *args: object) -> None: ...


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=4)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture
def store(jobs: JobProjection, events: Events) -> FlowStore:
    return FlowStore(jobs.pool, events=events)


@pytest.fixture
def mode() -> str:
    return "enforce"


@pytest.fixture
def app(store: FlowStore, mode: str) -> FastAPI:
    """The routes behind the real `auth_check` and gate, with the flows component's store."""
    app = FastAPI(dependencies=[Depends(auth_check)])
    parts = {AUTH: SimpleNamespace(would_refuse=_Noted()), FLOWS: SimpleNamespace(store=store)}
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode=mode),
        components=SimpleNamespace(get=lambda key: parts[key]),
    )
    app.include_router(crud_projects.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _Sessions(), credentials=lambda: _Credentials())
    return app


@pytest.fixture
async def http(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield client


@pytest.fixture
async def remote(app: FastAPI) -> AsyncIterator[RemoteFlowStore]:
    client = CrudClient(
        "http://api",
        "projects",
        ServiceAuth(_Source()),
        transport=httpx.ASGITransport(app=app),
        unavailable_budget=0.2,
    )
    try:
        yield RemoteFlowStore(client)
    finally:
        await client.aclose()


def _record(definition_id: str, run_id: str = "r2") -> FlowRecord:
    return FlowRecord(
        run_id=run_id,
        definition_id=definition_id,
        version=1,
        name="swap",
        workflow_id=f"flow-{run_id}",
        workflow_run_id="wr1",
        started_by={"kind": "browser"},
        inputs={"n": 1},
    )


def _same(a: Any, b: Any) -> None:
    """Equal but for `updated_at`, which every write moves."""
    assert a is not None and b is not None
    assert a.model_copy(update={"updated_at": NOW}) == b.model_copy(update={"updated_at": NOW})


async def test_insert_run_twice_answers_the_same_and_announces_once(
    remote: RemoteFlowStore, store: FlowStore, events: Events
) -> None:
    definition = await define(store, "swap")
    first = await remote.insert_run(_record(definition.id))
    again = await remote.insert_run(_record(definition.id))
    assert first == again == await store.get_run("r2")
    assert events.runs() == [("r2", "running")]


async def test_project_twice_answers_the_same_and_announces_each(
    remote: RemoteFlowStore, store: FlowStore, events: Events
) -> None:
    await started(store)
    write = ProjectionWrite(
        run_id="r1",
        workflow_run_id="wr1",
        step=step("a", 1),
        waiting_add=parked("a", 11),
        history_length=11,
    )
    first = await remote.project(write)
    again = await remote.project(write)
    _same(first, again)
    _same(again, await store.get_run("r1"))
    assert first is not None and first.status == "waiting"
    # One announcement per write, each in the write's own transaction (D7).
    assert events.runs() == [("r1", "running"), ("r1", "waiting"), ("r1", "waiting")]
    assert await remote.project(write.model_copy(update={"run_id": "nope"})) is None


async def test_project_refuses_a_body_for_another_run(
    http: httpx.AsyncClient, store: FlowStore
) -> None:
    await started(store)
    write = ProjectionWrite(run_id="other", workflow_run_id="wr1")
    response = await http.post(f"{BASE}/runs/r1/project", json=write.model_dump(mode="json"))
    assert response.status_code == 422
    assert response.headers["X-ScadBuddy-Crud"] == "1"


async def test_reads_answer_as_the_store(
    remote: RemoteFlowStore, store: FlowStore, http: httpx.AsyncClient
) -> None:
    await started(store)
    talking = step("s", 1).model_copy(update={"session_id": "sess-1"})
    await store.project(ProjectionWrite(run_id="r1", workflow_run_id="wr1", step=talking))
    assert await remote.get_run("r1") == await store.get_run("r1")
    assert await remote.get_run("nope") is None
    missing = await http.get(f"{BASE}/runs/nope")
    assert missing.status_code == 404
    assert missing.headers["X-ScadBuddy-Crud"] == "1"
    assert await remote.reset_floor("r1") == await store.reset_floor("r1")
    assert await remote.reset_floor("nope") is None
    found = await remote.run_talking_to("sess-1", "s")
    assert found is not None and found == await store.run_talking_to("sess-1", "s")
    assert await remote.run_talking_to("sess-2", "s") is None
    assert await remote.approval_expiry_s() == await store.approval_expiry_s()


async def test_reset_floor_reads_the_row(remote: RemoteFlowStore, store: FlowStore) -> None:
    await started(store)
    with store._pool.connection() as conn:
        conn.execute("UPDATE workflow_runs SET reset_floor = 31 WHERE id = 'r1'")
    assert await remote.reset_floor("r1") == 31


async def test_parent_tiers_answer_as_the_store(remote: RemoteFlowStore, store: FlowStore) -> None:
    session = "5e551011-0000-4000-8000-0000000000ab"
    with store._pool.connection() as conn:
        parent_session(conn, session, "token:5e551011-0000-4000-8000-0000000000ac", tier="write")
    principal = "token:5e551011-0000-4000-8000-0000000000ac"
    assert await remote.parent_tiers(session, principal) == ["read", "write"]
    assert await remote.parent_tiers(session, principal) == await store.parent_tiers(
        session, principal
    )
    assert await remote.parent_tiers(session, "someone-else") is None


def _at_parent(call_id: str) -> FlowWaiting:
    return FlowWaiting(
        call_id=call_id,
        kind="approval",
        fn="tool",
        since=NOW,
        history_length=11,
        hop="parent",
        hop_until=NOW + timedelta(minutes=5),
    )


async def test_move_hop_twice_answers_the_same_and_announces_once(
    remote: RemoteFlowStore, store: FlowStore, events: Events
) -> None:
    await started(store)
    await store.project(
        ProjectionWrite(run_id="r1", workflow_run_id="wr1", waiting_add=_at_parent("c"))
    )
    before = len(events.published)
    record = HopRecord(hop="parent", action="narrow", actor="session:s", at=NOW, reason="x")
    proposal = Proposal(args={"n": 2}, input_hash="h", proposed_by="session:s")
    first = await remote.move_hop("r1", "c", record, proposal=proposal)
    again = await remote.move_hop("r1", "c", record, proposal=proposal)
    assert first is not None and first == again
    assert (first.hop, first.proposed, first.hops) == ("person", True, [record])
    assert len(events.published) == before + 1
    run = await store.get_run("r1")
    assert run is not None and run.waiting_on == [first]
    request_id = "flow:r1:wr1:c"
    assert await remote.get_proposal(request_id) == await store.get_proposal(request_id)
    assert await remote.get_proposal(request_id) == proposal
    assert await remote.get_proposal("flow:r1:wr1:none") is None
    # Another record finds the call with the person already: none of its own.
    later = record.model_copy(update={"action": "timeout", "actor": "system"})
    assert await remote.move_hop("r1", "c", later) is None
    assert await remote.move_hop("nope", "c", record) is None


def _decision(**update: Any) -> Decision:
    base = Decision(
        request_id="flow:r1:wr1:c",
        run_id="r1",
        workflow_run_id="wr1",
        call_id="c",
        kind="approval",
        outcome="denied",
        response={"reason": "no"},
        responder="session:s",
    )
    return base.model_copy(update=update)


async def test_decisions_record_twice_and_delete(
    remote: RemoteFlowStore, store: FlowStore, http: httpx.AsyncClient
) -> None:
    await started(store)
    await store.project(
        ProjectionWrite(run_id="r1", workflow_run_id="wr1", waiting_add=_at_parent("c"))
    )
    decision = _decision()
    assert await remote.record_decision(decision, at_hop="parent") is True
    assert await remote.record_decision(decision, at_hop="parent") is True
    raw = await http.post(f"{BASE}/decisions", json={"decision": decision.model_dump(mode="json")})
    assert raw.json() == {"recorded": True}
    other = _decision(outcome="approved", response={})
    assert await remote.record_decision(other) is False
    assert await remote.get_decision(decision.request_id) == await store.get_decision(
        decision.request_id
    )
    held = await remote.get_decision(decision.request_id)
    assert held is not None and held.outcome == "denied"
    assert await remote.get_decision("flow:r1:wr1:none") is None
    await remote.delete_decision(decision.request_id)
    await remote.delete_decision(decision.request_id)
    assert await store.get_decision(decision.request_id) is None
    with pytest.raises(HopMovedError):
        await remote.record_decision(_decision(call_id="gone"), at_hop="parent")


async def test_a_hop_moved_since_this_very_decision_still_answers_recorded(
    remote: RemoteFlowStore, store: FlowStore
) -> None:
    """A retry after the call left `parent` finds its own decision there: recorded."""
    await started(store)
    await store.project(
        ProjectionWrite(run_id="r1", workflow_run_id="wr1", waiting_add=_at_parent("c"))
    )
    decision = _decision()
    assert await remote.record_decision(decision, at_hop="parent") is True
    await store.project(ProjectionWrite(run_id="r1", workflow_run_id="wr1", waiting_remove="c"))
    assert await remote.record_decision(decision, at_hop="parent") is True
    with pytest.raises(HopMovedError):
        await remote.record_decision(_decision(outcome="approved", response={}), at_hop="parent")


async def test_a_retry_finds_its_move_after_the_entry_was_resolved(
    remote: RemoteFlowStore, store: FlowStore
) -> None:
    """The answer to the first move was lost, and a person decided and the run moved on
    before the retry: the retry still answers the entry it moved, never another's."""
    await started(store)
    await store.project(
        ProjectionWrite(run_id="r1", workflow_run_id="wr1", waiting_add=_at_parent("c"))
    )
    record = HopRecord(hop="parent", action="pass_up", actor="session:s", at=NOW)
    first = await store.move_hop("r1", "c", record)
    await store.project(
        ProjectionWrite(run_id="r1", workflow_run_id="wr1", waiting_remove="c", history_length=20)
    )
    run = await store.get_run("r1")
    assert run is not None and run.waiting_on == []
    assert await remote.move_hop("r1", "c", record) == first
    other = record.model_copy(update={"actor": "session:other"})
    assert await remote.move_hop("r1", "c", other) is None


async def test_a_person_s_decision_with_the_same_answer_is_not_this_one(
    remote: RemoteFlowStore, store: FlowStore
) -> None:
    """After the hop moved, a person's denial with the parent's response is another's."""
    await started(store)
    await store.project(
        ProjectionWrite(run_id="r1", workflow_run_id="wr1", waiting_add=_at_parent("c"))
    )
    await store.record_decision(_decision(response={}, responder="browser"))
    await store.project(ProjectionWrite(run_id="r1", workflow_run_id="wr1", waiting_remove="c"))
    with pytest.raises(HopMovedError):
        await remote.record_decision(_decision(response={}), at_hop="parent")


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
async def test_a_person_or_nobody_is_refused_in_every_mode(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as client:
        nobody = await client.get(f"{BASE}/approval-expiry")
        person = await client.get(
            f"{BASE}/approval-expiry", headers={"Cookie": f"{SESSION_COOKIE}={SESSION}"}
        )
    assert nobody.status_code == 401
    assert person.status_code == 401
    assert SESSION in RESOLVED
    for refused in (nobody, person):
        assert refused.headers["X-ScadBuddy-Crud"] == "1"
