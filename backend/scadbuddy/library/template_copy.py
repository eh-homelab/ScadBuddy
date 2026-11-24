"""The copy of the templates' files to their ``templates`` rows (#7317, model records spec
§5.3), run at every API start and in every housekeeping sweep
(`workflows/housekeeping.py` `TEMPLATE_COPY_SWEEP`).

It reconciles; it does not only fill. Outside the models write lock it walks the template
folders as `Catalogue.slugs` does, reads every template's last commit with one
`Catalogue.versions` walk, and stamps each template's files (`template_stamp`). A
template needs a refresh when it has no row, its stamp differs, or its version differs.
It refreshes those templates `BATCH` at a time, each batch under the write lock, so a save
waits for at most one small batch. Under the lock it reads the files again, so no stale
read writes over a newer row (spec §7.2). When HEAD moved since the walk, it corrects the
batch's versions with `ModelHistory.last_commits_since`, or, when this replica cannot see
the walk's HEAD, with each template's own last commit. Then, under the lock, it drops each
row whose template the walk did not see and that is not there now.

A listing or stat error other than a missing entry (an NFS ``ESTALE``, ``EIO``), a
missing ``models/``, a batch whose read or git call failed (the write lock past
``git_timeout`` too), or a walk that saw no template while rows exist makes the run
``incomplete``: it drops nothing and sets no ``finished_at``. A stop leaves the run with
no ``finished_at`` too. One replica copies at a time, under a session advisory lock.
Synchronous: a caller on the loop uses a thread.
"""

from __future__ import annotations

import contextlib
import logging
import stat
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.core.gitrepo import GitError
from scadbuddy.core.paths import (
    BUILTIN_DIR,
    BUILTIN_PREFIX,
    SOURCE_NAME,
    is_builtin,
    model_path,
    revalidate_dirs,
)
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.template_refresh import TemplateRefresh
from scadbuddy.library.template_rows import TemplateRows, template_stamp

logger = logging.getLogger(__name__)

#: The advisory lock's name: ``pg_try_advisory_lock(hashtextextended(COPY_LOCK, 0))``.
COPY_LOCK = "scadbuddy:templates:copy"

#: Templates refreshed per hold of the write lock (plan decision 8).
BATCH = 8


@dataclass
class TemplateCopyResult:
    added: int = 0
    refreshed: int = 0
    dropped: int = 0
    #: ``{template_id, error}`` of each row the run wrote or kept whose ``meta_error``
    #: is set.
    unreadable: list[dict[str, str]] = field(default_factory=list)
    #: The walk may have missed templates: the run has no ``finished_at``.
    incomplete: bool = False


class _Walk:
    """Every template id under ``models/``, mine then the built-ins, as `Catalogue.slugs`
    lists them. ``complete`` turns false when a listing or a stat fails with anything but
    a missing entry, or when ``models/`` is not there."""

    def __init__(self, models: Path, builtins: Path) -> None:
        self.models = models
        self.builtins = builtins
        self.complete = True

    def _failed(self, path: Path, error: OSError) -> None:
        self.complete = False
        logger.warning(
            "could not list a templates folder",
            extra={"path": str(path), "error": type(error).__name__},
        )

    def _list(self, folder: Path, *, required: bool) -> list[Path]:
        try:
            return sorted(folder.iterdir())
        except (FileNotFoundError, NotADirectoryError) as error:
            if required:
                self._failed(folder, error)
            return []
        except OSError as error:
            self._failed(folder, error)
            return []

    def _is_template(self, folder: Path) -> bool:
        source = folder / SOURCE_NAME
        try:
            return stat.S_ISREG(source.stat().st_mode)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError as error:
            self._failed(source, error)
            return False

    def ids(self) -> list[str]:
        revalidate_dirs(self.models, self.builtins)
        mine = [
            entry.name
            for entry in self._list(self.models, required=True)
            if entry.name != BUILTIN_DIR and self._is_template(entry)
        ]
        builtins = [
            f"{BUILTIN_PREFIX}{entry.name}"
            for entry in self._list(self.builtins, required=False)
            if self._is_template(entry)
        ]
        return mine + builtins


def copy_templates(
    catalogue: Catalogue,
    rows: TemplateRows,
    refresh: TemplateRefresh,
    pool: ConnectionPool[Connection[DictRow]],
    stop: threading.Event | None = None,
) -> TemplateCopyResult | None:
    """One run of the copy; ``None`` when another replica runs one. ``stop`` ends it
    between batches and leaves its run unfinished.

    The advisory lock holds one connection of ``pool`` for the whole run. That is fine:
    the connection is autocommit, so it is never idle in a transaction and holds no row
    lock. The models write lock is taken per batch only."""
    with pool.connection() as lock:
        got = lock.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS got", (COPY_LOCK,)
        ).fetchone()
        if got is None or not got["got"]:
            logger.info("another replica copies the templates to rows; this one does not")
            return None
        try:
            return _Copy(catalogue, rows, refresh, stop).run()
        finally:
            try:
                lock.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (COPY_LOCK,))
            except Exception:
                # The connection must not go back to the pool with the lock held: closing
                # it ends its session, which releases the lock, and the pool drops it.
                logger.warning("could not release the template copy's lock; closed its connection")
                with contextlib.suppress(Exception):
                    lock.close()


class _Copy:
    def __init__(
        self,
        catalogue: Catalogue,
        rows: TemplateRows,
        refresh: TemplateRefresh,
        stop: threading.Event | None,
    ) -> None:
        self.catalogue = catalogue
        self.rows = rows
        self.refresh = refresh
        self.stop = stop
        history = catalogue.history
        self.history: ModelHistory | None = (
            history if history is not None and history.available else None
        )
        self.result = TemplateCopyResult()

    def _stopped(self) -> bool:
        return self.stop is not None and self.stop.is_set()

    def _locked(self) -> AbstractContextManager[Any]:
        # Without git there is one process and no lock (spec §5.2).
        return nullcontext() if self.history is None else self.history.write_lock()

    def run(self) -> TemplateCopyResult:
        run_id = self.rows.start_run()
        paths = self.catalogue.paths
        walk = _Walk(paths.models, paths.builtins)
        seen = walk.ids()
        walk_head = None if self.history is None else self.history.head()
        # One walk of the history (`last_commits`) for every template's version.
        versions: dict[str, str | None] = dict(self.catalogue.versions())
        known = self.rows.stamps()
        media = self.catalogue.media_store.items_for(seen)
        builtins = [template_id for template_id in seen if is_builtin(template_id)]
        covers = self.catalogue.media_store.covers_for(builtins) if builtins else {}

        wanted: list[str] = []
        for template_id in seen:
            if self._stopped():
                return self.result
            try:
                stamp = template_stamp(
                    paths, template_id, media.get(template_id, []), covers.get(template_id)
                )
            except OSError as error:
                walk._failed(paths.model_dir(template_id), error)
                continue
            row = known.get(template_id)
            if row is None or row != (stamp, versions.get(template_id)):
                wanted.append(template_id)

        for batch in _batches(wanted):
            if self._stopped():
                return self.result
            if not self._refresh(batch, versions, walk_head, known):
                walk.complete = False

        if self._stopped():
            return self.result
        if not walk.complete:
            self.result.incomplete = True
            logger.warning("a listing of the templates failed; dropped no row this run")
        elif not seen and known:
            # A models folder that lists no template is a volume not there, not every
            # template deleted: keep the rows.
            self.result.incomplete = True
            logger.warning(
                "the models folder lists no template; kept every row", extra={"rows": len(known)}
            )
        elif not self._drop(set(seen)):
            self.result.incomplete = True

        errors = self.rows.meta_errors()
        self.result.unreadable = [
            {"template_id": template_id, "error": errors[template_id]}
            for template_id in seen
            if template_id in errors
        ]
        end = self.rows.end_incomplete_run if self.result.incomplete else self.rows.finish_run
        end(
            run_id,
            added=self.result.added,
            refreshed=self.result.refreshed,
            dropped=self.result.dropped,
            unreadable=self.result.unreadable,
        )
        return self.result

    def _refresh(
        self,
        batch: list[str],
        versions: dict[str, str | None],
        walk_head: str | None,
        known: dict[str, tuple[str, str | None]],
    ) -> bool:
        """Read and write ``batch``'s rows under the write lock. False when a read or a
        git call failed: the batch's rows are left as they were."""
        try:
            with self._locked():
                head = None if self.history is None else self.history.head()
                read_versions = {template_id: versions.get(template_id) for template_id in batch}
                if self.history is not None:
                    read_versions = self._current(batch, read_versions, walk_head, head)
                written, gone = self.refresh.read(batch, read_versions, head)
                self.rows.put(written, gone)
        except (OSError, GitError):
            # A read error, or the write lock past `git_timeout` (a long save on another
            # replica): this batch only. A Postgres error fails the run.
            logger.exception("could not read the templates' rows", extra={"templates": batch})
            return False
        for row in written:
            if row.template_id in known:
                self.result.refreshed += 1
            else:
                self.result.added += 1
        self.result.dropped += sum(1 for template_id in gone if template_id in known)
        return True

    def _current(
        self,
        batch: list[str],
        versions: dict[str, str | None],
        walk_head: str | None,
        head: str | None,
    ) -> dict[str, str | None]:
        """``versions`` as of ``head``, under the lock: a commit that landed after the walk
        names its templates' new versions, so the copy never writes an older one back
        (plan decision 5). A template the walk had no version for reads its own."""
        history = self.history
        assert history is not None
        current = dict(versions)
        if head != walk_head:
            try:
                if walk_head is None:
                    raise GitError("the walk saw no HEAD")
                current.update(history.last_commits_since(walk_head, batch))
            except GitError:
                # This replica cannot see the walk's HEAD (Task 5 amendment 1): read
                # each template's last commit in full.
                return {
                    template_id: history.last_commit(model_path(template_id))
                    for template_id in batch
                }
        for template_id in batch:
            if current.get(template_id) is None:
                current[template_id] = history.last_commit(model_path(template_id))
        return current

    def _drop(self, seen: set[str]) -> bool:
        """Under the write lock, drop each row whose template the walk did not see and
        that is not there now (another replica may have just made it). False when a
        check failed: nothing is dropped."""
        try:
            with self._locked():
                gone = [
                    template_id
                    for template_id in self.rows.ids()
                    if template_id not in seen and not self.catalogue.exists(template_id)
                ]
                self.rows.put([], gone)
        except (OSError, GitError):
            logger.exception("could not check the templates no walk saw; dropped no row")
            return False
        self.result.dropped += len(gone)
        return True


def _batches(ids: list[str]) -> Iterator[list[str]]:
    for start in range(0, len(ids), BATCH):
        yield ids[start : start + BATCH]
