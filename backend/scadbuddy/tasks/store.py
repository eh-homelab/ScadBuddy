"""``tasks`` (#7364): the typed questions flows ask, each state change with its
``task.changed`` event in the same transaction."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.runs import TransactionalEvents
from scadbuddy.core.events import TaskChanged
from scadbuddy.flows.models import TERMINAL
from scadbuddy.tasks.models import Task, TaskOpen, TaskQuestion, TaskState

_QUESTION = (
    "id, run_id, project_id, title, schema, context, place, ui, expires_at, state,"
    " created_at, updated_at"
)
_COLUMNS = f"{_QUESTION}, answer, answered_by"
_MOVED = "id, run_id, state, place"


class TaskNotOpenError(Exception):
    """The task is not open, so it takes no answer; ``state`` is what it is."""

    def __init__(self, state: TaskState) -> None:
        super().__init__(f"the task is {state}")
        self.state = state


class RunNotFoundError(Exception):
    """The task's run does not exist."""


class RunEndedError(Exception):
    """The task's run has ended, so it opens no task."""


class RunBehindError(Exception):
    """The row of an open run does not know the caller's execution yet: a Reset made in
    Temporal, which the sweep has not followed. The same write again is taken once it has,
    and answered with the row as it stands if the run ends first."""


class TaskStore:
    def __init__(
        self,
        pool: ConnectionPool[Connection[DictRow]],
        *,
        events: TransactionalEvents | None = None,
    ) -> None:
        self._pool = pool
        self.events = events

    def open(self, task: TaskOpen, *, workflow_run_id: str | None = None) -> Task:
        """Insert the task. The same id again returns the row as it is, whatever the run's
        state. A new task needs a run that has not ended: the run's row is read ``FOR
        SHARE`` in the transaction, so a run that ends meanwhile waits for the insert (and
        cancels the task), or ends first and refuses it. An execution ``workflow_run_id``
        that a Reset replaced opens none: its run goes on without it. Raises
        `RunNotFoundError` and `RunEndedError`."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(f"SELECT {_COLUMNS} FROM tasks WHERE id = %s", (task.id,)).fetchone()
            if row is None:
                run = conn.execute(
                    "SELECT status, coalesce(%s = ANY(superseded_run_ids), false) AS replaced"
                    " FROM workflow_runs WHERE id = %s FOR SHARE",
                    (workflow_run_id, task.run_id),
                ).fetchone()
                if run is None:
                    raise RunNotFoundError(task.run_id)
                if run["replaced"] or run["status"] in TERMINAL:
                    # An earlier attempt may have committed while this one waited.
                    held = conn.execute(
                        f"SELECT {_COLUMNS} FROM tasks WHERE id = %s", (task.id,)
                    ).fetchone()
                    if held is None:
                        raise RunEndedError(task.run_id)
                    return Task.model_validate(held)
                row = conn.execute(
                    "INSERT INTO tasks (id, run_id, title, schema, context, place, ui, expires_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING"
                    f" RETURNING {_COLUMNS}",
                    (
                        task.id,
                        task.run_id,
                        task.title,
                        Jsonb(task.schema_),
                        Jsonb(task.context),
                        task.place,
                        task.ui,
                        task.expires_at,
                    ),
                ).fetchone()
                if row is None:
                    row = conn.execute(
                        f"SELECT {_COLUMNS} FROM tasks WHERE id = %s", (task.id,)
                    ).fetchone()
                else:
                    self._announce(conn, row)
        assert row is not None
        return Task.model_validate(row)

    def get(self, task_id: str) -> Task | None:
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM tasks WHERE id = %s", (task_id,)).fetchone()
        return Task.model_validate(row) if row else None

    def of_run(self, run_id: str, ids: Collection[str]) -> dict[str, TaskQuestion]:
        """The run's tasks among ``ids``, by id, without their answers."""
        if not ids:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_QUESTION} FROM tasks WHERE run_id = %s AND id = ANY(%s::text[])",
                (run_id, [*ids]),
            ).fetchall()
        return {row["id"]: TaskQuestion.model_validate(row) for row in rows}

    def list(
        self, *, state: TaskState | None = "open", place: str | None = None, limit: int = 100
    ) -> list[Task]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM tasks"
                " WHERE (%(state)s::text IS NULL OR state = %(state)s)"
                " AND (%(place)s::text IS NULL OR place = %(place)s)"
                " ORDER BY created_at, id LIMIT %(limit)s",
                {"state": state, "place": place, "limit": limit},
            ).fetchall()
        return [Task.model_validate(row) for row in rows]

    def mark_answered(
        self,
        task_id: str,
        answer: dict[str, Any],
        by: str | None,
        workflow_run_id: str,
        *,
        final: bool = False,
    ) -> Task:
        """Record the answer the run's execution ``workflow_run_id`` took. An open task, and
        one its run's end cancelled before this mark, is answered. The same answer again
        changes nothing but a missing ``by``, which it fills in. A task with another answer
        and an expired one raise `TaskNotOpenError`. A missing task raises `KeyError`.

        A Reset undoes an answer, so when the run is at another execution the task is
        answered as it stands. The run's row is locked before the task's, as `open` and
        `FlowStore._reset_run` do: a Reset under way commits first, and a later one waits.

        ``final`` is the run's own mark, of the answer its script gets. It replaces another
        answer and who gave it, and leaves an expired task as it stands. From an execution
        that the row of an open run does not know yet, it raises `RunBehindError`. From one
        that the row of an ended run does not know, it is answered with the task as it
        stands: nothing follows an ended run, so a refusal would never end."""
        with self._pool.connection() as conn, conn.transaction():
            run = self._run_of(conn, task_id, workflow_run_id)
            row = conn.execute(
                f"SELECT {_COLUMNS}, answer = %s::jsonb AS same FROM tasks"
                " WHERE id = %s FOR UPDATE",
                (Jsonb(answer), task_id),
            ).fetchone()
            if run is None or row is None:
                raise KeyError(task_id)
            if not run["current"]:
                if final and run["behind"]:
                    raise RunBehindError(task_id)
                return Task.model_validate(row)
            state, same = row["state"], row["state"] == "answered" and row["same"]
            if state == "expired" and final:
                return Task.model_validate(row)
            if state == "expired" or (state == "answered" and not same and not final):
                raise TaskNotOpenError(state)
            if not same or (row["answered_by"] is None and by is not None):
                row = conn.execute(
                    "UPDATE tasks SET state = 'answered', answer = %s, answered_by = %s,"
                    f" updated_at = now() WHERE id = %s RETURNING {_COLUMNS}",
                    (Jsonb(answer), by, task_id),
                ).fetchone()
                assert row is not None
                if state != "answered":
                    self._announce(conn, row)
        return Task.model_validate(row)

    def close(self, task_id: str, state: Literal["expired", "cancelled"]) -> Task | None:
        """End an open task; ``None`` when it was not open."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE tasks SET state = %s, updated_at = now()"
                f" WHERE id = %s AND state = 'open' RETURNING {_COLUMNS}",
                (state, task_id),
            ).fetchone()
            if row is not None:
                self._announce(conn, row)
        return Task.model_validate(row) if row else None

    def close_or_current(
        self,
        task_id: str,
        state: Literal["expired", "cancelled"],
        *,
        workflow_run_id: str | None = None,
    ) -> Task | None:
        """`close`, and the row as it stands when it was not open, in one transaction;
        ``None`` for a missing task.

        With ``workflow_run_id`` it is that execution's own close of a question it took no
        answer to, so it replaces an answer too. The executions are as `mark_answered`'s
        with ``final``, and so is the order of the locks."""
        with self._pool.connection() as conn, conn.transaction():
            closes: tuple[TaskState, ...] = ("open",)
            if workflow_run_id is not None:
                run = self._run_of(conn, task_id, workflow_run_id)
                if run is None:
                    return None
                if run["behind"]:
                    raise RunBehindError(task_id)
                closes = ("open", "answered") if run["current"] else ()
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if row is not None and row["state"] in closes:
                row = conn.execute(
                    "UPDATE tasks SET state = %s, answer = NULL, answered_by = NULL,"
                    f" updated_at = now() WHERE id = %s RETURNING {_COLUMNS}",
                    (state, task_id),
                ).fetchone()
                assert row is not None
                self._announce(conn, row)
        return Task.model_validate(row) if row else None

    def reopen_in(self, conn: Connection[DictRow], run_id: str, task_id: str) -> None:
        """Open the run's task again, with no answer, if it is not open: the run waits for
        it. In the caller's transaction, with the caller's order as `match_waiting_in`."""
        with conn.transaction():
            row = conn.execute(
                "UPDATE tasks SET state = 'open', answer = NULL, answered_by = NULL,"
                " updated_at = now() WHERE id = %s AND run_id = %s AND state <> 'open'"
                f" RETURNING {_MOVED}",
                (task_id, run_id),
            ).fetchone()
            if row is not None:
                self._announce(conn, row)

    def cancel_for_run(self, run_id: str) -> int:
        with self._pool.connection() as conn:
            return self.cancel_for_run_in(conn, run_id)

    def cancel_for_run_in(self, conn: Connection[DictRow], run_id: str) -> int:
        """Cancel every open task of the run, in the caller's transaction. Call this
        before the caller publishes its own event. Publishing takes the event log's
        advisory lock, and this takes row locks. `mark_answered` takes them in the other
        order, so after a publish the two deadlock."""
        with conn.transaction():
            rows = conn.execute(
                "UPDATE tasks SET state = 'cancelled', updated_at = now()"
                f" WHERE run_id = %s AND state = 'open' RETURNING {_MOVED}",
                (run_id,),
            ).fetchall()
            for row in rows:
                self._announce(conn, row)
            return len(rows)

    def match_waiting_in(
        self,
        conn: Connection[DictRow],
        run_id: str,
        waiting_ids: Collection[str],
        done_ids: Collection[str],
    ) -> int:
        """Make the run's tasks match a run that was reset, in the caller's transaction.
        An open task in neither set is cancelled. A task in ``waiting_ids`` that is not
        open opens again, with no answer. A task in ``done_ids`` stays as it is, and so
        does any other task that is not open. Returns how many rows changed. Call this
        before the caller publishes its own event. Publishing takes the event log's
        advisory lock, and this takes row locks. `mark_answered` takes them in the other
        order, so after a publish the two deadlock."""
        waiting, done = list(waiting_ids), list(done_ids)
        with conn.transaction():
            cancelled = conn.execute(
                "UPDATE tasks SET state = 'cancelled', updated_at = now()"
                " WHERE run_id = %s AND state = 'open'"
                " AND id <> ALL(%s::text[]) AND id <> ALL(%s::text[])"
                f" RETURNING {_MOVED}",
                (run_id, waiting, done),
            ).fetchall()
            reopened = conn.execute(
                "UPDATE tasks SET state = 'open', answer = NULL, answered_by = NULL,"
                " updated_at = now() WHERE run_id = %s AND state <> 'open'"
                " AND id = ANY(%s::text[]) AND id <> ALL(%s::text[])"
                f" RETURNING {_MOVED}",
                (run_id, waiting, done),
            ).fetchall()
            for row in (*cancelled, *reopened):
                self._announce(conn, row)
            return len(cancelled) + len(reopened)

    def _run_of(
        self, conn: Connection[DictRow], task_id: str, workflow_run_id: str
    ) -> DictRow | None:
        """The task's run, locked ``FOR SHARE``: whether ``workflow_run_id`` is its current
        execution, and whether the row is behind: the run is open, and the execution is not
        one that a Reset replaced either. A Reset under way holds the row, so this reads it
        as that Reset left it."""
        return conn.execute(
            "SELECT workflow_run_id = %(id)s AS current,"
            " workflow_run_id <> %(id)s AND NOT %(id)s = ANY(superseded_run_ids)"
            " AND NOT status = ANY(%(ended)s) AS behind FROM workflow_runs"
            " WHERE id = (SELECT run_id FROM tasks WHERE id = %(task)s) FOR SHARE",
            {"id": workflow_run_id, "task": task_id, "ended": sorted(TERMINAL)},
        ).fetchone()

    def _announce(self, conn: Connection[DictRow], row: DictRow) -> None:
        if self.events is not None:
            self.events.publish_in(
                conn,
                TaskChanged(
                    task_id=row["id"], run_id=row["run_id"], state=row["state"], place=row["place"]
                ),
            )
