"""A flow's versions: one row each in ``workflow_definitions``, one commit each in
``data/flows`` (#2503; spec 2026-10-11-flow-grants-and-git-design.md §6.2).

A version's script is read from git at the commit its row names, never from the row. A
registration or restore holds the flows repository's write lock across the reconcile,
the commit and the row's insert (``FlowHistory.register``). If the insert fails, the
commit stays and the request fails; the next registration, or the next start, indexes
it at its own position (``flows/backfill.py``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from scadbuddy.core.gitrepo import GitError, RevisionNotFoundError
from scadbuddy.flows.backfill import backfill, created_by, reconcile_locked
from scadbuddy.flows.models import Definition, DefinitionSummary, FlowDiff, FlowVersion
from scadbuddy.flows.repository import FlowHistory
from scadbuddy.flows.store import FlowStore

logger = logging.getLogger(__name__)


class FlowHistoryUnavailableError(Exception):
    """No flows repository: no git binary, or ``data/flows`` could not be initialised."""


class UnknownFlowError(Exception):
    """No version of a flow by this name."""


class ScriptMissingError(Exception):
    """The commit a version names is not in the repository (a damaged volume)."""


class FlowDefinitions:
    def __init__(self, history: FlowHistory, store: FlowStore) -> None:
        self.history = history
        self.store = store

    # -- start ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the repository, backfill the rows written before flows were in
        git, and index any commit a failed insert left (spec §6.4)."""
        await asyncio.to_thread(self._start)

    def _start(self) -> None:
        self.history.ensure_repo()
        if not self.history.available:
            return
        try:
            done = backfill(self.history, self.store)
        except (GitError, OSError):
            # The app boots either way: the rows keep their scripts until a later
            # start backfills them, and registering waits for the repository.
            logger.exception("could not backfill flow versions into git")
            return
        if done:
            logger.info("backfilled flow versions into git", extra={"count": done})

    # -- writes -----------------------------------------------------------------

    async def register(
        self, name: str, script: str, author: dict[str, Any], *, approval_timeout_s: int | None
    ) -> Definition:
        """A new version of ``name``: always one commit, an unchanged script too."""
        return await asyncio.to_thread(self._write, name, author, approval_timeout_s, script, None)

    async def restore(self, name: str, commit: str, author: dict[str, Any]) -> Definition:
        """A new version of ``name`` with its script at ``commit`` (one of its versions):
        a new commit and a new row, with that version's approval timeout."""
        return await asyncio.to_thread(self._restore, name, commit, author)

    def _restore(self, name: str, commit: str, author: dict[str, Any]) -> Definition:
        target = self._version(name, commit)
        row = self.store.stored_definition(target.version_id)
        timeout = row.approval_timeout_s if row is not None else None
        return self._write(name, author, timeout, None, target.commit)

    def _write(
        self,
        name: str,
        author: dict[str, Any],
        approval_timeout_s: int | None,
        script: str | None,
        restore: str | None,
    ) -> Definition:
        self._require()
        version_id = str(uuid.uuid4())
        rows: list[Any] = []

        def index(commit: str, version: int) -> None:
            rows.append(
                self.store.insert_version(
                    version_id,
                    name,
                    version,
                    commit,
                    author,
                    approval_timeout_s=approval_timeout_s,
                )
            )

        commit, _ = self.history.register(
            name,
            version_id,
            index,
            script=script,
            restore=restore,
            reconcile=lambda: reconcile_locked(self.history, self.store),
        )
        return Definition.model_validate(
            {**rows[0].model_dump(), "script": self.history.script_at(name, commit)}
        )

    # -- reads ------------------------------------------------------------------

    async def get(self, definition_id: str) -> Definition | None:
        """A version with its script: from git at its commit (:class:`ScriptMissingError`
        when that is gone), or the row's own before its backfill."""
        return await asyncio.to_thread(self._get, definition_id)

    def _get(self, definition_id: str) -> Definition | None:
        row = self.store.stored_definition(definition_id)
        if row is None:
            return None
        script = row.script
        if row.commit is not None:
            self._require()
            try:
                script = self.history.script_at(row.name, row.commit)
            except RevisionNotFoundError:
                raise ScriptMissingError(row.commit) from None
        assert script is not None
        return Definition.model_validate({**row.model_dump(), "script": script})

    async def flows(self) -> list[DefinitionSummary]:
        """Each flow at its newest version, by name."""
        return await self.store.list_definitions()

    async def versions(self, name: str, *, limit: int) -> list[FlowVersion]:
        """The flow's history, newest first, from git. :class:`UnknownFlowError` for a
        name with no version."""
        return await asyncio.to_thread(self._versions, name, limit)

    def _versions(self, name: str, limit: int) -> list[FlowVersion]:
        self._require()
        found = self.history.versions(name)
        if not found:
            raise UnknownFlowError(name)
        return [
            FlowVersion(
                id=commit.version_id,
                commit=commit.commit,
                short=commit.short,
                version=commit.version,
                message=commit.message,
                created_by=created_by(commit.agent),
                created_at=commit.date,
                current=index == 0,
            )
            for index, commit in enumerate(found[:limit])
        ]

    async def source(self, name: str, commit: str) -> str:
        """The flow's script at ``commit``, one of its versions."""
        return await asyncio.to_thread(self._source, name, commit)

    def _source(self, name: str, commit: str) -> str:
        return self.history.script_at(name, self._version(name, commit).commit)

    async def diff(self, name: str, commit: str, base: str | None) -> FlowDiff:
        """The flow's script from ``base`` (its version before ``commit`` when None) to
        ``commit``; both must be versions of the flow."""
        return await asyncio.to_thread(self._diff, name, commit, base)

    def _diff(self, name: str, commit: str, base: str | None) -> FlowDiff:
        self._version(name, commit)
        start, end, patch = self.history.diff_flow(name, base, commit)
        return FlowDiff(name=name, base=start, head=end, patch=patch)

    # -- helpers ----------------------------------------------------------------

    def _require(self) -> None:
        if not self.history.available:
            raise FlowHistoryUnavailableError

    def _version(self, name: str, commit: str) -> Any:
        """The flow's version at ``commit``: :class:`UnknownFlowError` for a name with
        none, :class:`RevisionNotFoundError` for a commit that is not one."""
        self._require()
        if not self.history.versions(name):
            raise UnknownFlowError(name)
        return self.history.version_at(name, commit)
