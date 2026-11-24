"""The copy of the saved outputs' files into ``outputs`` rows (#7255, outputs-in-Postgres
spec 2026-11-23 §6.1, §6.3, §6.5), run at every API start.

It reconciles; it does not only fill. Each run walks ``data/outputs/<slug>/<id>/``: a
folder with no row gets one, a row whose stamp of the record files differs from the
folder's is written again, and a row whose folder the walk did not see goes. A folder
with no readable ``meta.json`` gets no row and goes on the run's "not copied" list; a
side file that cannot be read is left out of the row and named on the "damaged" list.
The first run that finishes makes `OutputRows.copied` true, which ends the fallback to
the files.

Live writers on other pods run during the copy. Each one sets ``copied_at`` to the
database's clock. The copy takes a mark from that clock before it reads a batch, and
writes a row only where no writer touched it since the mark (`OutputRows.write_copy`),
so it never undoes a rename, never brings back a deleted row and never fails a save.

One replica copies at a time, under a session advisory lock. Synchronous, like
`OutputRows`: a caller on the loop uses a thread.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import re
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.library.output_rows import OutputRow, OutputRows
from scadbuddy.library.outputs import (
    ARRANGED_NAME,
    BACKFILL_NAME,
    BOM_NAME,
    INPUTS_NAME,
    MANIFEST_NAME,
    META_NAME,
    OUTPUT_ID_PATTERN,
    PARAMS_NAME,
    RECORD_NAME,
    OutputStore,
)

logger = logging.getLogger(__name__)

#: The advisory lock's name: ``pg_try_advisory_lock(hashtextextended(COPY_LOCK, 0))``.
COPY_LOCK = "scadbuddy:outputs:copy"

#: The files a row holds: their stamp (`files_stamp`) says whether a folder changed.
RECORD_FILES = (
    META_NAME,
    PARAMS_NAME,
    INPUTS_NAME,
    RECORD_NAME,
    MANIFEST_NAME,
    BOM_NAME,
    ARRANGED_NAME,
    BACKFILL_NAME,
)

#: Rows per transaction: prod commits take 1-2 s, so one commit per row is too slow.
BATCH = 100


@dataclass
class CopyResult:
    copied: int = 0
    refreshed: int = 0
    dropped: int = 0
    #: ``<slug>/<id>`` of each folder with no readable ``meta.json``.
    not_copied: list[str] = field(default_factory=list)
    #: ``{output_id, slug, file, error}`` for each side file left out of its row.
    damaged: list[dict[str, Any]] = field(default_factory=list)


def files_stamp(directory: Path) -> str:
    """A stamp of the folder's record files: the sha256 of each one's name, size and
    mtime in nanoseconds, a missing one as absent. The copy reads the folder again when
    it differs from the row's, so no NFS clock is compared with the database's."""
    entries: list[tuple[str, int | None, int | None]] = []
    for name in sorted(RECORD_FILES):
        try:
            found = (directory / name).stat()
        except OSError:
            entries.append((name, None, None))
            continue
        entries.append((name, found.st_size, found.st_mtime_ns))
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


class _Walk:
    """Every output folder under ``root``, as ``(slug, folder)``. ``complete`` turns
    false when a listing or a stat fails with anything but a missing entry (ESTALE, EIO):
    the walk may then have missed folders that are there."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.complete = True

    def _failed(self, path: Path, error: OSError) -> None:
        self.complete = False
        logger.warning(
            "could not list an outputs folder",
            extra={"path": str(path), "error": type(error).__name__},
        )

    def _is_dir(self, path: Path) -> bool:
        try:
            return stat.S_ISDIR(path.stat().st_mode)
        except FileNotFoundError:
            return False
        except OSError as error:
            self._failed(path, error)
            return False

    def _list(self, path: Path) -> list[Path]:
        try:
            return sorted(path.iterdir())
        except FileNotFoundError:
            return []
        except OSError as error:
            self._failed(path, error)
            return []

    def __iter__(self) -> Iterator[tuple[str, Path]]:
        for slug_dir in self._list(self.root):
            if not self._is_dir(slug_dir):
                continue
            for entry in self._list(slug_dir):
                if re.fullmatch(OUTPUT_ID_PATTERN, entry.name) and self._is_dir(entry):
                    yield slug_dir.name, entry


def copy_outputs(
    store: OutputStore,
    rows: OutputRows,
    pool: ConnectionPool[Connection[DictRow]],
    stop: threading.Event | None = None,
) -> CopyResult | None:
    """One run of the copy; ``None`` when another replica runs one. ``stop`` ends it
    between folders: it writes the batch read so far and leaves its run unfinished.

    The lock holds one connection of ``pool`` for the whole run. That is fine: the
    connection is autocommit, so it is never idle in a transaction and holds no row
    lock."""
    with pool.connection() as lock:
        got = lock.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS got", (COPY_LOCK,)
        ).fetchone()
        if got is None or not got["got"]:
            logger.info("another replica copies the saved outputs; this one does not")
            return None
        try:
            return _copy(store, rows, stop)
        finally:
            # A failed unlock must not hide the copy's own error. The lock ends with the
            # session in any case.
            with contextlib.suppress(Exception):
                lock.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (COPY_LOCK,))


def _has_meta(directory: Path) -> bool:
    try:
        (directory / META_NAME).stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True  # not known to be gone: keep the row
    return True


class _Batch:
    """The rows read since ``mark``, the database's clock taken before the first read."""

    def __init__(self, rows: OutputRows, result: CopyResult) -> None:
        self.rows = rows
        self.result = result
        self.mark: datetime | None = None
        self.new: list[OutputRow] = []
        self.changed: list[OutputRow] = []
        self.folders: dict[str, Path] = {}

    def start(self) -> None:
        """Take the mark, before the batch's first read."""
        if self.mark is None:
            self.mark = self.rows.clock()

    def add(self, row: OutputRow, directory: Path, *, known: bool) -> None:
        (self.changed if known else self.new).append(row)
        self.folders[row.output_id] = directory
        if len(self.folders) >= BATCH:
            self.write()

    def write(self) -> None:
        """Write the batch in one transaction. Then undo each row whose folder went
        between the read and the write (a delete on another pod)."""
        if self.mark is None:
            return
        mark = self.mark
        added, refreshed = self.rows.write_copy(self.new, self.changed, mark)
        gone = [i for i in sorted(added | refreshed) if not _has_meta(self.folders[i])]
        undone = self.rows.undo_copy(gone, mark)
        self.result.copied += len(added - undone)
        self.result.refreshed += len(refreshed - undone)
        self.mark = None
        self.new, self.changed, self.folders = [], [], {}


def _copy(store: OutputStore, rows: OutputRows, stop: threading.Event | None) -> CopyResult:
    run_id = rows.start_run()
    started = rows.run_started_at(run_id)
    stamps = rows.stamps()
    result = CopyResult()
    seen: set[str] = set()
    batch = _Batch(rows, result)
    walk = _Walk(store.paths.outputs)
    for slug, directory in walk:
        if stop is not None and stop.is_set():
            batch.write()
            return result
        output_id = directory.name
        seen.add(output_id)
        known = output_id in stamps
        # The stamp before the read: a file changed after the read changes the next
        # run's stamp, so that run reads the folder again.
        stamp = files_stamp(directory)
        if known and stamps[output_id] == stamp:
            continue
        batch.start()
        row, damaged = store.row_from_files(directory, slug)
        if row is None:
            result.not_copied.append(f"{slug}/{output_id}")
            continue
        result.damaged.extend(damaged)
        batch.add(dataclasses.replace(row, files_stamp=stamp), directory, known=known)
    batch.write()
    stale = rows.ids_copied_before(started) - seen
    if not walk.complete:
        # A listing that failed part-way may have missed folders that are there.
        logger.warning(
            "a listing of the outputs failed; dropped no row this run",
            extra={"rows": len(stale)},
        )
    elif stale and not seen:
        # An outputs folder that reads empty is a volume not there, not every output
        # deleted: keep the rows.
        logger.warning(
            "the outputs folder lists no output; kept every row", extra={"rows": len(stale)}
        )
    else:
        result.dropped = rows.drop_unseen(stale, started)
    rows.finish_run(
        run_id,
        copied=result.copied,
        refreshed=result.refreshed,
        dropped=result.dropped,
        not_copied=result.not_copied,
        damaged=result.damaged,
    )
    return result
