"""The copy of the saved outputs' files into ``outputs`` rows (#7255, outputs-in-Postgres
spec 2026-11-23 §6.1, §6.3, §6.5), run at every API start and in every housekeeping sweep
(`workflows/housekeeping.py` `OUTPUT_COPY_SWEEP`).

It reconciles; it does not only fill. Each run walks ``data/outputs/<slug>/<id>/``: a
folder with no row gets one, a row whose stamp of the record files differs from the
folder's is written again, and a row whose folder the walk did not see goes. A folder
with no readable ``meta.json`` gets no row and goes on the run's "not copied" list; a
side file that cannot be read is left out of the row and named on the "damaged" list.
The first run that finishes makes `OutputRows.copied` true, which ends the fallback to
the files. A run whose walk may have missed folders (a listing failed with ESTALE or
EIO, the outputs folder read empty while rows exist, or a first run found no outputs
folder) does not finish: it is recorded as ``incomplete``, so the fallback stays.

Live writers on other pods run during the copy. Each one sets ``copied_at`` to the
database's clock. The copy takes a mark from that clock before it reads a batch, and
writes a row only where no writer touched it since the mark (`OutputRows.write_copy`),
so it never undoes a rename and never fails a save. A refresh never brings back a
deleted row: it only updates. A new row can: a live delete removes the row and then the
folder, and the copy's insert can commit between the two. So the copy checks each
inserted row's ``meta.json`` after its batch, and once more at the run's end, and
undoes the row when the file is gone. A delete whose folder removal is still going at
the run's end leaves the row until the next run, which drops it with its folder gone.

One replica copies at a time, under a session advisory lock on a connection of its
own. Synchronous, like `OutputRows`: a caller on the loop uses a thread.
"""

from __future__ import annotations

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

import psycopg
from psycopg.conninfo import make_conninfo

from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE
from scadbuddy.library.output_rows import COPY_LOCK as COPY_LOCK
from scadbuddy.library.output_rows import CUT_OVER_NOTHING_TO_COPY, OutputRow, OutputRows
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

#: The lock connection's ``application_name``, as a pool's name is its connections'.
LOCK_APPLICATION = "scadbuddy-output-copy"
#: Seconds to connect for the lock: a database that cannot be reached fails the run.
LOCK_CONNECT_TIMEOUT = 10

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
    #: The walk may have missed folders: the run is recorded with no ``finished_at``.
    incomplete: bool = False


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
    the walk may then have missed folders that are there. With ``root_required`` (the
    first run, while the fallback stands) a missing ``root`` fails it too: a volume not
    mounted is no empty install."""

    def __init__(self, root: Path, *, root_required: bool = False) -> None:
        self.root = root
        self.root_required = root_required
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
        except FileNotFoundError as error:
            if self.root_required and path == self.root:
                self._failed(path, error)
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
    conninfo: str,
    stop: threading.Event | None = None,
) -> CopyResult | None:
    """One run of the copy; ``None`` when another replica runs one. ``stop`` ends it
    between folders: it writes the batch read so far and leaves its run unfinished.

    The lock is a session lock on a connection of its own (``conninfo``), held for the
    whole run (up to `workflows.housekeeping.OUTPUT_COPY_TIMEOUT`, 15 minutes). It is
    not taken from the API's pool, so the pool's other users keep every connection
    during a run. The run releases the lock (`pg_advisory_unlock`) before it returns,
    also when it fails, so `OutputRows.try_cut_over` called next finds it free (#7769).
    Closing the connection releases it too when that unlock could not run.

    After the cut-over mark (#7257, plan D4) it does nothing and returns ``None``: the
    files are no longer the record, so a run would drop the row of a folder no write
    made. It checks the mark before the lock and again once it holds the lock, as the
    mark may be written between the two."""
    if rows.cut_over():
        logger.info(CUT_OVER_NOTHING_TO_COPY)
        return None
    own = make_conninfo(
        conninfo,
        application_name=LOCK_APPLICATION,
        connect_timeout=LOCK_CONNECT_TIMEOUT,
        **TCP_KEEPALIVE,
    )
    with psycopg.connect(own, autocommit=True) as lock:
        got = lock.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (COPY_LOCK,)
        ).fetchone()
        if got is None or not got[0]:
            logger.info("another replica copies the saved outputs; this one does not")
            return None
        try:
            # Again under the lock: the mark may have been written since the first check.
            if rows.cut_over():
                logger.info(CUT_OVER_NOTHING_TO_COPY)
                return None
            return _copy(store, rows, stop)
        finally:
            # Released here, not by the close: the server frees a closed session's locks a
            # moment later, and the cut-over's check of both locks may come first (#7769).
            # A dropped connection's unlock fails; the server freed the lock with the
            # session, and the run's own outcome must come through.
            try:
                freed = lock.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (COPY_LOCK,)
                ).fetchone()
            except psycopg.Error as e:
                logger.warning(
                    "the output copy's lock could not be released; its closed session frees it",
                    extra={"error": type(e).__name__},
                )
            else:
                # False, not an error, when this session did not hold it (#7823).
                if freed is None or freed[0] is not True:
                    logger.warning("the output copy's unlock found its lock not held")


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
        #: Each row this run inserted and kept, with its folder, by its batch's mark: what
        #: `recheck` looks at again at the run's end.
        self.inserted: dict[datetime, dict[str, Path]] = {}

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
        kept = {i: self.folders[i] for i in added - undone}
        if kept:
            self.inserted[mark] = kept
        self.mark = None
        self.new, self.changed, self.folders = [], [], {}

    def recheck(self) -> None:
        """At the run's end: undo each row this run inserted whose ``meta.json`` went
        since its batch's check (a live delete's folder removal that ended late)."""
        for mark, folders in self.inserted.items():
            gone = [i for i in sorted(folders) if not _has_meta(folders[i])]
            self.result.copied -= len(self.rows.undo_copy(gone, mark))
        self.inserted = {}


def restore_slug(store: OutputStore, rows: OutputRows, slug: str) -> int:
    """Write the rows of ``slug``'s output folders again from their files, as the copy
    writes them (one transaction, guarded by a mark). A model delete that dropped them
    and then could not move the model calls it, so its outputs keep their rows. How
    many rows it wrote."""
    mark = rows.clock()
    read: list[OutputRow] = []
    walk = _Walk(store.paths.outputs)
    for directory in walk._list(store.paths.outputs / slug):
        if not (re.fullmatch(OUTPUT_ID_PATTERN, directory.name) and walk._is_dir(directory)):
            continue
        stamp = files_stamp(directory)
        row, _ = store.row_from_files(directory, slug)
        if row is not None:
            read.append(dataclasses.replace(row, files_stamp=stamp))
    known = rows.existing(row.output_id for row in read)
    added, refreshed = rows.write_copy(
        [row for row in read if row.output_id not in known],
        [row for row in read if row.output_id in known],
        mark,
    )
    return len(added) + len(refreshed)


def _copy(store: OutputStore, rows: OutputRows, stop: threading.Event | None) -> CopyResult:
    run_id = rows.start_run()
    started = rows.run_started_at(run_id)
    stamps = rows.stamps()
    result = CopyResult()
    seen: set[str] = set()
    batch = _Batch(rows, result)
    walk = _Walk(store.paths.outputs, root_required=not rows.copied())
    for slug, directory in walk:
        if stop is not None and stop.is_set():
            batch.write()
            batch.recheck()
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
    batch.recheck()
    stale = rows.ids_copied_before(started) - seen
    if not walk.complete:
        # A listing that failed part-way may have missed folders that are there.
        result.incomplete = True
        logger.warning(
            "a listing of the outputs failed; dropped no row this run",
            extra={"rows": len(stale)},
        )
    elif stale and not seen:
        # An outputs folder that reads empty is a volume not there, not every output
        # deleted: keep the rows.
        result.incomplete = True
        logger.warning(
            "the outputs folder lists no output; kept every row", extra={"rows": len(stale)}
        )
    else:
        result.dropped = rows.drop_unseen(stale, started)
    # A walk that may have missed folders must not end the fallback to the files: the
    # outputs it missed have no row yet.
    end = rows.end_incomplete_run if result.incomplete else rows.finish_run
    end(
        run_id,
        copied=result.copied,
        refreshed=result.refreshed,
        dropped=result.dropped,
        not_copied=result.not_copied,
        damaged=result.damaged,
    )
    return result
