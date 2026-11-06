"""The `operations` area of the CRUD routes (#5298 Task 6, inventory J24, J25): an
`Operation` record a worker writes through `RemoteOperationStore` answers as
`OperationStore` does, an insert or a finish sent twice answers once (D5), each write's
event commits with it (D7), the API prunes past its own retention setting, and only the
write that ended a row counts it (#2295). A caller writes only its own kinds' records."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.bambuddy.runs import PrintRunError
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.crud.client import CrudRefusedError
from scadbuddy.crud.operations import OTHER_CALLERS_KIND, RemoteOperationStore
from scadbuddy.flows.store import FlowStore
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.operations.store import Operation, OperationStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.operation_activities import operation_activities
from scadbuddy.workflows.operation_models import FinishOp, InsertOp, OperationInput
from tests.crud.crud_support import served_crud

pytestmark = pytest.mark.requires_postgres

FAILED = PrintRunError(status=502, title="Bad Gateway", detail="Bambuddy said no")


async def _never(*args: Any) -> dict[str, Any]:
    raise AssertionError("a kind's check or run is the worker's, never the route's")


#: A kind the projects worker runs, and one the print worker runs.
FLOW = "flow.test"
PRINT = "reprint"
KINDS = {
    FLOW: OperationKind(FLOW, _never, _never, queue="projects"),
    PRINT: OperationKind(PRINT, _never, _never, queue="bambuddy"),
}


class _Settings:
    """The API's `SettingsStore`, as far as the insert route reads it."""

    def __init__(self) -> None:
        self.retention: float | None = None
        self.loads = 0

    def load(self) -> SimpleNamespace:
        self.loads += 1
        return SimpleNamespace(operation_retention_seconds=self.retention)


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=4)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def metrics() -> Metrics:
    metrics = Metrics()
    metrics.operation_kinds([FLOW])
    return metrics


@pytest.fixture
def operations(jobs: JobProjection, pg_conninfo: str, metrics: Metrics) -> OperationStore:
    """The API's store: its events go to the log in the write's transaction."""
    bus = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo))
    return OperationStore(jobs.pool, events=bus, metrics=metrics)


@pytest.fixture
def settings() -> _Settings:
    return _Settings()


@pytest.fixture
async def remote(
    jobs: JobProjection, operations: OperationStore, settings: _Settings
) -> AsyncIterator[RemoteOperationStore]:
    async with served_crud(
        FlowStore(jobs.pool),
        operations=operations,
        kinds=KINDS,
        settings_store=cast(SettingsStore, settings),
    ) as crud:
        yield RemoteOperationStore(crud, "projects")


def _events(jobs: JobProjection) -> list[str]:
    """The operation each logged `operation.changed` names."""
    with jobs.pool.connection() as conn:
        rows = conn.execute(
            "SELECT payload->>'operation_id' AS op FROM events"
            " WHERE kind = 'operation.changed' ORDER BY seq"
        ).fetchall()
    return [str(row["op"]) for row in rows]


def _rows(jobs: JobProjection) -> list[str]:
    with jobs.pool.connection() as conn:
        return [row["id"] for row in conn.execute("SELECT id FROM operations ORDER BY id")]


async def _insert(
    store: RemoteOperationStore | OperationStore,
    op_id: str,
    *,
    key: str = "k",
    run: str = "r1",
    kind: str = FLOW,
) -> Operation:
    return await store.insert(
        op_id,
        kind=kind,
        subject="archive:5",
        operation_key=key,
        request={"archive_id": 5},
        workflow_id=f"op-{kind}-{key}",
        workflow_run_id=run,
        retention=None,
    )


def test_projects_is_served_the_operations_area() -> None:
    assert "operations" in CRUD_AREAS["projects"]


async def test_an_insert_sent_twice_is_one_row_and_one_event(
    remote: RemoteOperationStore, jobs: JobProjection
) -> None:
    first = await _insert(remote, "a")
    # A retried activity mints another id; the execution's row answers it.
    again = await _insert(remote, "b")
    assert first.id == again.id == "a" and again.status == "running"
    assert _rows(jobs) == ["a"]
    assert _events(jobs) == ["a"]


async def test_the_api_prunes_past_its_own_retention(
    remote: RemoteOperationStore, jobs: JobProjection, settings: _Settings
) -> None:
    await _insert(remote, "old", key="old")
    await remote.finish("old", result={})
    with jobs.pool.connection() as conn:
        conn.execute("UPDATE operations SET finished_at = now() - interval '2 hours'")
    # No retention keeps every finished row.
    await _insert(remote, "kept", key="kept")
    assert _rows(jobs) == ["kept", "old"]
    settings.retention = timedelta(hours=1).total_seconds()
    await _insert(remote, "new", key="new")
    assert _rows(jobs) == ["kept", "new"]
    assert settings.loads == 3


async def test_a_finish_sent_twice_ends_and_counts_once(
    remote: RemoteOperationStore, jobs: JobProjection, metrics: Metrics
) -> None:
    await _insert(remote, "a")
    done = await remote.finish("a", result={"queue_item_id": 7})
    again = await remote.finish("a", error=FAILED)
    assert done.status == again.status == "succeeded"
    assert again.result == {"queue_item_id": 7} and again.error is None
    finished = metrics.registry.get_sample_value(
        "scadbuddy_operations_finished_total", {"kind": FLOW, "outcome": "succeeded"}
    )
    assert finished == 1
    assert _events(jobs) == ["a", "a"]


async def test_a_failed_finish_carries_its_problem(remote: RemoteOperationStore) -> None:
    await _insert(remote, "a")
    ended = await remote.finish("a", error=FAILED)
    assert ended.status == "failed" and ended.error == FAILED and ended.result is None


async def test_finishing_no_operation_is_a_lookup_error(remote: RemoteOperationStore) -> None:
    with pytest.raises(LookupError, match="there is no operation missing"):
        await remote.finish("missing", result={})


async def test_the_workers_activities_load_no_settings(
    remote: RemoteOperationStore, jobs: JobProjection, settings: _Settings
) -> None:
    """The projects worker's `op_insert` and `op_finish`, with no settings store."""
    insert, finish = operation_activities(remote, None, {})
    env = ActivityEnvironment()
    request = OperationInput(kind=FLOW, subject="archive:5", key="k", request={})
    op: Any = await env.run(insert, InsertOp(input=request))
    assert isinstance(op, Operation) and op.status == "running"
    ended: Any = await env.run(finish, FinishOp(operation_id=op.id, result={"ok": True}))
    assert ended.status == "succeeded" and _rows(jobs) == [op.id]
    # The route read the retention, in the API.
    assert settings.loads == 1


async def test_another_callers_kind_is_refused_at_once(
    remote: RemoteOperationStore, jobs: JobProjection
) -> None:
    """A final refusal, not a retry: the client raises it on the first answer."""
    with pytest.raises(CrudRefusedError) as refused:
        await _insert(remote, "a", kind=PRINT)
    assert refused.value.status == 403
    assert refused.value.problem["reason"] == OTHER_CALLERS_KIND
    assert _rows(jobs) == [] and _events(jobs) == []


async def test_an_unknown_kind_is_refused_at_once(
    remote: RemoteOperationStore, jobs: JobProjection
) -> None:
    with pytest.raises(CrudRefusedError) as refused:
        await _insert(remote, "a", kind="no.such.kind")
    assert refused.value.status == 422
    assert _rows(jobs) == []


async def test_another_callers_row_is_missing_and_unchanged(
    remote: RemoteOperationStore, operations: OperationStore, jobs: JobProjection
) -> None:
    """The same answer as a row that does not exist, and the row stays running."""
    await _insert(operations, "printed", kind=PRINT)
    with pytest.raises(LookupError, match="there is no operation printed"):
        await remote.finish("printed", result={})
    held = await operations.get("printed")
    assert held is not None and held.status == "running" and held.finished_at is None
    assert _events(jobs) == ["printed"]


async def test_another_executions_workflow_ids_answer_nothing_of_its_row(
    remote: RemoteOperationStore, operations: OperationStore, jobs: JobProjection
) -> None:
    """The insert is natural on the workflow ids, so a caller naming a print row's ids
    with a kind of its own must not get that row back."""
    printed = await _insert(operations, "printed", kind=PRINT)
    with pytest.raises(CrudRefusedError) as refused:
        await remote.insert(
            "mine",
            kind=FLOW,
            subject="other",
            operation_key="k",
            request={},
            workflow_id=f"op-{PRINT}-k",
            workflow_run_id="r1",
        )
    assert refused.value.status == 404
    leaked = str(refused.value.problem)
    assert "printed" not in leaked and PRINT not in leaked and "archive:5" not in leaked
    assert _rows(jobs) == ["printed"] and await operations.get("printed") == printed
    # A genuine resend (a retried activity: a new op id) still answers its own row.
    first = await _insert(remote, "a")
    assert (await _insert(remote, "b")).id == first.id == "a"
