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
from scadbuddy.tasks.models import Task, TaskOpen, TaskState

_COLUMNS = (
    "id, run_id, project_id, title, schema, context, place, ui, expires_at, state, answer,"
    " answered_by, created_at, updated_at"
)
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


class TaskStore:
    def __init__(
        self,
        pool: ConnectionPool[Connection[DictRow]],
        *,
        events: TransactionalEvents | None = None,
    ) -> None:
        self._pool = pool
        self.events = events

    def open(self, task: TaskOpen) -> Task:
        """Insert the task. The same id again returns the row as it is, whatever the run's
        state. A new task needs a run that has not ended: the run's row is read ``FOR
        SHARE`` in the transaction, so a run that ends meanwhile waits for the insert (and
        cancels the task), or ends first and refuses it. Raises `RunNotFoundError` and
        `RunEndedError`."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(f"SELECT {_COLUMNS} FROM tasks WHERE id = %s", (task.id,)).fetchone()
            if row is None:
                run = conn.execute(
                    "SELECT status FROM workflow_runs WHERE id = %s FOR SHARE", (task.run_id,)
                ).fetchone()
                if run is None:
                    raise RunNotFoundError(task.run_id)
                if run["status"] in TERMINAL:
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

    def mark_answered(self, task_id: str, answer: dict[str, Any] | None, by: str | None) -> Task:
        """Answer an open task. A task that is answered already stays as it is when
        ``answer`` is the same, or ``None`` (the caller lost a race and does not know the
        winner's answer); a row answered with no answer takes ``answer``. Anything else
        raises `TaskNotOpenError`. A missing task raises `KeyError`."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["state"] == "open":
                row = conn.execute(
                    "UPDATE tasks SET state = 'answered', answer = %s, answered_by = %s,"
                    f" updated_at = now() WHERE id = %s RETURNING {_COLUMNS}",
                    (Jsonb(answer) if answer is not None else None, by, task_id),
                ).fetchone()
                assert row is not None
                self._announce(conn, row)
            elif row["state"] != "answered":
                raise TaskNotOpenError(row["state"])
            elif answer is not None and row["answer"] is None:
                row = conn.execute(
                    "UPDATE tasks SET answer = %s, answered_by = %s, updated_at = now()"
                    f" WHERE id = %s RETURNING {_COLUMNS}",
                    (Jsonb(answer), by, task_id),
                ).fetchone()
                assert row is not None
            elif answer is not None:
                same = conn.execute(
                    "SELECT answer = %s::jsonb AS same FROM tasks WHERE id = %s",
                    (Jsonb(answer), task_id),
                ).fetchone()
                if same is None or not same["same"]:
                    raise TaskNotOpenError("answered")
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

    def close_or_current(self, task_id: str, state: Literal["expired", "cancelled"]) -> Task | None:
        """`close`, and the row as it stands when it was not open, in one transaction;
        ``None`` for a missing task."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
            ).fetchone()
            if row is not None and row["state"] == "open":
                row = conn.execute(
                    "UPDATE tasks SET state = %s, updated_at = now()"
                    f" WHERE id = %s RETURNING {_COLUMNS}",
                    (state, task_id),
                ).fetchone()
                assert row is not None
                self._announce(conn, row)
        return Task.model_validate(row) if row else None

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

    def _announce(self, conn: Connection[DictRow], row: DictRow) -> None:
        if self.events is not None:
            self.events.publish_in(
                conn,
                TaskChanged(
                    task_id=row["id"], run_id=row["run_id"], state=row["state"], place=row["place"]
                ),
            )
