"""Operations and print runs as activity (spec 2026-10-09 §3)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from starlette.datastructures import Headers

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.api.operations import _author
from scadbuddy.bambuddy.activity import row_of as run_row_of
from scadbuddy.bambuddy.runs import PrintRun, PrintRunError, PrintRunStore
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.authorship import AgentAuthor, author_from, authored_as
from scadbuddy.operations.store import OperationStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.operation_models import OperationAuthor

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    projection = JobProjection(pg_conninfo, pool_size=2)
    projection.open()
    try:
        yield projection
    finally:
        projection.close()


@pytest.fixture
def index(jobs: JobProjection) -> ActivityIndex:
    return ActivityIndex(jobs.pool, events=None)


async def _insert(
    store: OperationStore, author: OperationAuthor | None = None, retention: Any = None
) -> Any:
    return await store.insert(
        "o1",
        kind="model_import",
        subject="keychain",
        operation_key="k",
        request={},
        workflow_id="op-model_import-k",
        workflow_run_id="r1",
        retention=retention,
        author=author,
    )


async def test_operation_insert_and_finish_write_activity(
    jobs: JobProjection, index: ActivityIndex
) -> None:
    store = OperationStore(jobs.pool, activity=index)
    op = await _insert(store, OperationAuthor(principal="token:t1", session="s1", tool="render"))
    got = await index.get("operation:o1")
    assert got is not None
    assert (got.status, got.principal, got.kind) == (
        "running",
        "token:t1",
        "operation.model_import",
    )
    assert (got.session, got.via_tool) == ("s1", "render")
    assert (got.workflow_id, got.run_id) == ("op-model_import-k", "r1")
    await store.finish(op.id, result={"ok": True})
    got = await index.get("operation:o1")
    assert got is not None and got.status == "succeeded" and got.principal == "token:t1"
    assert (got.workflow_id, got.run_id) == ("op-model_import-k", "r1")
    assert got.finished_at is not None


async def test_operation_without_author_is_the_browsers(
    jobs: JobProjection, index: ActivityIndex
) -> None:
    await _insert(OperationStore(jobs.pool, activity=index))
    got = await index.get("operation:o1")
    assert got is not None and got.principal == "browser"


async def test_operation_without_activity_index_still_inserts(jobs: JobProjection) -> None:
    op = await _insert(OperationStore(jobs.pool))
    assert op.status == "running"


def make_run(status: str = "running", may_have_queued: bool = False) -> PrintRun:
    return PrintRun(
        id="p1",
        subject=PrintSubject.output("abc").key,
        status=status,  # type: ignore[arg-type]
        created_at=datetime.now(UTC),
        may_have_queued=may_have_queued,
    )


@pytest.mark.parametrize(
    ("run_status", "expected"),
    [("running", "running"), ("succeeded", "succeeded"), ("failed", "failed")],
)
def test_print_run_status_mapping(run_status: str, expected: str) -> None:
    row = run_row_of(make_run(status=run_status))
    assert (row.status, row.kind, row.id) == (expected, "print_run", "print_run:p1")
    assert row.facets["may_have_queued"] is False


async def test_print_run_insert_and_finish_write_activity(
    jobs: JobProjection, index: ActivityIndex
) -> None:
    store = PrintRunStore(jobs.pool, activity=index)
    run = await store.insert_accepted(
        "p1",
        subject=PrintSubject.output("abc"),
        key="k",
        slug="demo",
        workflow_id="print-k",
        workflow_run_id="w1",
        retention=None,
    )
    got = await index.get("print_run:p1")
    assert got is not None and (got.status, got.subject) == ("running", run.subject)
    assert got.title == "Print demo" and got.workflow_id == "print-k" and got.run_id == "w1"
    await store.fail(run.id, "demo", PrintRunError(title="t", detail="d", status=502))
    got = await index.get("print_run:p1")
    assert got is not None and got.status == "failed" and got.workflow_id == "print-k"


def test_author_from_tool_alone_is_none() -> None:
    assert author_from(Headers({"x-scadbuddy-agent-tool": "render_model"})) is None


def test_author_keeps_tool() -> None:
    with authored_as(AgentAuthor("token:t1", "s1", "render_model")):
        got = _author()
    assert got == OperationAuthor(principal="token:t1", session="s1", tool="render_model")
    assert _author() is None
