"""``workflow_definitions`` and ``workflow_runs`` (#1057, spec 2026-10-01 §7.3).

A definition is written by its route. A run's row is written only by the run's own
workflow (plan 2026-10-09-durable-phase-6-flows.md Ruling 7), each write with its
``flow_run.changed`` event in the same transaction, and only by the sweep otherwise
(``mark_terminated``, ``follow_run``).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.runs import TransactionalEvents
from scadbuddy.core.events import FlowRunEvent
from scadbuddy.flows.manifest import OVERRIDES_SETTING
from scadbuddy.flows.models import (
    RESULT_MAX,
    TERMINAL,
    Decision,
    Definition,
    DefinitionSummary,
    Run,
)
from scadbuddy.workflows.flow_models import FlowRecord, FlowStep, FlowWaiting, ProjectionWrite

_DECISION = (
    "request_id, run_id, workflow_run_id, call_id, kind, outcome, response, responder, created_at"
)
_DEFINITION = "id, name, version, script, approval_timeout_s, created_by, created_at"
_SUMMARY = "id, name, version, approval_timeout_s, created_by, created_at"
_RUN = (
    "id, definition_id, version, name, status, waiting_on, steps, result,"
    " result_truncated, approval_timeout_s, workflow_id, workflow_run_id, started_by,"
    " created_at, updated_at"
)


#: How long the row stays locked waiting for Temporal's Reset to answer.
RESET_CALL_S = 30.0


class ResetSupersededError(Exception):
    """A different Reset replaced the execution first; ``run`` is the row as it moved."""

    def __init__(self, run: Run) -> None:
        super().__init__(f"run {run.id} was reset by another Reset first")
        self.run = run


def _done(row: DictRow) -> list[FlowWaiting]:
    """The row's resolved entries."""
    return [FlowWaiting.model_validate(w) for w in row["waiting_done"]]


class FlowStore:
    def __init__(
        self,
        pool: ConnectionPool[Connection[DictRow]],
        *,
        events: TransactionalEvents | None = None,
    ) -> None:
        self._pool = pool
        self.events = events

    # -- definitions ------------------------------------------------------------

    async def create_definition(
        self,
        name: str,
        script: str,
        created_by: dict[str, Any],
        *,
        approval_timeout_s: int | None = None,
    ) -> Definition:
        """A new version of ``name``; the first is 1."""
        return await asyncio.to_thread(
            self._create_definition, name, script, created_by, approval_timeout_s
        )

    async def get_definition(self, definition_id: str) -> Definition | None:
        return await asyncio.to_thread(self._get_definition, definition_id)

    async def list_definitions(self) -> list[DefinitionSummary]:
        """The newest version of each name, by name."""
        return await asyncio.to_thread(self._list_definitions)

    # -- runs -------------------------------------------------------------------

    async def get_run(self, run_id: str) -> Run | None:
        return await asyncio.to_thread(self._get_run, run_id)

    async def list_runs(
        self, *, definition_id: str | None = None, session: str | None = None, limit: int = 50
    ) -> list[Run]:
        """The newest runs first; ``session`` is the agent session that started them."""
        return await asyncio.to_thread(self._list_runs, definition_id, session, limit)

    async def insert_run(self, record: FlowRecord) -> Run:
        """The run's row; the existing one when a retried activity inserts it again,
        announced only when inserted."""
        return await asyncio.to_thread(self._insert_run, record)

    async def project(self, write: ProjectionWrite) -> Run | None:
        """Apply one of the run's own writes. A finished run is not moved again, except
        by a Reset's write. ``None`` when the run has no row."""
        return await asyncio.to_thread(self._project, write)

    async def reset_run(
        self,
        run_id: str,
        *,
        replaced: str,
        point: int,
        request_id: str,
        reset: Callable[[], Coroutine[Any, Any, str]],
    ) -> Run | None:
        """Reset the run's execution `replaced` to event `point` with `reset` (which
        answers the new execution's run id), and move the row with it, holding the row's
        lock throughout: the new execution's first writes wait for it, and a failed
        `reset` leaves the row as it was. Steps started after `point` are dropped, steps
        that ended after it run again, and calls parked at `point` wait again. Writes
        from `replaced` are ignored from now on. ``None`` when the run has no row; the
        row unchanged when this `request_id` reset `replaced` already (a retry), and
        `ResetSupersededError` when a different Reset did.

        A Reset that landed although this call failed (the RPC timed out, or the worker
        died before the commit) leaves the row rolled back while the new execution
        writes to it. The retry's `reset` answers the same execution (Temporal dedupes
        on the request id), and what that execution recorded is kept as it is."""
        loop = asyncio.get_running_loop()

        def call() -> str:
            future = asyncio.run_coroutine_threadsafe(reset(), loop)
            try:
                return future.result(RESET_CALL_S)
            except TimeoutError:
                future.cancel()
                raise

        return await asyncio.to_thread(self._reset_run, run_id, replaced, point, request_id, call)

    async def open_runs(self, older_than: timedelta) -> list[Run]:
        """Runs still ``running`` or ``waiting`` whose row has not moved for
        ``older_than``: the sweep's candidates."""
        return await asyncio.to_thread(self._open_runs, older_than)

    async def mark_terminated(self, run_id: str) -> Run | None:
        """End an open run whose execution closed without finishing it (the sweep)."""
        return await asyncio.to_thread(self._mark_terminated, run_id)

    async def follow_run(self, run_id: str, workflow_run_id: str) -> None:
        """Record the execution's current run id (a Reset the row has not seen yet)."""
        await asyncio.to_thread(self._follow_run, run_id, workflow_run_id)

    async def delete_run(self, run_id: str) -> bool:
        """Delete the run's row; False when there was none."""
        return await asyncio.to_thread(self._delete_run, run_id)

    # -- decisions ---------------------------------------------------------------

    async def record_decision(self, decision: Decision) -> bool:
        """Record a person's answer before the harness is told. True when recorded, or
        when this same decision is there already (a retried run resends it); False when
        another decision holds the request id."""
        return await asyncio.to_thread(self._record_decision, decision)

    async def get_decision(self, request_id: str) -> Decision | None:
        return await asyncio.to_thread(self._get_decision, request_id)

    async def delete_decision(self, request_id: str) -> None:
        """Undo a decision the harness refused: no `approved` row for a call it never ran."""
        await asyncio.to_thread(self._delete_decision, request_id)

    async def tool_overrides(self) -> Any:
        """Settings' raised tool tiers (the agent's `ai_settings` row), or None."""
        return await asyncio.to_thread(self._tool_overrides)

    # -- implementation -----------------------------------------------------------

    def _tool_overrides(self) -> Any:
        with self._pool.connection() as conn:
            try:
                row = conn.execute(
                    "SELECT value FROM ai_settings WHERE key = %s", (OVERRIDES_SETTING,)
                ).fetchone()
            except psycopg.errors.UndefinedTable:
                # No agent service has migrated this database: no overrides.
                return None
        return None if row is None else row["value"]

    def _record_decision(self, decision: Decision) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO workflow_run_decisions (request_id, run_id, workflow_run_id,"
                " call_id, kind, outcome, response, responder)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (request_id) DO NOTHING RETURNING request_id",
                (
                    decision.request_id,
                    decision.run_id,
                    decision.workflow_run_id,
                    decision.call_id,
                    decision.kind,
                    decision.outcome,
                    Jsonb(decision.response),
                    decision.responder,
                ),
            ).fetchone()
            if row is not None:
                return True
            existing = conn.execute(
                f"SELECT {_DECISION} FROM workflow_run_decisions WHERE request_id = %s",
                (decision.request_id,),
            ).fetchone()
        if existing is None:
            return False
        held = Decision.model_validate(existing)
        return (held.outcome, held.response) == (decision.outcome, decision.response)

    def _get_decision(self, request_id: str) -> Decision | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_DECISION} FROM workflow_run_decisions WHERE request_id = %s",
                (request_id,),
            ).fetchone()
        return Decision.model_validate(row) if row else None

    def _delete_decision(self, request_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM workflow_run_decisions WHERE request_id = %s", (request_id,))

    def _announce(self, conn: Connection[DictRow], run: Run) -> None:
        if self.events is not None:
            self.events.publish_in(
                conn,
                FlowRunEvent(run_id=run.id, definition_id=run.definition_id, status=run.status),
            )

    def _create_definition(
        self, name: str, script: str, created_by: dict[str, Any], approval_timeout_s: int | None
    ) -> Definition:
        with self._pool.connection() as conn, conn.transaction():
            # One writer per name at a time, so two registrations never race for a version.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"workflow_definitions:{name}",)
            )
            row = conn.execute(
                "INSERT INTO workflow_definitions"
                " (id, name, version, script, approval_timeout_s, created_by)"
                " SELECT %s, %s, COALESCE(MAX(version), 0) + 1, %s, %s, %s"
                " FROM workflow_definitions WHERE name = %s"
                f" RETURNING {_DEFINITION}",
                (str(uuid.uuid4()), name, script, approval_timeout_s, Jsonb(created_by), name),
            ).fetchone()
        assert row is not None
        return Definition.model_validate(row)

    def _get_definition(self, definition_id: str) -> Definition | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_DEFINITION} FROM workflow_definitions WHERE id = %s",
                (definition_id,),
            ).fetchone()
        return Definition.model_validate(row) if row else None

    def _list_definitions(self) -> list[DefinitionSummary]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT ON (name) {_SUMMARY} FROM workflow_definitions"
                " ORDER BY name, version DESC"
            ).fetchall()
        return [DefinitionSummary.model_validate(row) for row in rows]

    def _get_run(self, run_id: str) -> Run | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_RUN} FROM workflow_runs WHERE id = %s", (run_id,)
            ).fetchone()
        return Run.model_validate(row) if row else None

    def _list_runs(self, definition_id: str | None, session: str | None, limit: int) -> list[Run]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_RUN} FROM workflow_runs"
                " WHERE (%(definition)s::text IS NULL OR definition_id = %(definition)s)"
                " AND (%(session)s::text IS NULL OR started_by ->> 'session' = %(session)s)"
                " ORDER BY created_at DESC LIMIT %(limit)s",
                {"definition": definition_id, "session": session, "limit": limit},
            ).fetchall()
        return [Run.model_validate(row) for row in rows]

    def _insert_run(self, record: FlowRecord) -> Run:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "INSERT INTO workflow_runs (id, definition_id, version, name, status,"
                " approval_timeout_s, workflow_id, workflow_run_id, started_by)"
                " VALUES (%s, %s, %s, %s, 'running', %s, %s, %s, %s)"
                f" ON CONFLICT (id) DO NOTHING RETURNING {_RUN}",
                (
                    record.run_id,
                    record.definition_id,
                    record.version,
                    record.name,
                    record.approval_timeout_s,
                    record.workflow_id,
                    record.workflow_run_id,
                    Jsonb(record.started_by),
                ),
            ).fetchone()
            if row is not None:
                run = Run.model_validate(row)
                self._announce(conn, run)
                return run
            existing = conn.execute(
                f"SELECT {_RUN} FROM workflow_runs WHERE id = %s", (record.run_id,)
            ).fetchone()
        assert existing is not None  # the conflict was on this id
        return Run.model_validate(existing)

    def _project(self, write: ProjectionWrite) -> Run | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                f"SELECT {_RUN}, waiting_done, %s = ANY(superseded_run_ids) AS superseded"
                " FROM workflow_runs WHERE id = %s FOR UPDATE",
                (write.workflow_run_id, write.run_id),
            ).fetchone()
            if row is None:
                return None
            run = Run.model_validate(row)
            if run.status in TERMINAL and not write.reset:
                return run
            if row["superseded"]:
                # A write a Reset's replaced execution still had in flight.
                return run
            # Merged here, under the row's lock, never by concatenating JSON in SQL.
            steps = {step.call_id: step for step in run.steps}
            if write.step is not None:
                steps[write.step.call_id] = write.step.model_copy(
                    update={"workflow_run_id": write.workflow_run_id}
                )
            waiting = [w for w in run.waiting_on if w.call_id != write.waiting_remove]
            done = _done(row)
            done += [
                w.model_copy(update={"resolved_at": write.history_length})
                for w in run.waiting_on
                if w.call_id == write.waiting_remove
            ]
            if write.waiting_add is not None and all(
                w.call_id != write.waiting_add.call_id for w in waiting
            ):
                waiting.append(
                    write.waiting_add.model_copy(update={"workflow_run_id": write.workflow_run_id})
                )
            if write.status in ("succeeded", "failed"):
                status: str = write.status
            else:
                status = "waiting" if waiting else "running"
            result, truncated = run.result, run.result_truncated
            if write.result is not None:
                result, truncated = write.result[:RESULT_MAX], len(write.result) > RESULT_MAX
            updated = conn.execute(
                "UPDATE workflow_runs SET status = %s, waiting_on = %s, waiting_done = %s,"
                " steps = %s, result = %s, result_truncated = %s, workflow_run_id = %s,"
                f" updated_at = now() WHERE id = %s RETURNING {_RUN}",
                (
                    status,
                    Jsonb([w.model_dump(mode="json") for w in waiting]),
                    Jsonb([w.model_dump(mode="json") for w in done]),
                    Jsonb([s.model_dump(mode="json") for s in sorted(steps.values(), key=_seq)]),
                    result,
                    truncated,
                    write.workflow_run_id,
                    write.run_id,
                ),
            ).fetchone()
            assert updated is not None
            after = Run.model_validate(updated)
            self._announce(conn, after)
        return after

    def _reset_run(
        self, run_id: str, replaced: str, point: int, request_id: str, reset: Callable[[], str]
    ) -> Run | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                f"SELECT {_RUN}, waiting_done, superseded_run_ids, reset_request_ids"
                " FROM workflow_runs WHERE id = %s FOR UPDATE",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            run = Run.model_validate(row)
            if replaced in row["superseded_run_ids"]:
                by = dict(zip(row["superseded_run_ids"], row["reset_request_ids"], strict=False))
                if by.get(replaced) == request_id:
                    return run
                raise ResetSupersededError(run)
            current = reset()

            def own(record: FlowStep | FlowWaiting) -> bool:
                # Written by the new execution already: this Reset landed on an earlier
                # attempt whose commit was lost, and the execution has run since.
                return record.workflow_run_id == current

            # Temporal keeps the history up to the reset task's started event and does
            # that task again (with new randomness): what was recorded in it or after
            # it is done again. A step from before 6e has no end length; it counts as
            # ended before the point, since a step that runs again records its end again.
            redone = point - 1
            steps = [
                step
                if own(step)
                or step.ended_history_length is None
                or step.ended_history_length < redone
                else step.model_copy(
                    update={
                        "status": "running",
                        "ended_at": None,
                        "ended_history_length": None,
                        "error": None,
                    }
                )
                for step in run.steps
                if own(step) or step.history_length < redone
            ]
            waiting = [w for w in run.waiting_on if own(w)]
            done = [w for w in _done(row) if own(w)]
            for w in [*run.waiting_on, *_done(row)]:
                if own(w):
                    continue
                # An entry with no length was parked before 6e; `check_reset` refuses
                # such a run, so this only keeps it waiting as it was.
                if w.history_length is not None and w.history_length >= redone:
                    continue
                if w.resolved_at is not None and w.resolved_at < redone:
                    done.append(w)
                else:
                    waiting.append(w.model_copy(update={"resolved_at": None}))
            updated = conn.execute(
                "UPDATE workflow_runs SET status = %s, waiting_on = %s, waiting_done = %s,"
                " steps = %s, result = NULL, result_truncated = false, workflow_run_id = %s,"
                " superseded_run_ids = array_append(superseded_run_ids, %s),"
                " reset_request_ids = array_append(reset_request_ids, %s),"
                f" updated_at = now() WHERE id = %s RETURNING {_RUN}",
                (
                    "waiting" if waiting else "running",
                    Jsonb([w.model_dump(mode="json") for w in waiting]),
                    Jsonb([w.model_dump(mode="json") for w in done]),
                    Jsonb([s.model_dump(mode="json") for s in steps]),
                    current,
                    replaced,
                    request_id,
                    run_id,
                ),
            ).fetchone()
            assert updated is not None
            after = Run.model_validate(updated)
            self._announce(conn, after)
        return after

    def _open_runs(self, older_than: timedelta) -> list[Run]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_RUN} FROM workflow_runs WHERE status IN ('running', 'waiting')"
                " AND updated_at <= now() - %s ORDER BY updated_at",
                (older_than,),
            ).fetchall()
        return [Run.model_validate(row) for row in rows]

    def _mark_terminated(self, run_id: str) -> Run | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE workflow_runs SET status = 'terminated', waiting_on = '[]',"
                " updated_at = now() WHERE id = %s AND status IN ('running', 'waiting')"
                f" RETURNING {_RUN}",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            run = Run.model_validate(row)
            self._announce(conn, run)
        return run

    def _follow_run(self, run_id: str, workflow_run_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE workflow_runs SET workflow_run_id = %s, updated_at = now() WHERE id = %s",
                (workflow_run_id, run_id),
            )

    def _delete_run(self, run_id: str) -> bool:
        with self._pool.connection() as conn, conn.transaction():
            # Its decisions go by cascade; the operations that made them carry the
            # answers in their requests, so they go too.
            conn.execute("DELETE FROM operations WHERE subject = %s", (f"flow:{run_id}",))
            cur = conn.execute("DELETE FROM workflow_runs WHERE id = %s", (run_id,))
        return cur.rowcount > 0


def _seq(step: Any) -> int:
    return int(step.seq)
