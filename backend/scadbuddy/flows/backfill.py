"""The reconciler of flow versions (#2503; spec 2026-10-11-flow-grants-and-git-design.md
§6.2, §6.4).

At start and before every registration: a commit whose ``ScadBuddy-Flow-Version`` no
row names (an insert that failed after its commit) gets a row with that id at its own
position. It matches on trailers, never on content. It runs under the flows
repository's write lock, which registration holds too.

The start-time backfill of the rows written before flows were in git is gone with
``workflow_definitions.script`` (#1142): its migration refuses a row with no commit.
"""

from __future__ import annotations

import logging
from typing import Any

from scadbuddy.core.authorship import AgentAuthor
from scadbuddy.flows.repository import FlowCommit, FlowHistory
from scadbuddy.flows.store import FlowStore

logger = logging.getLogger(__name__)


def created_by(agent: AgentAuthor | None) -> dict[str, Any]:
    """Who made a version, from its commit's author trailers."""
    if agent is None:
        return {"kind": "browser"}
    return {"kind": "agent", "principal": agent.principal, "session": agent.session}


def reconcile(history: FlowHistory, store: FlowStore) -> list[FlowCommit]:
    """:func:`reconcile_locked` under the repository's write lock."""
    with history.write_lock():
        return reconcile_locked(history, store)


def reconcile_locked(history: FlowHistory, store: FlowStore) -> list[FlowCommit]:
    """Under the write lock: a row for every commit no row names. The commits indexed."""
    rows = store.version_commits()
    orphans = [c for c in reversed(history.commits()) if c.version_id not in rows]
    for orphan in orphans:
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
            # A register or restore after a delete that committed but never inserted: its
            # flow is back in the tree, so it is no longer deleted (#2646).
            revive=history.present(orphan.name),
        )
    return orphans
