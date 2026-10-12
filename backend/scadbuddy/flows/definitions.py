"""A flow's versions: commits in ``data/flows`` (#2503), indexed in Postgres.

The script is read from git, at the commit a version id names; Postgres says which
commit that is, which commit is each flow's head, and the flow's approval timeout.
Every write takes the flows repository's write lock first and holds it across the
commit and the index's update, so two registrations never set a head out of order.

Versions registered before flows were in git are imported at start (:meth:`start`):
each becomes one commit, oldest first, with its author and date, and the runs of it
are mapped to that commit. The import is one Postgres transaction under its own
advisory lock; each commit names its version id in a trailer, so an import that fails
half way finds the commits it made when it runs again rather than making them twice.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from scadbuddy.core.authorship import AgentAuthor, authored_as
from scadbuddy.core.gitstore import GitError, Revision, RevisionNotFoundError
from scadbuddy.flows.models import (
    Definition,
    DefinitionSummary,
    FlowDiff,
    FlowVersion,
    LegacyVersion,
)
from scadbuddy.flows.repository import IMPORT_TRAILER, FlowHistory
from scadbuddy.flows.store import KEEP, FlowStore, VersionRow

logger = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class UnknownFlowError(Exception):
    """No version has this id."""


class FlowHistoryUnavailableError(Exception):
    """No flows repository: no git binary, or ``data/flows`` could not be initialised."""


def created_by(revision: Revision) -> dict[str, Any]:
    """Who made a commit, as a version's ``created_by``."""
    if revision.agent is None:
        return {"kind": "browser"}
    agent = revision.agent
    return {"kind": "agent", "principal": agent.principal, "session": agent.session}


def _author(created: dict[str, Any]) -> AgentAuthor | None:
    if created.get("kind") != "agent":
        return None
    return AgentAuthor(principal=created.get("principal"), session=created.get("session"))


class FlowDefinitions:
    def __init__(self, history: FlowHistory, store: FlowStore) -> None:
        self.history = history
        self.store = store

    # -- start ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the repository, import what Postgres still holds, and bring each
        flow's head up to its newest commit (one a failed write or an operator left)."""
        await asyncio.to_thread(self._start)

    def _start(self) -> None:
        self.history.ensure_repo()
        if not self.history.available:
            return
        try:
            imported = self.store.import_legacy(self._importer())
            if imported:
                logger.info("imported flow versions into git", extra={"count": imported})
            self._reconcile()
        except (GitError, OSError):
            logger.exception("could not import flow versions into git")

    def _importer(self) -> Any:
        found: dict[str, str] | None = None
        previous: dict[str, str] = {}

        def commit_for(legacy: LegacyVersion) -> tuple[str, int]:
            nonlocal found
            if found is None:
                found = self.history.imported()
            name = legacy.name
            commit = found.get(legacy.id)
            last = previous.get(name)
            if commit is None and last is not None and self._script(name, last) == legacy.script:
                # Registered again unchanged: the same commit, as a registration now is.
                commit = last
            if commit is None:
                with self.history.write_lock(), authored_as(_author(legacy.created_by)):
                    commit = self.history.save_locked(
                        name,
                        legacy.script,
                        f"Register {name}",
                        date=legacy.created_at,
                        trailers=[f"{IMPORT_TRAILER}: {legacy.id}"],
                    ) or self.history.last_commit(name)
            if commit is None:  # pragma: no cover - a save that changed nothing has a commit
                raise GitError(f"no commit for flow {name} version {legacy.version}")
            previous[name] = commit
            return commit, self.history.version_of(name, commit)

        return commit_for

    def _reconcile(self) -> None:
        indexed = self.store.head_commits()
        for name, commit in self.history.heads().items():
            if NAME_PATTERN.match(name) and indexed.get(name) != commit:
                revision = self.history.revision(commit)
                self.store.record_version(
                    name,
                    commit,
                    self.history.version_of(name, commit),
                    created_by(revision),
                    created_at=revision.date,
                    head=True,
                    approval_timeout_s=KEEP if name in indexed else None,
                )

    # -- writes -----------------------------------------------------------------

    async def register(
        self,
        name: str,
        script: str,
        author: dict[str, Any],
        *,
        approval_timeout_s: int | None,
    ) -> Definition:
        """A new version of ``name``, one commit authored by the request's author; the
        head as it is when the script is unchanged. Either way the approval timeout is
        set."""
        return await asyncio.to_thread(self._register, name, script, author, approval_timeout_s)

    def _register(
        self, name: str, script: str, author: dict[str, Any], approval_timeout_s: int | None
    ) -> Definition:
        self._require()
        with self.history.write_lock():
            commit = self.history.save_locked(name, script, f"Register {name}")
            commit = commit or self.history.last_commit(name)
            assert commit is not None  # unchanged means committed before
            row = self.store.record_version(
                name,
                commit,
                self.history.version_of(name, commit),
                author,
                head=True,
                approval_timeout_s=approval_timeout_s,
            )
        return Definition.model_validate({**row.model_dump(), "script": script})

    async def restore(self, definition_id: str, commit: str, author: dict[str, Any]) -> Definition:
        """The flow's script at ``commit`` as its new head: a new commit, never a
        rewrite; the head as it is when that is its script already. :class:`UnknownFlowError`
        for an unknown version, :class:`RevisionNotFoundError` for an unknown commit."""
        return await asyncio.to_thread(self._restore, definition_id, commit, author)

    def _restore(self, definition_id: str, commit: str, author: dict[str, Any]) -> Definition:
        self._require()
        name = self._name(definition_id)
        target = self.history.own_commit(name, commit)
        with self.history.write_lock():
            created = self.history.restore_locked(name, target)
            head = created or self.history.last_commit(name)
            assert head is not None
            row = self.store.record_version(
                name, head, self.history.version_of(name, head), author, head=True
            )
        return self._definition(row)

    # -- reads ------------------------------------------------------------------

    async def get(self, definition_id: str) -> Definition | None:
        return await asyncio.to_thread(self._get, definition_id)

    def _get(self, definition_id: str) -> Definition | None:
        row = self.store.version(definition_id)
        return None if row is None else self._definition(row)

    async def at(self, definition_id: str, commit: str) -> Definition:
        """The flow's version at ``commit``. :class:`UnknownFlowError` for an unknown version id,
        :class:`RevisionNotFoundError` when the flow did not exist at ``commit``."""
        return await asyncio.to_thread(self._at, definition_id, commit)

    def _at(self, definition_id: str, commit: str) -> Definition:
        self._require()
        name = self._name(definition_id)
        return self._definition(self._row(name, self.history.own_commit(name, commit)))

    async def flows(self) -> list[DefinitionSummary]:
        """Each flow at its head version, by name."""
        rows = await asyncio.to_thread(self.store.heads)
        return [DefinitionSummary.model_validate(row.model_dump()) for row in rows]

    async def versions(self, definition_id: str, *, limit: int) -> list[FlowVersion]:
        """The flow's commits, newest first. :class:`UnknownFlowError` for an unknown version."""
        return await asyncio.to_thread(self._versions, definition_id, limit)

    def _versions(self, definition_id: str, limit: int) -> list[FlowVersion]:
        self._require()
        name = self._name(definition_id)
        revisions = self.history.history(name, limit=limit)
        if not revisions:
            return []
        newest = self.history.version_of(name, revisions[0].commit)
        return [
            FlowVersion(
                id=self._row(name, revision.commit, revision).id,
                commit=revision.commit,
                short=revision.short,
                version=newest - index,
                message=revision.message,
                created_by=created_by(revision),
                created_at=revision.date,
                current=index == 0,
            )
            for index, revision in enumerate(revisions)
        ]

    async def diff(self, definition_id: str, commit: str, base: str | None) -> FlowDiff:
        """The flow's script from ``base`` (the version before ``commit`` when None) to
        ``commit``. :class:`UnknownFlowError`/:class:`RevisionNotFoundError` as :meth:`at`."""
        return await asyncio.to_thread(self._diff, definition_id, commit, base)

    def _diff(self, definition_id: str, commit: str, base: str | None) -> FlowDiff:
        self._require()
        name = self._name(definition_id)
        head = self.history.own_commit(name, commit)
        if base is None:
            parent = self.history.parent(head)
            base = self.history.last_commit(name, at=parent) if parent else None
        revisions = self.history.revision_range(base, head)
        return FlowDiff(
            name=name,
            base=revisions.base,
            head=revisions.head,
            patch=self.history.diff_flow(name, revisions),
        )

    # -- helpers ----------------------------------------------------------------

    def _require(self) -> None:
        if not self.history.available:
            raise FlowHistoryUnavailableError

    def _name(self, definition_id: str) -> str:
        row = self.store.version(definition_id)
        if row is None:
            raise UnknownFlowError(definition_id)
        return row.name

    def _row(self, name: str, commit: str, revision: Revision | None = None) -> VersionRow:
        """The version id of ``commit``, made from the commit when it has none (a write
        whose index update failed)."""
        row = self.store.version_at(name, commit)
        if row is not None:
            return row
        revision = revision or self.history.revision(commit)
        return self.store.record_version(
            name,
            commit,
            self.history.version_of(name, commit),
            created_by(revision),
            created_at=revision.date,
            head=False,
        )

    def _script(self, name: str, commit: str) -> str | None:
        try:
            return self.history.script_at(name, commit)
        except RevisionNotFoundError:
            return None

    def _definition(self, row: VersionRow) -> Definition:
        """A version with its script: from git at its commit, or the row's own for one
        not yet imported."""
        script = row.script
        if row.commit is not None:
            self._require()
            script = self.history.script_at(row.name, row.commit)
        assert script is not None
        return Definition.model_validate({**row.model_dump(), "script": script})
