from __future__ import annotations

import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from scadbuddy.core.events import Event, TaskChanged
from scadbuddy.flows.store import FlowStore
from scadbuddy.tasks.models import TaskOpen
from scadbuddy.tasks.store import RunEndedError, RunNotFoundError, TaskNotOpenError, TaskStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []
        #: The connection's transaction status at each publish.
        self.statuses: list[TransactionStatus] = []

    def publish_in(self, conn: Connection[Any], event: Event) -> None:
        self.events.append(event)
        self.statuses.append(conn.info.transaction_status)

    def changes(self) -> list[tuple[str, str]]:
        return [(e.task_id, e.state) for e in self.events if isinstance(e, TaskChanged)]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def store(pg_pool: PgPool, recorder: Recorder) -> Iterator[TaskStore]:
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO workflow_definitions (id, name, version, created_by)"
            " VALUES ('def-1', 'tasks', 1, '{}')"
        )
        for run in ("run-1", "run-2"):
            conn.execute(
                "INSERT INTO workflow_runs (id, definition_id, version, name, status,"
                " workflow_id, workflow_run_id, started_by)"
                " VALUES (%s, 'def-1', 1, 'tasks', 'waiting', %s, 'x', %s)",
                (run, f"flow-{run}", json.dumps({})),
            )
    yield TaskStore(pg_pool, events=recorder)
    assert set(recorder.statuses) <= {TransactionStatus.INTRANS}


def _task(task_id: str, run_id: str = "run-1", place: str | None = None) -> TaskOpen:
    return TaskOpen(id=task_id, run_id=run_id, title="Ask", schema=SCHEMA, place=place)


def _states(store: TaskStore, run_id: str) -> dict[str, str]:
    return {t.id: t.state for t in store.list(state=None) if t.run_id == run_id}


def test_open_twice_keeps_one_row(store: TaskStore, recorder: Recorder) -> None:
    first = store.open(_task("t1"))
    again = store.open(_task("t1").model_copy(update={"title": "Other"}))
    assert again == first and first.state == "open" and first.title == "Ask"
    assert len(store.list(state=None)) == 1
    assert recorder.changes() == [("t1", "open")]


def test_a_task_round_trips_its_fields(store: TaskStore) -> None:
    task = TaskOpen(
        id="t1",
        run_id="run-1",
        title="Ask",
        schema=SCHEMA,
        context=[{"type": "text", "text": "hi"}],
        place="room/a",
        ui="email",
    )
    got = store.open(task)
    assert got.schema_ == SCHEMA and got.context == task.context
    assert got.place == "room/a" and got.ui == "email" and got.answer is None
    assert store.get("t1") == got and store.get("nope") is None


def test_mark_answered_only_while_open(store: TaskStore, recorder: Recorder) -> None:
    store.open(_task("t1"))
    done = store.mark_answered("t1", {"ok": True}, "u1", "x")
    assert done.state == "answered" and done.answer == {"ok": True} and done.answered_by == "u1"
    with pytest.raises(TaskNotOpenError) as err:
        store.mark_answered("t1", {"ok": False}, "u2", "x")
    assert err.value.state == "answered"
    assert store.get("t1") == done
    assert recorder.changes() == [("t1", "open"), ("t1", "answered")]


def test_the_same_answer_again_changes_nothing_and_publishes_nothing(
    store: TaskStore, recorder: Recorder
) -> None:
    store.open(_task("t1"))
    done = store.mark_answered("t1", {"ok": True}, "u1", "x")
    recorder.events.clear()
    assert store.mark_answered("t1", {"ok": True}, "u2", "x") == done
    assert store.mark_answered("t1", {"ok": True}, None, "x") == done
    assert recorder.events == []


def test_the_same_answer_again_fills_in_who_gave_it(store: TaskStore, recorder: Recorder) -> None:
    """A mark that does not know who answered may come first."""
    store.open(_task("t1"))
    assert store.mark_answered("t1", {"ok": True}, None, "x").answered_by is None
    recorder.events.clear()
    with pytest.raises(TaskNotOpenError):
        store.mark_answered("t1", {"ok": False}, "u1", "x")
    got = store.mark_answered("t1", {"ok": True}, "u1", "x")
    assert (got.state, got.answer, got.answered_by) == ("answered", {"ok": True}, "u1")
    assert store.get("t1") == got
    assert recorder.events == []


def test_a_task_cancelled_before_its_mark_is_answered(store: TaskStore, recorder: Recorder) -> None:
    """The run took the answer and ended before the mark."""
    store.open(_task("t1"))
    store.close("t1", "cancelled")
    recorder.events.clear()
    got = store.mark_answered("t1", {"ok": True}, "u1", "x")
    assert (got.state, got.answer, got.answered_by) == ("answered", {"ok": True}, "u1")
    assert recorder.changes() == [("t1", "answered")]


def test_mark_answered_of_an_expired_task_raises_with_its_state(store: TaskStore) -> None:
    store.open(_task("t1"))
    store.close("t1", "expired")
    with pytest.raises(TaskNotOpenError) as err:
        store.mark_answered("t1", {"ok": True}, "u1", "x")
    assert err.value.state == "expired"


@pytest.mark.parametrize("state", ["open", "answered", "cancelled", "expired"])
def test_the_answer_of_another_execution_leaves_the_task_as_it_is(
    store: TaskStore, pg_pool: PgPool, recorder: Recorder, state: str
) -> None:
    store.open(_task("t1"))
    with pg_pool.connection() as conn:
        conn.execute("UPDATE tasks SET state = %s", (state,))
    held = store.get("t1")
    recorder.events.clear()
    assert store.mark_answered("t1", {"ok": True}, "u1", "replaced") == held
    assert store.get("t1") == held
    assert recorder.events == []


def test_mark_answered_of_a_missing_task_raises_a_key_error(store: TaskStore) -> None:
    with pytest.raises(KeyError):
        store.mark_answered("nope", {}, "u1", "x")


def test_close_of_an_answered_task_returns_none(store: TaskStore, recorder: Recorder) -> None:
    store.open(_task("t1"))
    store.mark_answered("t1", {"ok": True}, "u1", "x")
    recorder.events.clear()
    assert store.close("t1", "expired") is None
    assert recorder.events == []
    answered = store.get("t1")
    assert answered is not None and answered.state == "answered"


def test_close_of_an_open_task_sets_the_state(store: TaskStore, recorder: Recorder) -> None:
    store.open(_task("t1", place="p"))
    closed = store.close("t1", "expired")
    assert closed is not None and closed.state == "expired"
    last = recorder.events[-1]
    assert isinstance(last, TaskChanged) and last.state == "expired" and last.place == "p"
    assert last.run_id == "run-1" and last.task_id == "t1"


def test_cancel_for_run_cancels_only_open_tasks_of_that_run(
    store: TaskStore, recorder: Recorder
) -> None:
    for name in ("a", "b", "c"):
        store.open(_task(name))
    store.open(_task("d", run_id="run-2"))
    store.mark_answered("c", {"ok": True}, "u1", "x")
    recorder.events.clear()
    assert store.cancel_for_run("run-1") == 2
    assert _states(store, "run-1") == {"a": "cancelled", "b": "cancelled", "c": "answered"}
    assert _states(store, "run-2") == {"d": "open"}
    assert sorted(recorder.changes()) == [("a", "cancelled"), ("b", "cancelled")]
    assert store.cancel_for_run("run-1") == 0


def test_cancel_for_run_in_joins_the_callers_transaction(
    store: TaskStore, pg_pool: PgPool, recorder: Recorder
) -> None:
    store.open(_task("a"))
    with pytest.raises(RuntimeError), pg_pool.connection() as conn, conn.transaction():
        assert store.cancel_for_run_in(conn, "run-1") == 1
        raise RuntimeError
    assert _states(store, "run-1") == {"a": "open"}


def test_each_write_publishes_inside_its_transaction(
    store: TaskStore, pg_pool: PgPool, recorder: Recorder
) -> None:
    def published() -> list[TransactionStatus]:
        statuses, recorder.statuses = recorder.statuses, []
        return statuses

    inside = [TransactionStatus.INTRANS]
    for name in ("a", "b", "c", "d"):
        store.open(_task(name))
        assert published() == inside
    store.mark_answered("a", {"ok": True}, "u1", "x")
    assert published() == inside
    store.close("b", "expired")
    assert published() == inside
    # An autocommit connection: each method opens its own transaction.
    with pg_pool.connection() as conn:
        store.match_waiting_in(conn, "run-1", waiting_ids={"a", "c"}, done_ids=set())
    assert published() == inside * 2
    with pg_pool.connection() as conn:
        store.cancel_for_run_in(conn, "run-1")
    assert published() == inside * 2
    store.open(_task("e"))
    assert published() == inside
    store.cancel_for_run("run-1")
    assert published() == inside


def test_list_filters_by_place(store: TaskStore) -> None:
    store.open(_task("a", place="room/a"))
    store.open(_task("b", place="room/b"))
    store.open(_task("c"))
    store.mark_answered("c", {"ok": True}, "u1", "x")
    assert [t.id for t in store.list(place="room/a")] == ["a"]
    assert sorted(t.id for t in store.list()) == ["a", "b"]
    assert [t.id for t in store.list(state="answered")] == ["c"]
    assert len(store.list(state=None)) == 3
    assert len(store.list(limit=1)) == 1


def test_match_waiting_in_reconciles_a_reset_run(
    store: TaskStore, pg_pool: PgPool, recorder: Recorder
) -> None:
    names = ("keep", "stray", "wait_answered", "wait_cancelled", "done", "gone")
    for name in names:
        store.open(_task(name))
    store.open(_task("other", run_id="run-2"))
    store.mark_answered("wait_answered", {"ok": True}, "u1", "x")
    store.close("wait_cancelled", "cancelled")
    store.mark_answered("done", {"ok": True}, "u1", "x")
    store.close("gone", "expired")
    recorder.events.clear()
    with pg_pool.connection() as conn, conn.transaction():
        changed = store.match_waiting_in(
            conn,
            "run-1",
            waiting_ids={"keep", "wait_answered", "wait_cancelled"},
            done_ids={"done"},
        )
    assert changed == 3
    assert _states(store, "run-1") == {
        "keep": "open",
        "stray": "cancelled",
        "wait_answered": "open",
        "wait_cancelled": "open",
        "done": "answered",
        "gone": "expired",
    }
    reopened = store.get("wait_answered")
    assert reopened is not None and reopened.answer is None and reopened.answered_by is None
    assert _states(store, "run-2") == {"other": "open"}
    assert sorted(recorder.changes()) == [
        ("stray", "cancelled"),
        ("wait_answered", "open"),
        ("wait_cancelled", "open"),
    ]


def test_the_database_refuses_a_place_with_a_control_character(
    store: TaskStore, pg_pool: PgPool
) -> None:
    store.open(_task("t1"))
    with pytest.raises(Exception, match="tasks_place"), pg_pool.connection() as conn:
        conn.execute("UPDATE tasks SET place = %s", ("a\nb",))


def test_forgetting_a_run_deletes_its_tasks(store: TaskStore, pg_pool: PgPool) -> None:
    store.open(_task("t1"))
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM workflow_runs WHERE id = 'run-1'")
    assert store.get("t1") is None


def test_the_model_refuses_a_bad_place_or_title() -> None:
    for bad in ("", "x" * 101, "a\nb", "a\x00b", "a\x7fb"):
        with pytest.raises(ValueError):
            _task("t", place=bad)
    with pytest.raises(ValueError):
        TaskOpen(id="t", run_id="r", title="", schema=SCHEMA)
    with pytest.raises(ValueError):
        TaskOpen(id="t", run_id="r", title="x" * 501, schema=SCHEMA)
    assert _task("t", place="x" * 100).place == "x" * 100


def test_open_on_an_unknown_run_raises_and_stores_nothing(store: TaskStore) -> None:
    with pytest.raises(RunNotFoundError):
        store.open(_task("t1", "run-9"))
    assert store.list(state=None) == []


@pytest.mark.parametrize("status", ["succeeded", "failed", "terminated"])
def test_open_on_an_ended_run_raises_and_the_same_id_again_answers_its_row(
    store: TaskStore, pg_pool: PgPool, status: str
) -> None:
    first = store.open(_task("t1"))
    with pg_pool.connection() as conn:
        conn.execute("UPDATE workflow_runs SET status = %s", (status,))
    with pytest.raises(RunEndedError):
        store.open(_task("t2"))
    assert store.open(_task("t1")) == first
    assert [t.id for t in store.list(state=None)] == ["t1"]


def _wait_for_block(pg_pool: PgPool, blocker: int, count: int = 1) -> None:
    """Wait until ``count`` backends of this database wait for a lock that the backend
    ``blocker`` holds."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with pg_pool.connection() as conn:
            waiting = conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                " AND %s = ANY(pg_blocking_pids(pid))",
                (blocker,),
            ).fetchone()
        if waiting is not None and waiting["n"] >= count:
            return
        time.sleep(0.05)
    pytest.fail("nothing waits for the row")


def test_a_run_that_ends_while_open_waits_for_its_row_leaves_no_open_task(
    store: TaskStore, pg_pool: PgPool
) -> None:
    with psycopg.connect(pg_pool.conninfo) as ender, ThreadPoolExecutor(1) as pool:
        ender.execute("UPDATE workflow_runs SET status = 'failed' WHERE id = 'run-2'")
        opening = pool.submit(store.open, _task("t1", "run-2"))
        _wait_for_block(pg_pool, ender.info.backend_pid)
        ender.commit()
        with pytest.raises(RunEndedError):
            opening.result(timeout=30)
    assert store.list(state=None) == []


def test_a_retried_open_answers_the_row_the_first_attempt_committed(
    store: TaskStore, pg_pool: PgPool
) -> None:
    with psycopg.connect(pg_pool.conninfo) as ender, ThreadPoolExecutor(1) as pool:
        ender.execute("UPDATE workflow_runs SET status = 'failed' WHERE id = 'run-2'")
        retry = pool.submit(store.open, _task("t1", "run-2"))
        _wait_for_block(pg_pool, ender.info.backend_pid)
        # The first attempt's insert commits while the retry waits, then the run ends.
        with pg_pool.connection() as conn:
            conn.execute(
                "INSERT INTO tasks (id, run_id, title, schema) VALUES ('t1', 'run-2', 'Ask', %s)",
                (json.dumps(SCHEMA),),
            )
        ender.commit()
        answered = retry.result(timeout=30)
    assert answered.id == "t1" and answered.state == "open"


def _hold_run(conn: Connection[Any]) -> None:
    """As `_reset_run` does: the run's row first, then its new execution."""
    conn.execute("SELECT 1 FROM workflow_runs WHERE id = 'run-1' FOR UPDATE")
    conn.execute("UPDATE workflow_runs SET workflow_run_id = 'y' WHERE id = 'run-1'")


def test_a_mark_in_flight_while_a_reset_commits_marks_nothing(
    store: TaskStore, pg_pool: PgPool, recorder: Recorder
) -> None:
    store.open(_task("t1"))
    marks: list[Any] = []
    with ThreadPoolExecutor(1) as pool:

        def reset() -> str:
            # `_reset_run` holds the run's row here.
            with pg_pool.connection() as conn:
                [held] = conn.execute(
                    "SELECT pid FROM pg_locks WHERE relation = 'workflow_runs'::regclass"
                    " AND database = (SELECT oid FROM pg_database"
                    " WHERE datname = current_database())"
                    " AND mode = 'RowShareLock' AND pid <> pg_backend_pid()"
                ).fetchall()
            marks.append(pool.submit(store.mark_answered, "t1", {"ok": True}, "u1", "x"))
            _wait_for_block(pg_pool, held["pid"])
            return "y"

        run = FlowStore(pg_pool)._reset_run("run-1", "x", 10, "req-1", reset)
        assert run is not None and run.workflow_run_id == "y"
        left = marks[0].result(timeout=30)
    assert (left.state, left.answer) == ("open", None)
    assert store.get("t1") == left
    assert recorder.changes() == [("t1", "open")]


def test_a_mark_and_an_open_that_wait_for_a_reset_do_not_deadlock_with_it(
    store: TaskStore, pg_pool: PgPool, pg_conninfo: str
) -> None:
    """Each takes the run's row first, then the tasks'."""
    store.open(_task("t1"))
    with (
        ThreadPoolExecutor(2) as pool,
        psycopg.connect(pg_conninfo, row_factory=dict_row) as resetter,
    ):
        _hold_run(resetter)
        marking = pool.submit(store.mark_answered, "t1", {"ok": True}, "u1", "x")
        opening = pool.submit(store.open, _task("t2"))
        _wait_for_block(pg_pool, resetter.info.backend_pid, count=2)
        # A mark that held its task's row would block this, and wait for the run's.
        assert store.match_waiting_in(resetter, "run-1", set(), set()) == 1
        resetter.commit()
        assert marking.result(timeout=30).state == "cancelled"
        assert opening.result(timeout=30).state == "open"
    assert _states(store, "run-1") == {"t1": "cancelled", "t2": "open"}


def test_a_reset_waits_for_a_mark_in_flight(
    store: TaskStore, pg_pool: PgPool, pg_conninfo: str
) -> None:
    """The mark holds the run's row, so the Reset sees it done and opens the task again."""
    store.open(_task("t1"))
    with (
        ThreadPoolExecutor(2) as pool,
        psycopg.connect(pg_conninfo) as holder,
        psycopg.connect(pg_conninfo, row_factory=dict_row) as resetter,
    ):
        # The mark waits here, with the run's row locked.
        holder.execute("SELECT 1 FROM tasks WHERE id = 't1' FOR UPDATE")
        marking = pool.submit(store.mark_answered, "t1", {"ok": True}, "u1", "x")
        _wait_for_block(pg_pool, holder.info.backend_pid)

        def reset() -> int:
            _hold_run(resetter)
            changed = store.match_waiting_in(resetter, "run-1", {"t1"}, set())
            resetter.commit()
            return changed

        resetting = pool.submit(reset)
        time.sleep(0.5)
        assert not resetting.done()
        holder.commit()
        assert marking.result(timeout=30).state == "answered"
        assert resetting.result(timeout=30) == 1
    assert _states(store, "run-1") == {"t1": "open"}


def test_close_or_current_closes_an_open_task_and_answers_the_row_as_it_is(
    store: TaskStore, recorder: Recorder
) -> None:
    store.open(_task("t1"))
    store.open(_task("t2"))
    store.mark_answered("t2", {"ok": True}, None, "x")
    closed = store.close_or_current("t1", "expired")
    assert closed is not None and closed.state == "expired"
    held = store.close_or_current("t2", "cancelled")
    assert held is not None and held.state == "answered"
    assert store.close_or_current("nope", "expired") is None
    assert recorder.changes() == [
        ("t1", "open"),
        ("t2", "open"),
        ("t2", "answered"),
        ("t1", "expired"),
    ]


def test_the_same_answer_with_a_big_number_is_the_same_answer(store: TaskStore) -> None:
    store.open(_task("t1"))
    first = store.mark_answered("t1", {"n": 1e23}, "alice", "x")
    assert store.mark_answered("t1", {"n": 1e23}, "alice", "x") == first
    with pytest.raises(TaskNotOpenError):
        store.mark_answered("t1", {"n": 2e23}, "alice", "x")


def test_the_model_refuses_a_big_context_or_ui() -> None:
    big = [{"type": "text", "text": "x" * 16_400}]
    with pytest.raises(ValueError):
        TaskOpen(id="t", run_id="r", title="t", schema=SCHEMA, context=big)
    with pytest.raises(ValueError):
        TaskOpen(id="t", run_id="r", title="t", schema=SCHEMA, ui="x" * 201)
    ok = TaskOpen(id="t", run_id="r", title="t", schema=SCHEMA, ui="x" * 200)
    assert ok.ui == "x" * 200
