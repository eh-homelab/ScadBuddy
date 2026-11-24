from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from psycopg import Connection
from psycopg.pq import TransactionStatus

from scadbuddy.core.events import Event, TaskChanged
from scadbuddy.tasks.models import TaskOpen
from scadbuddy.tasks.store import TaskNotOpenError, TaskStore
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
    done = store.mark_answered("t1", {"ok": True}, "u1")
    assert done.state == "answered" and done.answer == {"ok": True} and done.answered_by == "u1"
    with pytest.raises(TaskNotOpenError) as err:
        store.mark_answered("t1", {"ok": False}, "u2")
    assert err.value.state == "answered"
    assert store.get("t1") == done
    assert recorder.changes() == [("t1", "open"), ("t1", "answered")]


def test_the_same_answer_again_changes_nothing_and_publishes_nothing(
    store: TaskStore, recorder: Recorder
) -> None:
    store.open(_task("t1"))
    done = store.mark_answered("t1", {"ok": True}, "u1")
    recorder.events.clear()
    assert store.mark_answered("t1", {"ok": True}, "u2") == done
    assert store.mark_answered("t1", None, None) == done
    assert recorder.events == []


def test_an_answered_row_with_no_answer_is_filled(store: TaskStore, recorder: Recorder) -> None:
    store.open(_task("t1"))
    first = store.mark_answered("t1", None, None)
    assert first.state == "answered" and first.answer is None
    recorder.events.clear()
    got = store.mark_answered("t1", {"ok": True}, "u1")
    assert got.state == "answered" and got.answer == {"ok": True} and got.answered_by == "u1"
    assert recorder.events == []
    with pytest.raises(TaskNotOpenError):
        store.mark_answered("t1", {"ok": False}, "u1")


def test_mark_answered_of_a_cancelled_task_raises_with_its_state(store: TaskStore) -> None:
    store.open(_task("t1"))
    store.close("t1", "cancelled")
    with pytest.raises(TaskNotOpenError) as err:
        store.mark_answered("t1", {"ok": True}, "u1")
    assert err.value.state == "cancelled"
    with pytest.raises(TaskNotOpenError) as err:
        store.mark_answered("t1", None, None)
    assert err.value.state == "cancelled"


def test_mark_answered_of_a_missing_task_raises_a_key_error(store: TaskStore) -> None:
    with pytest.raises(KeyError):
        store.mark_answered("nope", {}, "u1")


def test_close_of_an_answered_task_returns_none(store: TaskStore, recorder: Recorder) -> None:
    store.open(_task("t1"))
    store.mark_answered("t1", {"ok": True}, "u1")
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
    store.mark_answered("c", {"ok": True}, "u1")
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
    store.mark_answered("a", {"ok": True}, "u1")
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
    store.mark_answered("c", {"ok": True}, "u1")
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
    store.mark_answered("wait_answered", {"ok": True}, "u1")
    store.close("wait_cancelled", "cancelled")
    store.mark_answered("done", {"ok": True}, "u1")
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
