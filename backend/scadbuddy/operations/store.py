"""``operations`` (#1053, spec 2026-10-01 §4.2 "Our record"): one row per execution of
an ``Operation`` workflow, written only by its activities, each write with its
``operation.changed`` event in the same transaction."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Literal

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.bambuddy.runs import DatabaseRequiredError, PrintRunError, TransactionalEvents
from scadbuddy.core.events import OperationEvent

OperationStatus = Literal["running", "succeeded", "failed"]

_COLUMNS = "id, kind, subject, status, result, error, created_at, finished_at"


class Operation(BaseModel):
    """One command, as ``GET /operations/{id}`` reads it."""

    id: str
    #: The kind, e.g. ``reprint``.
    kind: str
    #: What it acted on: an output id, ``archive:<id>``, ``project``, ``sidebar``.
    subject: str
    #: ``running`` until the effect finished (``succeeded``) or failed. Both are final.
    status: OperationStatus
    #: The route's answer body, once ``succeeded``.
    result: dict[str, Any] | None = None
    #: The problem the route answers with, once ``failed``.
    error: PrintRunError | None = None
    created_at: datetime
    finished_at: datetime | None = None
    #: Only on an answer to a route: an earlier command with the same key, so this
    #: request ran nothing.
    repeated: bool = False


class OperationStore:
    def __init__(
        self,
        pool: ConnectionPool[Connection[DictRow]] | None,
        *,
        events: TransactionalEvents | None = None,
    ) -> None:
        self._pool = pool
        self.events = events

    @property
    def available(self) -> bool:
        return self._pool is not None

    def _require(self) -> ConnectionPool[Connection[DictRow]]:
        if self._pool is None:
            raise DatabaseRequiredError
        return self._pool

    async def find(self, key: str) -> Operation | None:
        """The key's newest operation, whatever its status: one key is one effect."""
        return await asyncio.to_thread(self._find, key)

    async def get(self, op_id: str) -> Operation | None:
        return await asyncio.to_thread(self._get, op_id)

    async def named_by_running(self, names: list[str]) -> set[str]:
        """Those of ``names`` that the request of an operation still running contains
        (a claim's name, #1054)."""
        return await asyncio.to_thread(self._named_by_running, names)

    async def insert(
        self,
        op_id: str,
        *,
        kind: str,
        subject: str,
        key: str,
        request: dict[str, Any],
        workflow_id: str,
        workflow_run_id: str,
        retention: timedelta | None,
    ) -> Operation:
        """Record an accepted operation; the execution's row if it has one already (a
        retried activity), announced only when inserted. Prunes operations that finished
        more than ``retention`` ago; ``None`` keeps every one."""
        return await asyncio.to_thread(
            self._insert,
            op_id,
            kind,
            subject,
            key,
            request,
            workflow_id,
            workflow_run_id,
            retention,
        )

    async def finish(
        self,
        op_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: PrintRunError | None = None,
    ) -> Operation:
        """End a running operation; a retried end finds it ended and changes nothing."""
        return await asyncio.to_thread(self._finish, op_id, result, error)

    async def running_executions(self, older_than: timedelta) -> list[tuple[str, str, str]]:
        """``(operation id, workflow id, workflow run id)`` of each operation still
        ``running`` that was accepted more than ``older_than`` ago."""
        return await asyncio.to_thread(self._running_executions, older_than)

    def _announce(self, conn: Connection[DictRow], op: Operation) -> None:
        if self.events is not None:
            self.events.publish_in(
                conn, OperationEvent(operation_id=op.id, op_kind=op.kind, subject=op.subject)
            )

    def _find(self, key: str) -> Operation | None:
        with self._require().connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM operations WHERE idempotency_key = %s"
                " ORDER BY created_at DESC LIMIT 1",
                (key,),
            ).fetchone()
        return Operation.model_validate(row) if row else None

    def _get(self, op_id: str) -> Operation | None:
        with self._require().connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM operations WHERE id = %s", (op_id,)
            ).fetchone()
        return Operation.model_validate(row) if row else None

    def _named_by_running(self, names: list[str]) -> set[str]:
        with self._require().connection() as conn:
            rows = conn.execute(
                "SELECT name FROM unnest(%s::text[]) AS name WHERE EXISTS ("
                " SELECT 1 FROM operations"
                " WHERE status = 'running' AND strpos(request::text, name) > 0)",
                (names,),
            ).fetchall()
        return {row["name"] for row in rows}

    def _insert(
        self,
        op_id: str,
        kind: str,
        subject: str,
        key: str,
        request: dict[str, Any],
        workflow_id: str,
        workflow_run_id: str,
        retention: timedelta | None,
    ) -> Operation:
        with self._require().connection() as conn, conn.transaction():
            if retention is not None:
                conn.execute("DELETE FROM operations WHERE finished_at < now() - %s", (retention,))
            row = conn.execute(
                "INSERT INTO operations (id, kind, subject, idempotency_key, status, request,"
                " workflow_id, workflow_run_id) VALUES (%s, %s, %s, %s, 'running', %s, %s, %s)"
                " ON CONFLICT (workflow_id, workflow_run_id) DO NOTHING"
                f" RETURNING {_COLUMNS}",
                (op_id, kind, subject, key, Jsonb(request), workflow_id, workflow_run_id),
            ).fetchone()
            if row is not None:
                op = Operation.model_validate(row)
                self._announce(conn, op)
                return op
            existing = conn.execute(
                f"SELECT {_COLUMNS} FROM operations"
                " WHERE workflow_id = %s AND workflow_run_id = %s",
                (workflow_id, workflow_run_id),
            ).fetchone()
        assert existing is not None  # the conflict was on this pair
        return Operation.model_validate(existing)

    def _finish(
        self, op_id: str, result: dict[str, Any] | None, error: PrintRunError | None
    ) -> Operation:
        status: OperationStatus = "failed" if error is not None else "succeeded"
        with self._require().connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE operations SET status = %s, result = %s, error = %s, finished_at = now()"
                f" WHERE id = %s AND status = 'running' RETURNING {_COLUMNS}",
                (
                    status,
                    Jsonb(result) if error is None else None,
                    Jsonb(error.model_dump(mode="json")) if error is not None else None,
                    op_id,
                ),
            ).fetchone()
            if row is not None:
                op = Operation.model_validate(row)
                self._announce(conn, op)
                return op
            current = conn.execute(
                f"SELECT {_COLUMNS} FROM operations WHERE id = %s", (op_id,)
            ).fetchone()
        if current is None:
            raise LookupError(f"there is no operation {op_id}")
        return Operation.model_validate(current)

    def _running_executions(self, older_than: timedelta) -> list[tuple[str, str, str]]:
        with self._require().connection() as conn:
            rows = conn.execute(
                "SELECT id, workflow_id, workflow_run_id FROM operations"
                " WHERE status = 'running' AND created_at <= now() - %s ORDER BY created_at",
                (older_than,),
            ).fetchall()
        return [(row["id"], row["workflow_id"], row["workflow_run_id"]) for row in rows]
