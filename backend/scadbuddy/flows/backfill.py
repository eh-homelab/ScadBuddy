"""The backfill and the reconciler of flow versions (#2503; spec
2026-10-11-flow-grants-and-git-design.md §6.2, §6.4).

**Backfill**, once, at the backend's start: every ``workflow_definitions`` row with no
commit, in ``(name, version)`` order, is written to ``<name>/flow.py`` and committed,
one commit per row (``--allow-empty`` for a script equal to the version before), dated
its ``created_at`` and authored from its ``created_by``, carrying its id in
``ScadBuddy-Flow-Version``; then the row and its runs name that commit. A crash between
a commit and its row's update is repaired on the next start by the commit's trailer.

**Reconciler**, at start and before every registration: a commit whose
``ScadBuddy-Flow-Version`` no row names (an insert that failed after its commit) gets a
row with that id at its own position. It matches on trailers, never on content.

Both run under the flows repository's write lock, which registration holds too.
"""

from __future__ import annotations

import logging
from typing import Any

from scadbuddy.core.authorship import AgentAuthor, authored_as
from scadbuddy.flows.repository import FlowCommit, FlowHistory
from scadbuddy.flows.store import FlowStore

logger = logging.getLogger(__name__)


def created_by(agent: AgentAuthor | None) -> dict[str, Any]:
    """Who made a version, from its commit's author trailers."""
    if agent is None:
        return {"kind": "browser"}
    return {"kind": "agent", "principal": agent.principal, "session": agent.session}


def author_of(created: dict[str, Any]) -> AgentAuthor | None:
    """The commit author a row's ``created_by`` records."""
    if created.get("kind") != "agent":
        return None
    return AgentAuthor(principal=created.get("principal"), session=created.get("session"))


def backfill(history: FlowHistory, store: FlowStore) -> int:
    """Commit every row written before flows were in git, then reconcile. The number of
    rows backfilled."""
    with history.write_lock():
        rows = store.unbackfilled()
        if rows:
            made = {c.version_id: c.commit for c in history.commits()}
            for row in rows:
                commit = made.get(row.id)
                if commit is None:

                    def index(commit: str, version: int, row_id: str = row.id) -> None:
                        store.set_commit(row_id, commit)

                    with authored_as(author_of(row.created_by)):
                        history.register_locked(
                            row.name, row.id, index, script=row.script, date=row.created_at
                        )
                else:
                    # Committed before a crash, its row never updated.
                    store.set_commit(row.id, commit)
        reconcile_locked(history, store)
    return len(rows)


def reconcile_locked(history: FlowHistory, store: FlowStore) -> list[FlowCommit]:
    """Under the write lock: a row for every commit no row names. The commits indexed."""
    rows = store.version_commits()
    orphans = [c for c in reversed(history.commits()) if rows.get(c.version_id, None) is None]
    for orphan in orphans:
        if orphan.version_id in rows:
            # A row with no commit that a commit names: join them.
            store.set_commit(orphan.version_id, orphan.commit)
            continue
        logger.warning(
            "indexing a flow version whose row was never written",
            extra={"flow": orphan.name, "commit": orphan.commit},
        )
        store.insert_version(
            orphan.version_id,
            orphan.name,
            orphan.version,
            orphan.commit,
            created_by(orphan.agent),
            approval_timeout_s=None,
            created_at=orphan.date,
        )
    return orphans
