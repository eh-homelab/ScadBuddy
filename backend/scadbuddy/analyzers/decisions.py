"""Where decisions live, and how the one that applies is chosen (#284 "Scopes").

Two stores behind one protocol, as the render queue has (#241): Postgres when
``SCADBUDDY_DATABASE_URL`` is set, so every replica sees the same decisions, and a
JSON file on the data volume otherwise, so a single pod needs no database. The table
is a backend migration (``render/pg_store.py``'s ledger), not an ``ai_*`` one: script
analyzers run with AI off, and so must their decisions.

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
from pathlib import Path
from typing import Any, Protocol

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import TypeAdapter

from scadbuddy.analyzers.model import Decision, ScopeKind, ScopeRef
from scadbuddy.library.outputs import OUTPUT_ID_PATTERN
from scadbuddy.library.slugs import MODEL_ID_PATTERN
from scadbuddy.render.pg_store import migrate

logger = logging.getLogger(__name__)

DECISIONS_NAME = "decisions.json"
_DECISIONS = TypeAdapter(list[Decision])

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

    def put(self, decision: Decision) -> Decision:
        """Store ``decision``, replacing any other for the same rule, instance and scope."""
        ...

    def remove(self, decision_id: str) -> Decision | None: ...

    def close(self) -> None: ...


def _same_target(left: Decision, right: Decision) -> bool:
    return (
        left.diagnostic_id == right.diagnostic_id
        and left.instance == right.instance
        and left.scope == right.scope
    )


class FileDecisionStore:
    """``data/analyzers/decisions.json``: every decision in one file, rewritten whole.

    A handful of decisions per template is the expected size; a lock serialises this
    process's writers and a rename makes each write atomic for readers.
    """

    backend = "file"

    def __init__(self, directory: Path) -> None:
        self.path = directory / DECISIONS_NAME
        self._lock = threading.Lock()

    def _read(self) -> list[Decision]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        return _DECISIONS.validate_json(raw)

    def _write(self, decisions: Sequence[Decision]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}")
        partial.write_bytes(_DECISIONS.dump_json(list(decisions), indent=2) + b"\n")
        partial.replace(self.path)

    def list(
        self,
        *,
        scopes: Collection[ScopeRef] | None = None,
        diagnostic_id: str | None = None,
    ) -> list[Decision]:
        with self._lock:
            decisions = self._read()
        wanted = {(scope.kind, scope.key) for scope in scopes} if scopes is not None else None
        return [
            decision
            for decision in decisions
            if (wanted is None or (decision.scope.kind, decision.scope.key) in wanted)
            and (diagnostic_id is None or decision.diagnostic_id == diagnostic_id)
        ]

    def get(self, decision_id: str) -> Decision | None:
        with self._lock:
            return next((row for row in self._read() if row.id == decision_id), None)

    def put(self, decision: Decision) -> Decision:
        with self._lock:
            kept = [row for row in self._read() if not _same_target(row, decision)]
            kept.append(decision)
            self._write(kept)
        return decision

    def remove(self, decision_id: str) -> Decision | None:
        with self._lock:
            decisions = self._read()
            gone = next((row for row in decisions if row.id == decision_id), None)
            if gone is not None:
                self._write([row for row in decisions if row.id != decision_id])
        return gone

    def close(self) -> None:
        return None


class PostgresDecisionStore:
    """``analyzer_decisions`` on the #241 database (migration 2 in ``pg_store``).

    Connects on first use, not at construction, so building the app state stays
    offline, and applies the backend's migrations itself (idempotent, under their
    advisory lock) so it does not depend on the render queue having opened first.
    """

    backend = "postgres"

    def __init__(self, conninfo: str, *, pool_size: int = 4, connect_timeout: float = 30.0):
        self.connect_timeout = connect_timeout
        self._pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=pool_size,
            open=False,
            connection_class=Connection[DictRow],
            kwargs={"autocommit": True, "row_factory": dict_row},
            name="scadbuddy-analyzer-decisions",
        )
        self._opened = False
        self._open_lock = threading.Lock()

    def _ready(self) -> ConnectionPool[Connection[DictRow]]:
        with self._open_lock:
            if not self._opened:
                self._pool.open(wait=True, timeout=self.connect_timeout)
                with self._pool.connection() as conn:
                    migrate(conn)
                self._opened = True
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

    def put(self, decision: Decision) -> Decision:
        with self._ready().connection() as conn, conn.transaction():
            conn.execute(
                "DELETE FROM analyzer_decisions WHERE scope_kind = %s AND scope_key = %s"
                " AND diagnostic_id = %s AND instance = %s",
                (
                    decision.scope.kind,
                    decision.scope.key,
                    decision.diagnostic_id,
                    decision.instance or "",
                ),
            )
            conn.execute(
                "INSERT INTO analyzer_decisions"
                " (id, diagnostic_id, instance, scope_kind, scope_key, kind, body, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
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
        return decision

    def remove(self, decision_id: str) -> Decision | None:
        with self._ready().connection() as conn:
            row = conn.execute(
                "DELETE FROM analyzer_decisions WHERE id = %s RETURNING body", (decision_id,)
            ).fetchone()
        return self._decision(row) if row is not None else None

    def close(self) -> None:
        with self._open_lock:
            if self._opened:
                self._pool.close()
                self._opened = False


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
