"""A flow's versions: one row each in ``workflow_definitions``, one commit each in
``data/flows`` (#2503; spec 2026-10-11-flow-grants-and-git-design.md §6.2).

A version's script is read from git at the commit its row names, never from the row. A
registration or restore holds the flows repository's write lock across the reconcile,
the commit and the row's insert (``FlowHistory.register``). If the insert fails, the
commit stays and the request fails; the next registration, or the next start, indexes
it at its own position (``flows/reconcile.py``).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from typing import Any

import psycopg

from scadbuddy.core.gitrepo import GitError, RevisionNotFoundError
from scadbuddy.flows.models import (
    Definition,
    DefinitionSummary,
    FlowDetails,
    FlowDiff,
    FlowState,
    FlowVersion,
)
from scadbuddy.flows.reconcile import created_by, reconcile, reconcile_locked
from scadbuddy.flows.repository import (
    DetailsNotSavedError,
    FlowExistsError,
    FlowHistory,
    FlowNotPresentError,
)
from scadbuddy.flows.states import FlowStates
from scadbuddy.flows.store import FlowStore

logger = logging.getLogger(__name__)


class FlowHistoryUnavailableError(Exception):
    """No flows repository: no git binary, or ``data/flows`` could not be initialised."""


class UnknownFlowError(Exception):
    """No version of a flow by this name."""


class FlowDeletedError(Exception):
    """The flow is deleted: restore a version first (#2646)."""


class UnmigratedFlowError(Exception):
    """The flow still has a version from before flows were in git that names no commit
    (#5139): a new version would take a number that row holds."""


class ScriptMissingError(Exception):
    """The commit a version names is not in the repository (a damaged volume), or it
    names none (a row from before flows were in git that no backfill reached)."""


class FlowDefinitions:
    def __init__(self, history: FlowHistory, store: FlowStore, states: FlowStates) -> None:
        self.history = history
        self.store = store
        self.states = states
        #: Each flow's display name as committed at one HEAD (#5357), so a run read
        #: asks git for HEAD and nothing more until a commit moves it.
        self._names_head: str | None = None
        self._names: dict[str, str | None] = {}
        self._names_lock = threading.Lock()

    # -- start ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the repository and index any commit a failed insert left (spec
        §6.4)."""
        await asyncio.to_thread(self._start)

    def _start(self) -> None:
        self.history.ensure_repo()
        if not self.history.available:
            return
        try:
            reconcile(self.history, self.store)
        except (GitError, OSError, psycopg.Error):
            # The app boots either way: the next registration reconciles first.
            logger.exception("could not reconcile flow versions with git")

    # -- writes -----------------------------------------------------------------

    async def register(
        self,
        name: str,
        script: str,
        author: dict[str, Any],
        *,
        approval_timeout_s: int | None,
        inputs_schema: dict[str, Any] | None = None,
        message: str | None = None,
        body: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
    ) -> Definition:
        """A new version of ``name``: always one version commit, an unchanged script too, with
        its inputs schema beside it (#2483) or none, named by ``message`` and ``body``
        when given. A ``display_name`` or ``description`` that changes the flow's details
        is a commit of its own after it (#3186); None leaves one as it is."""
        return await asyncio.to_thread(
            self._write,
            name,
            author,
            approval_timeout_s,
            script,
            None,
            inputs_schema,
            message=message,
            body=body,
            display_name=display_name,
            description=description,
        )

    async def register_new(
        self,
        name: str,
        script: str,
        author: dict[str, Any],
        *,
        inputs_schema: dict[str, Any] | None,
        message: str,
        display_name: str,
        description: str,
    ) -> Definition | None:
        """Version 1 of ``name`` (a built-in flow, #4252), or None when the name has
        any version already, a deleted flow's too: a shop's edit or delete stands, and two
        replicas starting at once register it once."""
        try:
            return await asyncio.to_thread(
                self._write,
                name,
                author,
                None,
                script,
                None,
                inputs_schema,
                message=message,
                display_name=display_name,
                description=description,
                only_new=True,
            )
        except FlowExistsError:
            return None

    async def upgrade_builtin(
        self,
        name: str,
        script: str,
        author: dict[str, Any],
        *,
        head: str,
        inputs_schema: dict[str, Any] | None,
        message: str,
    ) -> Definition | None:
        """A new version of a built-in flow (#4673) over its unedited ``head`` (the
        built-in's own commit), or None when ``head`` is no longer the newest version
        (another replica upgraded it first, or a person's version came after) or the flow
        is deleted, both checked under the history's write lock. The flow's details stay
        as they are."""
        try:
            return await asyncio.to_thread(
                self._write,
                name,
                author,
                None,
                script,
                None,
                inputs_schema,
                message=message,
                expect_head=head,
            )
        except FlowExistsError:
            return None

    async def restore(
        self,
        name: str,
        commit: str,
        author: dict[str, Any],
        *,
        message: str | None = None,
        body: str | None = None,
    ) -> Definition:
        """A new version of ``name`` with its script at ``commit`` (one of its versions):
        a new commit and a new row, with that version's approval timeout. The flow's
        details stay as they are (#3186), except for a deleted flow, which comes back with
        the display name and description it had at ``commit``, in the version's commit."""
        return await asyncio.to_thread(self._restore, name, commit, author, message, body)

    def _restore(
        self,
        name: str,
        commit: str,
        author: dict[str, Any],
        message: str | None,
        body: str | None,
    ) -> Definition:
        target = self._version(name, commit)
        row = self.store.stored_definition(target.version_id)
        timeout = row.approval_timeout_s if row is not None else None
        return self._write(name, author, timeout, None, target.commit, message=message, body=body)

    async def update_details(
        self,
        name: str,
        *,
        display_name: str | None,
        description: str | None,
        message: str | None = None,
        body: str | None = None,
    ) -> FlowDetails:
        """Change the flow's display name or description (#3186): one commit of
        ``<name>/flow.json`` under the repository's write lock, none when nothing
        changed. None leaves a field as it is, an empty string clears it.
        :class:`UnknownFlowError` for a name with no version, :class:`FlowDeletedError`
        for a deleted flow."""
        return await asyncio.to_thread(
            self._update_details, name, display_name, description, message, body
        )

    def _update_details(
        self,
        name: str,
        display_name: str | None,
        description: str | None,
        message: str | None,
        body: str | None,
    ) -> FlowDetails:
        self._require()
        if not self.history.versions(name):
            raise UnknownFlowError(name)
        if self.states.state(name).deleted_at is not None:
            raise FlowDeletedError(name)
        try:
            # Checks that the flow is in the tree under the write lock, so a delete
            # that lands first is refused as one.
            commit = self.history.set_details(
                name,
                display_name=display_name,
                description=description,
                message=message,
                body=body,
            )
        except FlowNotPresentError:
            raise FlowDeletedError(name) from None
        return self._details(name).model_copy(update={"commit": commit})

    async def details(self, name: str) -> FlowDetails:
        """The flow's display name and description now (#3186)."""
        return await asyncio.to_thread(self._details, name)

    def _details(self, name: str) -> FlowDetails:
        found = self.history.details(name)
        return FlowDetails(
            name=name, display_name=found.display_name, description=found.description
        )

    async def display_names(self, names: set[str]) -> dict[str, str | None]:
        """Each flow's display name now (#3186), for the runs that name it."""
        return await asyncio.to_thread(self._display_names, names)

    def _display_names(self, names: set[str]) -> dict[str, str | None]:
        """Read at HEAD, not from the tree, so a details write not yet committed (or
        one whose commit fails) is never cached. Without git, or before the first
        commit, each is read from the tree as before."""
        try:
            head = self.history.head()
        except GitError:
            head = None
        if head is None:
            return {name: self.history.details(name).display_name for name in names}
        with self._names_lock:
            if head != self._names_head:
                self._names_head, self._names = head, {}
            known = {name: self._names[name] for name in names if name in self._names}
        found = {
            name: self.history.details_at(name, head).display_name
            for name in names
            if name not in known
        }
        # details_at reads a deleted flow's details at the HEAD of the moment, not at
        # `head`, so a commit between the two reads can store a value under the old
        # `head`. That is harmless only because the next read that sees the new HEAD
        # empties the map; keep that reset if this cache changes.
        with self._names_lock:
            if head == self._names_head:
                self._names.update(found)
        return known | found

    def _detail_fields(self, name: str) -> dict[str, Any]:
        return self._details(name).model_dump(include={"display_name", "description"})

    def _before_commit(self, name: str) -> None:
        """Under the write lock, before a registration's or restore's commit: reconcile,
        then refuse a name whose commit-less row holds a number git would give again."""
        reconcile_locked(self.history, self.store)
        if self.store.has_commitless(name):
            raise UnmigratedFlowError(name)

    def _write(
        self,
        name: str,
        author: dict[str, Any],
        approval_timeout_s: int | None,
        script: str | None,
        restore: str | None,
        inputs_schema: dict[str, Any] | None = None,
        *,
        message: str | None = None,
        body: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        only_new: bool = False,
        expect_head: str | None = None,
    ) -> Definition:
        self._require()
        # A built-in's authorship goes in its commit too, so git, reconcile and the row
        # agree (review of #5682).
        builtin = author.get("name") if author.get("kind") == "builtin" else None
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
                    # A new version of a deleted flow brings it back (#2485).
                    revive=True,
                )
            )

        details_error: str | None = None
        try:
            commit, _ = self.history.register(
                name,
                version_id,
                index,
                script=script,
                inputs=inputs_schema,
                restore=restore,
                reconcile=lambda: self._before_commit(name),
                message=message,
                body=body,
                display_name=display_name,
                description=description,
                only_new=only_new,
                builtin=builtin if isinstance(builtin, str) else None,
                expect_head=expect_head,
            )
        except DetailsNotSavedError as error:
            # The version is committed and indexed: answer it, never fail it (#3186).
            logger.warning("a flow version was saved without its details", exc_info=True)
            commit, details_error = error.commit, str(error)
        return Definition.model_validate(
            {
                **rows[0].model_dump(),
                **self._detail_fields(name),
                **self._flags(self.states.state(name)),
                "script": self.history.script_at(name, commit),
                "inputs_schema": self.history.inputs_at(name, commit),
                "details_error": details_error,
            }
        )

    async def archive(self, name: str, by: dict[str, Any]) -> FlowState:
        """Archive every version of ``name`` (#2485): hidden by default, never started.
        :class:`UnknownFlowError` for a name with no version."""
        return await asyncio.to_thread(self._archive, name, by)

    def _archive(self, name: str, by: dict[str, Any]) -> FlowState:
        if not self.states.exists(name):
            raise UnknownFlowError(name)
        state = self.states.archive(name, by)
        if state.deleted_at is not None:
            raise FlowDeletedError(name)
        return state

    async def unarchive(self, name: str) -> FlowState:
        return await asyncio.to_thread(self._unarchive, name)

    def _unarchive(self, name: str) -> FlowState:
        if not self.states.exists(name):
            raise UnknownFlowError(name)
        return self.states.unarchive(name)

    async def delete(
        self,
        name: str,
        by: dict[str, Any],
        *,
        message: str | None = None,
        body: str | None = None,
    ) -> FlowState:
        """Delete ``name`` (#2485): one commit removing ``<name>/``, recorded under the
        repository's write lock, named by ``message`` and ``body`` when given (#3186). Its
        versions' rows and commits stay, so a run still reads the script it ran, and a
        restore of a version brings it back. A flow deleted already is answered as it
        is."""
        return await asyncio.to_thread(self._delete, name, by, message, body)

    def _delete(
        self, name: str, by: dict[str, Any], message: str | None, body: str | None
    ) -> FlowState:
        self._require()
        if not self.states.exists(name):
            raise UnknownFlowError(name)
        recorded: list[FlowState] = []
        self.history.delete(
            name,
            lambda commit: recorded.append(self.states.mark_deleted(name, by, commit)),
            message=message,
            body=body,
        )
        return recorded[0]

    @staticmethod
    def _flags(state: FlowState) -> dict[str, Any]:
        return state.model_dump(include={"archived_at", "archived_by", "deleted_at", "deleted_by"})

    # -- reads ------------------------------------------------------------------

    async def get(self, definition_id: str) -> Definition | None:
        """A version with its script: from git at its commit (:class:`ScriptMissingError`
        when that is gone or the row names none)."""
        return await asyncio.to_thread(self._get, definition_id)

    def _get(self, definition_id: str) -> Definition | None:
        row = self.store.stored_definition(definition_id)
        if row is None:
            return None
        if row.commit is None:
            raise ScriptMissingError(definition_id)
        self._require()
        try:
            script = self.history.script_at(row.name, row.commit)
            inputs = self.history.inputs_at(row.name, row.commit)
        except RevisionNotFoundError:
            raise ScriptMissingError(row.commit) from None
        return Definition.model_validate(
            {
                **row.model_dump(),
                **self._detail_fields(row.name),
                **self._flags(self.states.state(row.name)),
                "script": script,
                "inputs_schema": inputs,
            }
        )

    async def flows(self, *, hidden: bool = False) -> list[DefinitionSummary]:
        """Each flow at its newest version, by name; archived and deleted ones too when
        ``hidden`` (#2485), each with its display name and description (#3186)."""
        return await asyncio.to_thread(self._flows, hidden)

    def _flows(self, hidden: bool) -> list[DefinitionSummary]:
        return [
            summary.model_copy(update=self._detail_fields(summary.name))
            for summary in self.states.newest(hidden=hidden)
        ]

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
                body=commit.body,
                created_by=created_by(commit.agent, commit.builtin),
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

    async def inputs_schema(self, name: str, commit: str) -> dict[str, Any] | None:
        """The flow's inputs schema at ``commit``, one of its versions; None for none."""
        return await asyncio.to_thread(self._inputs_schema, name, commit)

    def _inputs_schema(self, name: str, commit: str) -> dict[str, Any] | None:
        return self.history.inputs_at(name, self._version(name, commit).commit)

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
