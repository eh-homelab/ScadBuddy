"""The projects worker's grant and Reset routes (#5298 Task 5, inventory J11, J16-J20):
a grant's consume chooses its rule in the API under the grant's row lock and answers a
resent request id with its first use, and a Reset's Temporal call runs in the API under
the run's row lock."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.api.workflowservice.v1 import ResetWorkflowExecutionRequest
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.projects import RemoteFlowStore
from scadbuddy.flow_triggers.store import TriggerStore
from scadbuddy.flows.grants import (
    CallFacts,
    ConsumeRequest,
    GrantRule,
    GrantSpec,
    GrantUse,
)
from scadbuddy.flows.models import Decision
from scadbuddy.flows.store import FlowStore, ResetBeforeFloorError, ResetSupersededError
from scadbuddy.render.projection import JobProjection
from tests.crud.crud_support import served_flow_store
from tests.test_flow_store import Events, _history, started
from tests.test_flow_trigger_store import approved, make, seed

pytestmark = pytest.mark.requires_postgres


class _Service:
    def __init__(self) -> None:
        self.resets: list[ResetWorkflowExecutionRequest] = []
        #: How long Temporal takes to answer, and what it fails with instead.
        self.delay = 0.0
        self.error: RPCError | None = None

    async def reset_workflow_execution(
        self, request: ResetWorkflowExecutionRequest
    ) -> SimpleNamespace:
        self.resets.append(request)
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(run_id="wr2")


def _temporal(service: _Service) -> Any:
    return SimpleNamespace(namespace="default", workflow_service=service)


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=10)
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
def service() -> _Service:
    return _Service()


@pytest.fixture
async def crud_app(store: FlowStore, service: _Service) -> AsyncIterator[RemoteFlowStore]:
    """The routes on a local port, with a Temporal client whose Reset answers `wr2`."""
    async with served_flow_store(store, client=_temporal(service)) as remote:
        yield remote


def _arrange(request_id: str) -> ConsumeRequest:
    return ConsumeRequest(
        request_id=request_id,
        fn="arrange",
        facts=CallFacts(fn="arrange", models=["coaster"]),
        input_hash="h",
    )


async def _grant(store: FlowStore, run_id: str, **rule: Any) -> None:
    spec = GrantSpec(
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        rules=[GrantRule(fn="arrange", **rule)],
    )
    await store.insert_grant(run_id, spec, status="approved", requested_by={"kind": "browser"})


def test_the_flows_area_is_listed() -> None:
    assert "flows" in CRUD_AREAS["projects"]


async def test_a_consume_sent_twice_records_one_use_and_spends_once(
    crud_app: RemoteFlowStore, store: FlowStore, jobs: JobProjection
) -> None:
    await started(store)
    await _grant(store, "r1", max_calls=5)
    first = await crud_app.consume("r1", _arrange("q1"))
    again = await crud_app.consume("r1", _arrange("q1"))
    assert isinstance(first, GrantUse) and first == again
    assert first == await store.grant_use("q1") == await crud_app.grant_use("q1")
    with jobs.pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM flow_grant_uses").fetchone()
    assert row is not None and row["n"] == 1
    grant = await crud_app.get_grant("r1")
    assert grant is not None and grant == await store.get_grant("r1")
    assert grant.used["0"].calls == 1
    assert await crud_app.get_grant("nope") is None
    assert await crud_app.grant_use("nope") is None
    # Outside: why, from the API's own check.
    printing = _arrange("q2").model_copy(update={"facts": CallFacts(fn="queue_print")})
    assert await crud_app.consume("r1", printing) == "not a arrange rule"


async def test_concurrent_consumes_at_a_standing_grants_limit_admit_one(
    crud_app: RemoteFlowStore, jobs: JobProjection
) -> None:
    triggers = TriggerStore(jobs.pool)
    trigger = await make(triggers, overlap="allow_all")
    spec = GrantSpec(
        expires_at=datetime.now(UTC) + timedelta(days=1),
        rules=[GrantRule(fn="arrange", max_calls=1)],
    )
    await approved(triggers, trigger, spec)
    runs = [str(uuid.uuid4()) for _ in range(4)]
    for run_id in runs:
        assert await seed(triggers, trigger, run_id)
    taken = await asyncio.gather(
        *(crud_app.consume(run_id, _arrange(f"req-{run_id}")) for run_id in runs)
    )
    assert sum(isinstance(t, GrantUse) for t in taken) == 1
    assert sorted(t for t in taken if isinstance(t, str)) == ["call limit reached"] * 3
    standing = await triggers.current_grant(trigger.id)
    assert standing is not None and standing.approval_id is not None
    assert standing.used[standing.approval_id]["0"].calls == 1


async def test_a_grant_decision_is_recorded_once_and_withdrawn(
    crud_app: RemoteFlowStore, store: FlowStore
) -> None:
    await started(store)
    await _grant(store, "r1")
    use = await crud_app.consume("r1", _arrange("flow:r1:wr1:c"))
    assert isinstance(use, GrantUse)
    decision = Decision(
        request_id="flow:r1:wr1:c",
        run_id="r1",
        workflow_run_id="wr1",
        call_id="c",
        kind="approval",
        outcome="approved",
        response={},
        responder=f"grant:{use.grant_id}",
        input_hash="h",
    )
    assert await crud_app.record_grant_decision(decision) is True
    assert await crud_app.record_grant_decision(decision) is True
    other = decision.model_copy(update={"input_hash": "other"})
    assert await crud_app.record_grant_decision(other) is False
    assert await store.get_decision(decision.request_id) is not None
    await crud_app.withdraw_grant_decision(decision.request_id)
    await crud_app.withdraw_grant_decision(decision.request_id)
    assert await store.get_decision(decision.request_id) is None


async def _reset(
    remote: RemoteFlowStore, request_id: str, point: int = 26, responder: str | None = None
) -> Any:
    async def never() -> str:
        raise AssertionError("the API resets, not the worker")

    return await remote.reset_run(
        "r1",
        replaced="wr1",
        point=point,
        request_id=request_id,
        reset=never,
        responder=responder,
    )


async def test_a_reset_moves_the_row_once_and_answers_a_resend_the_same(
    crud_app: RemoteFlowStore, store: FlowStore, events: Events, service: _Service
) -> None:
    await _history(store)
    before = len(events.published)
    run = await _reset(crud_app, "op1", responder="session:s")
    assert run is not None and (run.workflow_run_id, run.status) == ("wr2", "waiting")
    assert run == await store.get_run("r1")
    [sent] = service.resets
    assert sent.workflow_execution.workflow_id == "flow-r1"
    assert (sent.workflow_execution.run_id, sent.workflow_task_finish_event_id) == ("wr1", 26)
    assert (sent.request_id, sent.reason) == ("op1", "flow reset by session:s")
    assert len(events.published) == before + 1
    # Sent again: the row as that Reset left it, and no second Reset.
    assert await _reset(crud_app, "op1", responder="session:s") == run
    assert len(service.resets) == 1
    assert len(events.published) == before + 1
    # Another Reset of the same execution: refused, with the row it moved to.
    with pytest.raises(ResetSupersededError) as refused:
        await _reset(crud_app, "op2", point=21)
    assert refused.value.run.workflow_run_id == "wr2"
    assert len(service.resets) == 1


async def test_a_reset_before_the_floor_or_of_no_run(
    crud_app: RemoteFlowStore, store: FlowStore, service: _Service
) -> None:
    await _history(store)
    with store._pool.connection() as conn:
        conn.execute("UPDATE workflow_runs SET reset_floor = 30 WHERE id = 'r1'")
    with pytest.raises(ResetBeforeFloorError) as refused:
        await _reset(crud_app, "op1")
    assert refused.value.floor == 30
    assert service.resets == []
    missing = await crud_app.reset_run(
        "nope", replaced="wr1", point=26, request_id="op1", reset=_unused
    )
    assert missing is None


async def _unused() -> str:
    raise AssertionError("never called")


async def test_without_a_responder_the_reason_names_the_operation(
    crud_app: RemoteFlowStore, store: FlowStore, service: _Service
) -> None:
    """A worker from before `responder` was sent."""
    await _history(store)
    assert await _reset(crud_app, "op1") is not None
    assert [r.reason for r in service.resets] == ["flow reset by operation op1"]


async def test_a_slow_reset_is_one_request(store: FlowStore, service: _Service) -> None:
    """Temporal answers after the client's own timeout: the Reset's request waits for it
    (`RESET_REQUEST_S`), rather than timing out and retrying past the budget."""
    await _history(store)
    service.delay = 0.6
    async with served_flow_store(
        store, client=_temporal(service), timeout=0.3, unavailable_budget=0.1
    ) as remote:
        run = await _reset(remote, "op1")
    assert run is not None and run.workflow_run_id == "wr2"
    assert len(service.resets) == 1


async def test_a_reset_with_flows_unconnected_is_retried_then_unavailable(
    store: FlowStore,
) -> None:
    await _history(store)
    before = await store.get_run("r1")
    async with served_flow_store(store, client=None, unavailable_budget=0.3) as remote:
        with pytest.raises(CrudUnavailableError, match="503"):
            await _reset(remote, "op1")
    assert await store.get_run("r1") == before


async def test_temporal_refusing_a_reset_is_refused_at_once(
    crud_app: RemoteFlowStore, store: FlowStore, service: _Service
) -> None:
    await _history(store)
    before = await store.get_run("r1")
    service.error = RPCError("no such event", RPCStatusCode.INVALID_ARGUMENT, b"")
    with pytest.raises(CrudRefusedError) as refused:
        await _reset(crud_app, "op1")
    assert refused.value.status == 422
    assert refused.value.problem["reason"] == "reset-refused"
    assert refused.value.problem["grpc_status"] == "INVALID_ARGUMENT"
    assert len(service.resets) == 1
    assert await store.get_run("r1") == before


async def test_a_transient_temporal_failure_is_retried(store: FlowStore, service: _Service) -> None:
    await _history(store)
    before = await store.get_run("r1")
    service.error = RPCError("try later", RPCStatusCode.UNAVAILABLE, b"")
    async with served_flow_store(
        store, client=_temporal(service), unavailable_budget=0.5
    ) as remote:
        with pytest.raises(CrudUnavailableError, match="500"):
            await _reset(remote, "op1")
    assert len(service.resets) > 1
    assert await store.get_run("r1") == before
