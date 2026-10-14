"""Activity kinds (spec 2026-10-09 §4): each feature's ``ACTIVITY_KINDS`` from its
``scadbuddy/<feature>/activity.py``, one per operation kind, and the agent's
``ai_activity_kinds`` rows. Nothing is listed by hand."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from psycopg import Connection, errors

from scadbuddy.activity.models import ActivityKind, Facet
from scadbuddy.core.components import feature_exports

ACTIVITY_MODULE = "activity"
ACTIVITY_ATTR = "ACTIVITY_KINDS"
AGENT_PREFIX = "agent."


class AgentKeyCollisionError(ValueError):
    pass


class KindRegistry:
    def __init__(self, kinds: Iterable[ActivityKind]) -> None:
        self._kinds: dict[str, ActivityKind] = {}
        for kind in kinds:
            if kind.key in self._kinds:
                raise ValueError(f"two activity kinds are registered as {kind.key!r}")
            self._kinds[kind.key] = kind

    def get(self, key: str) -> ActivityKind | None:
        return self._kinds.get(key)

    def all(self) -> list[ActivityKind]:
        return list(self._kinds.values())

    def facet(self, key: str, name: str) -> Facet | None:
        kind = self._kinds.get(key)
        return None if kind is None else next((f for f in kind.facets if f.name == name), None)


def operation_kind_key(name: str) -> str:
    return f"operation.{name}"


def discover_kinds(
    operation_kinds: Iterable[str], agent_rows: list[dict[str, Any]]
) -> KindRegistry:
    code: list[ActivityKind] = [
        k for group in feature_exports(ACTIVITY_MODULE, ACTIVITY_ATTR) for k in group
    ]
    for kind in code:
        if kind.key.startswith(AGENT_PREFIX):
            raise AgentKeyCollisionError(
                f"{kind.key!r}: the {AGENT_PREFIX!r} prefix is the agent's"
            )
    declared = {k.key for k in code}
    ops = [
        ActivityKind(key=operation_kind_key(name), label=name.replace("_", " ").capitalize())
        for name in operation_kinds
        if operation_kind_key(name) not in declared
    ]
    agent = [
        ActivityKind(
            key=row["key"],
            label=row["label"],
            facets=tuple(
                Facet(name=f["name"], type=f["type"], label=f["label"]) for f in row["facets"]
            ),
        )
        for row in agent_rows
        if row["key"].startswith(AGENT_PREFIX)
    ]
    return KindRegistry([*code, *ops, *agent])


def load_agent_kinds(conn: Connection[Any]) -> list[dict[str, Any]]:
    """The agent's kinds, retired ones too (they still label old rows); none when the
    agent's tables are absent."""
    try:
        with conn.transaction():
            return list(
                conn.execute(
                    "SELECT key, label, facets, retired_at FROM ai_activity_kinds ORDER BY key"
                ).fetchall()
            )
    except errors.UndefinedTable:
        return []
