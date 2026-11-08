"""The reconciler of flow versions (#2503; spec 2026-10-11-flow-grants-and-git-design.md
§6.4), at the backend's start and before every registration: a commit whose
``ScadBuddy-Flow-Version`` no row names (an insert that failed after its commit) gets a
row with that id at its own position, and a commit-less row a commit names is joined to
it. It matches on trailers, never on content. It runs under the flows repository's write
lock, which registration holds too.

The start-time backfill of the rows written before flows were in git is gone with their
``script`` column (#5139). A commit-less row left over keeps its number: an orphan
numbered as it is is skipped, and its name takes no new version
(``definitions.UnmigratedFlowError``).
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from scadbuddy.core.authorship import AgentAuthor
from scadbuddy.flows.repository import FlowCommit, FlowHistory
from scadbuddy.flows.store import FlowStore

logger = logging.getLogger(__name__)


def created_by(agent: AgentAuthor | None, builtin: str | None = None) -> dict[str, Any]:
    """Who made a version, from its commit's author trailers: the build for a built-in's
    (`BUILTIN_TRAILER`, #4252), the agent for its own, else the browser."""
    if builtin is not None:
        return {"kind": "builtin", "name": builtin}
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
    orphans = [c for c in reversed(history.commits()) if rows.get(c.version_id) is None]
    indexed: list[FlowCommit] = []
    for orphan in orphans:
        if orphan.version_id in rows:
            # A commit-less row the commit names (an older build's backfill that crashed
            # between its commit and its update): join them.
            store.set_commit(orphan.version_id, orphan.commit)
            indexed.append(orphan)
            continue
        logger.warning(
            "indexing a flow version whose row was never written",
            extra={"flow": orphan.name, "commit": orphan.commit},
        )
        try:
            store.insert_version(
                orphan.version_id,
                orphan.name,
                orphan.version,
                orphan.commit,
                created_by(orphan.agent, orphan.builtin),
                approval_timeout_s=None,
                created_at=orphan.date,
                # A register or restore after a delete that committed but never inserted:
                # its flow is back in the tree, so it is no longer deleted (#2646).
                revive=history.present(orphan.name),
            )
        except psycopg.errors.UniqueViolation:
            # Its number is a commit-less row's (#5139): left unindexed, never a failed
            # start or registration.
            logger.warning(
                "skipping a flow version whose number a version with no commit holds",
                extra={"flow": orphan.name, "commit": orphan.commit},
            )
            continue
        indexed.append(orphan)
    return indexed
