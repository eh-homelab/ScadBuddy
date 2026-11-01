"""`PrintRunStore` (#470, #1052): a run's record in Postgres, written only by its
`PrintRun` workflow's activities."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, cast

import pytest
from psycopg import Connection
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.runs import (
    LOST,
    LOST_UNQUEUED,
    PrintRun,
    PrintRunError,
    PrintRunStore,
)
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.events import Event, PrintRunEvent
from scadbuddy.printers.lan_models import LanStopped
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
from scadbuddy.render.pg_store import MIGRATIONS_DIR
from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows import client as client_module
from scadbuddy.workflows.client import reconcile_lost_runs
from tests.printers.conftest import KEK
from tests.support.temporal import DownClient, namespace_not_found_error, temporal_client

pytestmark = pytest.mark.requires_postgres

OUTPUT = "a" * 32
RESULT = PrintRunResult(library_file_id=1, copies=1, bambuddy_url="http://b/queue")
REFUSED = PrintRunError(status=422, title="Unprocessable Content", detail="no spool")


class Events:
    """Records what is published inside a transaction."""

    def __init__(self) -> None:
        self.published: list[Event] = []

    def publish_in(self, conn: Connection[Any], event: Event) -> None:
        self.published.append(event)


@pytest.fixture
def jobs(pg_conninfo: str) -> Iterator[JobProjection]:
    """The pool the runs share with the render projection, as the app builds it."""
    store = JobProjection(pg_conninfo, pool_size=2)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture
def store(jobs: JobProjection, events: Events) -> PrintRunStore:
    return PrintRunStore(jobs.pool, events=events)


async def accept(
    store: PrintRunStore, key: str = "k", *, run_id: str = "r1", wf_run: str = "w1"
) -> PrintRun:
    return await store.insert_accepted(
        run_id,
        subject=PrintSubject.output(OUTPUT),
        key=key,
        slug="demo",
        workflow_id=f"print-{key}",
        workflow_run_id=wf_run,
        retention=None,
    )


async def test_insert_accepted_twice_returns_the_first_row_and_publishes_once(
    store: PrintRunStore, events: Events
) -> None:
    """Temporal retries the first activity when its worker died after the commit but
    before it reported (§4.2 step 3): the retry must not add a row or announce twice."""
    first = await accept(store, run_id="r1")
    again = await accept(store, run_id="r2")

    assert again.id == first.id == "r1"
    assert again.status == "running"
    assert [type(e) for e in events.published] == [PrintRunEvent]


async def test_a_new_execution_of_the_same_workflow_id_is_a_new_row(store: PrintRunStore) -> None:
    first = await accept(store, run_id="r1", wf_run="w1")
    second = await accept(store, run_id="r2", wf_run="w2")
    assert {first.id, second.id} == {"r1", "r2"}


async def test_finish_is_guarded_on_running_and_publishes_in_the_transaction(
    store: PrintRunStore, events: Events
) -> None:
    run = await accept(store)
    done = await store.succeed(run.id, "demo", RESULT)
    late = await store.fail(run.id, "demo", REFUSED)

    assert done.status == "succeeded" and done.result == RESULT
    assert late.status == "succeeded"  # the second end changed nothing
    assert len(events.published) == 2  # accepted, succeeded; not the no-op fail


async def test_update_result_rewrites_only_a_succeeded_run(
    store: PrintRunStore, events: Events
) -> None:
    """#4541: a LAN run's later starts rewrite its result; a running or failed run's
    record is never touched, and the change is announced as the end was."""
    running = await accept(store)
    later = RESULT.model_copy(update={"lan_waiting": [1]})
    assert (await store.update_result(running.id, "demo", later)).result is None
    await store.succeed(running.id, "demo", RESULT)
    updated = await store.update_result(running.id, "demo", later)
    assert updated.status == "succeeded" and updated.result == later
    assert (await store.get(running.id)) == updated
    assert len(events.published) == 3  # accepted, succeeded, updated; not the no-op
    failed = await accept(store, "k2", run_id="r2", wf_run="w2")
    await store.fail(failed.id, "demo", REFUSED)
    assert (await store.update_result(failed.id, "demo", later)).result is None
    with pytest.raises(LookupError):
        await store.update_result("nope", "demo", later)


async def test_lan_waiting_reads_each_printers_newest_run_with_prints_to_start(
    store: PrintRunStore, jobs: JobProjection
) -> None:
    """#4942: the Prints page's controls. A printer's newest succeeded run whose prints
    still to start were neither all started nor stopped; nothing for any other run."""
    printers = PrinterStore(jobs.pool, KEK)
    one = printers.create(PrinterCreate(serial="01P00A000000001", name="One")).id
    two = printers.create(PrinterCreate(serial="01P00A000000002", name="Two")).id
    waiting = RESULT.model_copy(update={"path": "native", "lan_waiting": [1]})
    stopped = waiting.model_copy(
        update={"lan_stopped": LanStopped(stage="cancelled", message="Stopped.")}
    )
    for run_id, printer, result in [
        ("old", one, waiting),
        ("new", one, waiting),
        ("stopped", two, stopped),
        ("done", two, RESULT),
    ]:
        await store.insert_accepted(
            run_id,
            subject=PrintSubject.output(OUTPUT),
            key=run_id,
            slug="demo",
            workflow_id=f"print-{run_id}",
            workflow_run_id="w",
            retention=None,
            printer=printer,
        )
        await store.succeed(run_id, "demo", result)
    running = await accept(store, "k3", run_id="running", wf_run="w3")
    assert running.status == "running"
    found = await store.lan_waiting([one, two])
    assert {printer: run.id for printer, run in found.items()} == {one: "new"}
    assert await store.lan_waiting([]) == {}


async def test_a_run_with_prints_still_to_start_is_still_using_its_files(
    store: PrintRunStore,
) -> None:
    """#4337: a LAN run succeeds once its first print starts, and starts the rest from its
    sliced files later (#4541), so until it has started (or stopped) every one it still
    uses them, as a running run does: an output delete refuses while it waits, and the
    per-print copies' sweep leaves its subject alone."""
    waiting = RESULT.model_copy(update={"path": "native", "lan_waiting": [1]})
    stopped = waiting.model_copy(
        update={"lan_stopped": LanStopped(stage="cancelled", message="Stopped.")}
    )
    subjects = {
        "running": PrintSubject.output("o-running"),
        "waiting": PrintSubject.library(1_000_000_042),
        "stopped": PrintSubject.output("o-stopped"),
        "done": PrintSubject.output("o-done"),
    }
    for run_id, subject in subjects.items():
        await store.insert_accepted(
            run_id,
            subject=subject,
            key=run_id,
            slug="demo",
            workflow_id=f"print-{run_id}",
            workflow_run_id="w",
            retention=None,
        )
    await store.succeed("waiting", "demo", waiting)
    await store.succeed("stopped", "demo", stopped)
    await store.succeed("done", "demo", RESULT)

    assert await store.subjects_in_use() == {"o-running", "library:1000000042"}
    assert [r.id for r in await store.running_for("library:1000000042")] == ["waiting"]
    assert await store.running_for("o-stopped") == []
    assert await store.running_for("o-done") == []


async def test_start_enqueue_marks_may_have_queued_on_a_later_failure(store: PrintRunStore) -> None:
    run = await accept(store)
    await store.start_enqueue(run.id)
    failed = await store.fail(run.id, "demo", REFUSED)
    assert failed.may_have_queued


async def test_an_unqueued_failure_clears_the_started_enqueue(store: PrintRunStore) -> None:
    """A run cancelled once ``start_enqueue`` wrote, before any ``POST /queue/``, records
    that nothing was queued (review #1316 (8) 2)."""
    run = await accept(store)
    await store.start_enqueue(run.id)
    failed = await store.fail(run.id, "demo", REFUSED, unqueued=True)
    assert failed.status == "failed" and not failed.may_have_queued


async def test_a_failure_before_any_enqueue_may_not_have_queued(store: PrintRunStore) -> None:
    run = await accept(store)
    failed = await store.fail(run.id, "demo", REFUSED)
    assert not failed.may_have_queued


async def test_find_with_a_request_id_returns_a_failed_run_of_any_age(
    jobs: JobProjection, events: Events
) -> None:
    store = PrintRunStore(jobs.pool, events=events, repeat_window=timedelta(0))
    run = await accept(store)
    await store.fail(run.id, "demo", REFUSED)

    found = await store.find("k", has_request_id=True)
    assert found is not None and found.id == run.id
    assert await store.find("k", has_request_id=False) is None


async def test_find_without_a_request_id_keeps_the_repeat_window(store: PrintRunStore) -> None:
    run = await accept(store)
    running = await store.find("k", has_request_id=False)
    assert running is not None and running.id == run.id
    await store.succeed(run.id, "demo", RESULT)
    found = await store.find("k", has_request_id=False)
    assert found is not None and found.status == "succeeded"
    other = await accept(store, "other", run_id="r9", wf_run="w9")
    await store.fail(other.id, "demo", REFUSED)
    assert await store.find("other", has_request_id=False) is None


async def test_retention_none_keeps_every_row_and_a_number_prunes_older_finished_rows(
    store: PrintRunStore,
) -> None:
    old = await accept(store, "old", run_id="old", wf_run="w-old")
    await store.succeed(old.id, "demo", RESULT)
    await accept(store, "a", run_id="a", wf_run="w-a")
    assert await store.get("old") is not None

    await asyncio.sleep(0.05)
    await store.insert_accepted(
        "b",
        subject=PrintSubject.output(OUTPUT),
        key="b",
        slug="demo",
        workflow_id="print-b",
        workflow_run_id="w-b",
        retention=timedelta(milliseconds=10),
    )
    assert await store.get("old") is None
    assert await store.get("a") is not None  # still running: never pruned


async def test_get_reads_a_row_and_an_unknown_id_is_none(store: PrintRunStore) -> None:
    run = await accept(store)
    got = await store.get(run.id)
    assert got is not None and got.output_id == OUTPUT
    assert await store.get("nope") is None


async def test_a_run_reads_back_its_subject_and_the_output_id_it_always_answered(
    store: PrintRunStore,
) -> None:
    # #1750: `subject` is the key; `output_id` stays what clients read before it, an
    # output's bare id and a library file's `library:<file id>`.
    output = await accept(store, "k1", run_id="r1")
    library = await store.insert_accepted(
        "r2",
        subject=PrintSubject.library(41),
        key="k2",
        slug="library-41",
        workflow_id="print-k2",
        workflow_run_id="w2",
        retention=None,
    )
    for run, subject, output_id in (
        (output, f"output:{OUTPUT}", OUTPUT),
        (library, "library:41", "library:41"),
    ):
        got = await store.get(run.id)
        assert got is not None
        assert (got.subject, got.output_id) == (subject, output_id)
        assert got.model_dump(mode="json")["output_id"] == output_id
        assert got.model_dump(mode="json")["subject"] == subject


def test_a_run_from_before_1750_still_reads() -> None:
    # Temporal history and the accept Update carry runs serialized with `output_id` only.
    old = {
        "id": "r1",
        "output_id": OUTPUT,
        "status": "running",
        "created_at": "2026-10-06T10:00:00Z",
    }
    run = PrintRun.model_validate(old)
    assert (run.subject, run.output_id) == (f"output:{OUTPUT}", OUTPUT)
    assert PrintRun.model_validate(run.model_dump(mode="json")) == run


async def test_fail_lost_says_whether_anything_could_have_been_queued(
    store: PrintRunStore, events: Events
) -> None:
    """Review #1061 F1: a run whose execution ended without recording an outcome."""
    unqueued = await accept(store, "k1", run_id="r1")
    queueing = await accept(store, "k2", run_id="r2")
    await store.start_enqueue(queueing.id)

    first = await store.fail_lost(unqueued.id)
    second = await store.fail_lost(queueing.id)

    assert first.status == second.status == "failed"
    assert first.error == LOST_UNQUEUED and not first.may_have_queued
    assert second.error == LOST and second.may_have_queued
    announced = [event for event in events.published if isinstance(event, PrintRunEvent)]
    assert [event.slug for event in announced[-2:]] == ["demo", "demo"]


async def test_fail_lost_leaves_an_ended_run_alone(store: PrintRunStore) -> None:
    run = await accept(store)
    await store.succeed(run.id, "demo", RESULT)
    assert (await store.fail_lost(run.id)).status == "succeeded"


async def test_running_executions_names_each_running_rows_execution(
    store: PrintRunStore,
) -> None:
    running = await accept(store, "k1", run_id="r1", wf_run="w1")
    done = await accept(store, "k2", run_id="r2", wf_run="w2")
    await store.succeed(done.id, "demo", RESULT)

    assert await store.running_executions(timedelta(0)) == [(running.id, "print-k1", "w1")]
    assert await store.running_executions(timedelta(hours=1)) == []


def test_the_migration_says_every_run_it_ends_may_have_queued(
    jobs: JobProjection,
) -> None:
    """Review #1061 (third) 1: during a rolling update an old pod may still queue a run
    the migration ends, so no row it ends says nothing was queued."""
    migration = (MIGRATIONS_DIR / "20261003T0223Z_print_runs_on_temporal.sql").read_text()
    update = migration[migration.index("UPDATE print_runs") :]
    with jobs.pool.connection() as conn:
        for run_id, attempted in (("r1", False), ("r2", True)):
            conn.execute(
                "INSERT INTO print_runs (id, output_id, idempotency_key, status,"
                " enqueue_attempted) VALUES (%s, %s, %s, 'running', %s)",
                (run_id, OUTPUT, run_id, attempted),
            )
        conn.execute(update)
        rows = conn.execute(
            "SELECT error->>'detail' AS detail, enqueue_attempted FROM print_runs"
        ).fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["enqueue_attempted"]
        assert "check Bambuddy's queue before printing again" in row["detail"]


async def test_reconcile_fails_the_runs_whose_execution_is_gone_or_closed(
    store: PrintRunStore,
) -> None:
    """Review #1061 F1: a terminated execution, or one that is gone, never ends its
    row itself; one still running is left to end it."""
    async with temporal_client() as client:
        queue = f"unserved-{uuid.uuid4().hex[:8]}"
        live = await client.start_workflow(
            "PrintRun", "x", id=f"print-live-{uuid.uuid4().hex[:8]}", task_queue=queue
        )
        killed = await client.start_workflow(
            "PrintRun", "x", id=f"print-killed-{uuid.uuid4().hex[:8]}", task_queue=queue
        )
        await killed.terminate("an operator ended it")
        for run_id, handle in (("live", live), ("killed", killed)):
            await store.insert_accepted(
                run_id,
                subject=PrintSubject.output(OUTPUT),
                key=run_id,
                slug="demo",
                workflow_id=handle.id,
                workflow_run_id=handle.result_run_id or "",
                retention=None,
            )
        await accept(store, "gone", run_id="gone", wf_run=str(uuid.uuid4()))
        try:
            ended = await reconcile_lost_runs(client, store, older_than=timedelta(0))
        finally:
            await live.terminate("test over")
    assert ended == 2
    assert (await store.get("live")).status == "running"  # type: ignore[union-attr]
    assert (await store.get("killed")).error == LOST_UNQUEUED  # type: ignore[union-attr]
    assert (await store.get("gone")).status == "failed"  # type: ignore[union-attr]


async def test_reconcile_leaves_a_run_whose_workflow_was_reset_and_still_runs(
    store: PrintRunStore,
) -> None:
    """Review #1061 (third) 2: a reset terminates the run that inserted the row and
    goes on under a new run id with the same row; the row is not lost."""
    async with temporal_client() as client:
        queue = f"unserved-{uuid.uuid4().hex[:8]}"
        workflow_id = f"print-reset-{uuid.uuid4().hex[:8]}"
        first = await client.start_workflow("PrintRun", "x", id=workflow_id, task_queue=queue)
        await first.terminate("reset")
        reset = await client.start_workflow("PrintRun", "x", id=workflow_id, task_queue=queue)
        await store.insert_accepted(
            "reset",
            subject=PrintSubject.output(OUTPUT),
            key="reset",
            slug="demo",
            workflow_id=workflow_id,
            workflow_run_id=first.result_run_id or "",
            retention=None,
        )
        try:
            ended = await reconcile_lost_runs(client, store, older_than=timedelta(0))
        finally:
            await reset.terminate("test over")
    assert ended == 0
    assert (await store.get("reset")).status == "running"  # type: ignore[union-attr]


async def _unavailable() -> Any:
    raise RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b"")


async def _hangs() -> Any:
    await asyncio.Event().wait()


async def _no_namespace() -> Any:
    raise namespace_not_found_error()


async def _exhausted() -> Any:
    raise RPCError("namespace rate limit exceeded", RPCStatusCode.RESOURCE_EXHAUSTED, b"")


async def _denied() -> Any:
    raise RPCError("request unauthorized", RPCStatusCode.PERMISSION_DENIED, b"")


async def _unauthenticated() -> Any:
    raise RPCError("missing credentials", RPCStatusCode.UNAUTHENTICATED, b"")


@pytest.mark.parametrize(
    ("describe", "level"),
    [
        (_unavailable, logging.WARNING),
        (_hangs, logging.WARNING),
        (_no_namespace, logging.WARNING),
        (_exhausted, logging.WARNING),
        # Temporal refused: about Temporal, not the row, so it stops the pass too, but
        # once, at ERROR (review #1316 (11) 2, (12) 1).
        (_denied, logging.ERROR),
        (_unauthenticated, logging.ERROR),
    ],
    ids=["unavailable", "hangs", "namespace", "exhausted", "denied", "unauthenticated"],
)
async def test_temporal_down_stops_the_pass_once(
    store: PrintRunStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    describe: Any,
    level: int,
) -> None:
    """Review #1316 (10) 1: a Temporal that does not answer stops the pass at the first
    describe, logged once, and each describe is bounded. A missing namespace's NOT_FOUND
    is about Temporal, not the execution: it never ends a live row. Every other failure
    of the describe is about Temporal too, read as the route reads it (review #1316 (12)
    1)."""
    monkeypatch.setattr(client_module, "DESCRIBE_SECONDS", 0.05)
    monkeypatch.setattr(client_module, "CONNECT_MARGIN_SECONDS", 0.05)
    for run_id in ("one", "two", "three"):
        await accept(store, run_id, run_id=run_id, wf_run=str(uuid.uuid4()))
    client = DownClient(describe)

    with caplog.at_level(logging.WARNING, logger=client_module.__name__):
        ended = await reconcile_lost_runs(cast(Any, client), store, older_than=timedelta(0))

    assert ended == 0
    assert (await store.get("one")).status == "running"  # type: ignore[union-attr]
    assert (await store.get("two")).status == "running"  # type: ignore[union-attr]
    assert len(client.timeouts) == 1 and client.timeouts[0] is not None
    records = [r for r in caplog.records if r.name == client_module.__name__]
    assert [r.levelno for r in records] == [level]
