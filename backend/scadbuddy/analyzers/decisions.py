"""Where decisions live, and how the one that applies is chosen (#284 "Scopes").

Postgres only: the ``analyzer_decisions`` table, a backend migration file in
``backend/scadbuddy/migrations/`` in the ``scadbuddy_migrations`` ledger (not an ``ai_*`` one:
script analyzers run with AI off, and so must their decisions). There is no file
fallback. Until the database is required everywhere (#401), a ScadBuddy without
``SCADBUDDY_DATABASE_URL`` has no store; the routes that persist answer 503 saying
so, and a run reports that no decisions could be read.

Resolution: of the decisions matching a diagnostic at the scopes a print falls in,
the narrowest scope wins, and at one scope a decision about this instance wins over
one about every instance of the rule. An ``enforced`` decision is the exception: the
broadest enforced decision wins over everything narrower, which is the escape hatch
#284 describes for safety rules.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections.abc import Collection, Sequence
from typing import Any, Protocol

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.analyzers.model import Decision, ScopeKind, ScopeRef
from scadbuddy.library.outputs import OUTPUT_ID_PATTERN
from scadbuddy.library.slugs import MODEL_ID_PATTERN
from scadbuddy.render.pg_store import migrate

logger = logging.getLogger(__name__)

_SLUG = MODEL_ID_PATTERN.removeprefix("^").removesuffix("$")
#: What a key must look like for each scope. Loose where the value is someone else's
#: (a printer model, a model version) and exact where it is ScadBuddy's own.
SCOPE_KEY_PATTERNS: dict[ScopeKind, re.Pattern[str]] = {
    "global": re.compile(r"^$"),
    "material": re.compile(r"^[a-z0-9][a-z0-9 +._-]*(/[a-z0-9][a-z0-9 +._-]*)?$"),
    "printer": re.compile(r"^(id:[0-9]+|model:[A-Za-z0-9][A-Za-z0-9 ._+-]*)$"),
    "template": re.compile(rf"^{_SLUG}$"),
    "template_version": re.compile(rf"^{_SLUG}@[0-9A-Za-z._:-]+$"),
    "configuration": re.compile(rf"^{_SLUG}#[0-9a-f]{{16}}$"),
    "print": re.compile(OUTPUT_ID_PATTERN),
}


def valid_scope(scope: ScopeRef) -> bool:
    return SCOPE_KEY_PATTERNS[scope.kind].fullmatch(scope.key) is not None


def new_decision_id() -> str:
    return uuid.uuid4().hex


class DecisionStore(Protocol):
    backend: str

    def list(
        self,
        *,
        scopes: Collection[ScopeRef] | None = None,
        diagnostic_id: str | None = None,
    ) -> list[Decision]:
        """Decisions at any of ``scopes`` (every scope when ``None``), oldest first."""
        ...

    def get(self, decision_id: str) -> Decision | None: ...

    def put(self, decision: Decision) -> Decision | None:
        """Store ``decision``, replacing any other for the same rule, instance and scope;
        returns the one it replaced, if any."""
        ...

    def remove(self, decision_id: str) -> Decision | None: ...

    def close(self) -> None: ...


#: The first key of ``pg_advisory_xact_lock(int, int)`` that serialises writes to one
#: decision target ("SBAD"); the second is a hash of the target.
DECISION_LOCK_CLASS = 0x5342_4144

#: How long a request waits for a database connection before it is told the store is
#: unavailable. Short on purpose: a route answers 503 rather than hanging.
CONNECT_TIMEOUT = 5.0


class PostgresDecisionStore:
    """``analyzer_decisions`` on the #241 database, created by
    ``migrations/20260928T0724Z_analyzer_decisions.sql``.

    Connects on first use, not at construction, so building the app state stays
    offline, and applies the backend's migrations itself (idempotent, under their
    advisory lock) so it does not depend on the render queue having opened first.
    A database that cannot be reached raises ``psycopg.OperationalError`` or
    ``psycopg_pool.PoolTimeout`` within about :data:`CONNECT_TIMEOUT`; callers map both
    to "unavailable".
    """

    backend = "postgres"

    def __init__(
        self, conninfo: str, *, pool_size: int = 4, connect_timeout: float = CONNECT_TIMEOUT
    ) -> None:
        self.connect_timeout = connect_timeout
        self._pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=pool_size,
            open=False,
            timeout=connect_timeout,
            connection_class=Connection[DictRow],
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "connect_timeout": max(1, int(connect_timeout)),
            },
            name="scadbuddy-analyzer-decisions",
        )
        self._pool_open = False
        self._migrated = False
        self._open_lock = threading.Lock()

    def _ready(self) -> ConnectionPool[Connection[DictRow]]:
        # Only the non-blocking open is under the lock. Waiting for a connection (up to
        # the timeout) happens outside it, so one slow connect does not queue every
        # other request behind the lock; the migration is idempotent and takes its own
        # advisory lock, so two requests running it at once is harmless.
        with self._open_lock:
            if not self._pool_open:
                self._pool.open(wait=False)
                self._pool_open = True
        if not self._migrated:
            with self._pool.connection() as conn:
                migrate(conn)
            self._migrated = True
        return self._pool

    @staticmethod
    def _decision(row: DictRow) -> Decision:
        return Decision.model_validate(row["body"])

    def list(
        self,
        *,
        scopes: Collection[ScopeRef] | None = None,
        diagnostic_id: str | None = None,
    ) -> list[Decision]:
        clauses: list[str] = []
        args: list[Any] = []
        if scopes is not None:
            if not scopes:
                return []
            clauses.append(
                "(scope_kind, scope_key) IN (" + ", ".join("(%s, %s)" for _ in scopes) + ")"
            )
            for scope in scopes:
                args.extend((scope.kind, scope.key))
        if diagnostic_id is not None:
            clauses.append("diagnostic_id = %s")
            args.append(diagnostic_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._ready().connection() as conn:
            rows = conn.execute(
                f"SELECT body FROM analyzer_decisions{where} ORDER BY created_at, id".encode(),
                args,
            ).fetchall()
        return [self._decision(row) for row in rows]

    def get(self, decision_id: str) -> Decision | None:
        with self._ready().connection() as conn:
            row = conn.execute(
                "SELECT body FROM analyzer_decisions WHERE id = %s", (decision_id,)
            ).fetchone()
        return self._decision(row) if row is not None else None

    def put(self, decision: Decision) -> Decision | None:
        """An upsert on the target's unique index, so concurrent puts never collide.

        The advisory lock on the target makes reading the row being replaced and
        replacing it one step, so the caller learns exactly which decision it replaced
        (and can announce it removed) even when two puts race.
        """
        target = (
            decision.scope.kind,
            decision.scope.key,
            decision.diagnostic_id,
            decision.instance or "",
        )
        with self._ready().connection() as conn, conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                (DECISION_LOCK_CLASS, "\x1f".join(target)),
            )
            old = conn.execute(
                "SELECT body FROM analyzer_decisions WHERE scope_kind = %s AND scope_key = %s"
                " AND diagnostic_id = %s AND instance = %s",
                target,
            ).fetchone()
            conn.execute(
                "INSERT INTO analyzer_decisions"
                " (id, diagnostic_id, instance, scope_kind, scope_key, kind, body, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (scope_kind, scope_key, diagnostic_id, instance) DO UPDATE SET"
                " id = EXCLUDED.id, kind = EXCLUDED.kind, body = EXCLUDED.body,"
                " created_at = EXCLUDED.created_at",
                (
                    decision.id,
                    decision.diagnostic_id,
                    decision.instance or "",
                    decision.scope.kind,
                    decision.scope.key,
                    decision.kind,
                    Jsonb(json.loads(decision.model_dump_json())),
                    decision.created_at,
                ),
            )
        return self._decision(old) if old is not None else None

    def remove(self, decision_id: str) -> Decision | None:
        with self._ready().connection() as conn:
            row = conn.execute(
                "DELETE FROM analyzer_decisions WHERE id = %s RETURNING body", (decision_id,)
            ).fetchone()
        return self._decision(row) if row is not None else None

    def close(self) -> None:
        with self._open_lock:
            if self._pool_open:
                self._pool.close()
                self._pool_open = False


def resolve(
    diagnostic_id: str,
    key: str,
    decisions: Sequence[Decision],
    scopes: Sequence[ScopeRef],
) -> Decision | None:
    """The decision that decides this diagnostic at these scopes, if any does.

    ``scopes`` is broadest first, as :meth:`AnalysisContext.scopes` lists them; the
    position is the rank, so a printer id outranks the printer's model.
    """
    ordered = sorted(enumerate(scopes), key=lambda pair: (pair[1].rank, pair[0]))
    ranks = {(scope.kind, scope.key): index for index, (_, scope) in enumerate(ordered)}
    matching = [
        decision
        for decision in decisions
        if decision.diagnostic_id == diagnostic_id
        and decision.instance in (None, key)
        and (decision.scope.kind, decision.scope.key) in ranks
    ]
    if not matching:
        return None

    def rank(decision: Decision) -> tuple[int, int]:
        # Scope first; at one scope, the instance-specific decision.
        return (
            ranks[(decision.scope.kind, decision.scope.key)],
            1 if decision.instance is not None else 0,
        )

    enforced = [decision for decision in matching if decision.enforced]
    if enforced:
        return min(enforced, key=lambda decision: (rank(decision)[0], -rank(decision)[1]))
    return max(matching, key=rank)
