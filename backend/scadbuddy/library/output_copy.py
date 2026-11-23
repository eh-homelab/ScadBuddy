"""The copy of the saved outputs' files into ``outputs`` rows (#7255, outputs-in-Postgres
spec 2026-11-23 §6.1, §6.3, §6.5), run at every API start.

It reconciles; it does not only fill. Each run walks ``data/outputs/<slug>/<id>/``: a
folder with no row gets one, a row older than its folder's newest record file is written
again, and a row whose folder the walk did not see goes. A folder with no readable
``meta.json`` gets no row and goes on the run's "not copied" list; a side file that
cannot be read is left out of the row and named on the "damaged" list. The first run that
finishes makes `OutputRows.copied` true, which ends the fallback to the files.

One replica copies at a time, under a session advisory lock. Synchronous, like
`OutputRows`: a caller on the loop uses a thread.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from scadbuddy.library.output_rows import OutputRow, OutputRows
from scadbuddy.library.outputs import (
    _ID_LIST,
    ARRANGED_NAME,
    BACKFILL_NAME,
    BOM_NAME,
    INPUTS_NAME,
    MANIFEST_NAME,
    META_NAME,
    MODEL_NAME,
    OUTPUT_ID_PATTERN,
    PARAMS_NAME,
    RECORD_NAME,
    BackfillState,
    OutputMeta,
    OutputStore,
    _holds_plate_cover,
)
from scadbuddy.render.job_models import BomEntry, ManifestObject, OutputRecord

logger = logging.getLogger(__name__)

#: The advisory lock's name: ``pg_try_advisory_lock(hashtextextended(COPY_LOCK, 0))``.
COPY_LOCK = "scadbuddy:outputs:copy"

#: The files a row holds: a newer one than the row's ``copied_at`` writes the row again.
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


def _dicts(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def _check_list(check: Callable[[Any], object]) -> Callable[[Any], list[Any]]:
    def checked(value: Any) -> list[Any]:
        if not isinstance(value, list):
            raise ValueError("not a list")
        for item in value:
            check(item)
        return value

    return checked


def _record(value: Any) -> dict[str, Any]:
    OutputRecord.model_validate(value)
    return _dicts(value)


def _backfill(value: Any) -> dict[str, Any]:
    BackfillState.model_validate(value)
    return _dicts(value)


def _arranged(value: Any) -> list[Any]:
    ids: list[Any] = _ID_LIST.validate_python(value)
    return ids


#: Each side file and the check its JSON must pass, as the store's readers read it.
_SIDE_FILES: dict[str, Callable[[Any], Any]] = {
    PARAMS_NAME: _dicts,
    INPUTS_NAME: _dicts,
    RECORD_NAME: _record,
    MANIFEST_NAME: _check_list(ManifestObject.model_validate),
    BOM_NAME: _check_list(BomEntry.model_validate),
    ARRANGED_NAME: _arranged,
    BACKFILL_NAME: _backfill,
}


def _newest_mtime(directory: Path) -> datetime | None:
    newest: float | None = None
    for name in RECORD_FILES:
        try:
            mtime = (directory / name).stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return None if newest is None else datetime.fromtimestamp(newest, UTC)


def read_folder(directory: Path, slug: str) -> tuple[OutputRow | None, list[dict[str, Any]]]:
    """The output's row from its files, and the side files left out of it. ``None`` for
    a folder whose ``meta.json`` is missing, unreadable or names another output."""
    output_id = directory.name
    try:
        meta = OutputMeta.model_validate_json((directory / META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return None, []
    if meta.id != output_id or meta.slug != slug:
        return None, []
    values: dict[str, Any] = {}
    damaged: list[dict[str, Any]] = []
    for name, check in _SIDE_FILES.items():
        try:
            text = (directory / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            values[name] = None
            continue
        try:
            values[name] = check(json.loads(text))
        except (OSError, ValueError, ValidationError) as error:
            values[name] = None
            damaged.append(
                {"output_id": output_id, "slug": slug, "file": name, "error": type(error).__name__}
            )
    row = OutputRow(
        output_id=output_id,
        slug=slug,
        meta=meta,
        params=values[PARAMS_NAME] or {},
        inputs=values[INPUTS_NAME],
        record=values[RECORD_NAME],
        manifest=values[MANIFEST_NAME],
        bom=values[BOM_NAME],
        arranged_from=values[ARRANGED_NAME],
        backfill=values[BACKFILL_NAME],
        has_plate_cover=_holds_plate_cover(directory / MODEL_NAME),
        copied_at=meta.created_at,
    )
    return row, damaged


def _folders(root: Path) -> Iterator[tuple[str, Path]]:
    """Every output folder under ``root``, as ``(slug, folder)``."""
    try:
        slugs = sorted(entry for entry in root.iterdir() if entry.is_dir())
    except FileNotFoundError:
        return
    for slug_dir in slugs:
        try:
            entries = sorted(slug_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if re.fullmatch(OUTPUT_ID_PATTERN, entry.name) and entry.is_dir():
                yield slug_dir.name, entry


def copy_outputs(
    store: OutputStore,
    rows: OutputRows,
    pool: ConnectionPool[Connection[DictRow]],
    stop: threading.Event | None = None,
) -> CopyResult | None:
    """One run of the copy; ``None`` when another replica runs one. ``stop`` ends it
    between folders, with its run left unfinished."""
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
            lock.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (COPY_LOCK,))


def _copy(store: OutputStore, rows: OutputRows, stop: threading.Event | None) -> CopyResult:
    run_id = rows.start_run()
    started = rows.run_started_at(run_id)
    copied_at = rows.copied_times()
    result = CopyResult()
    seen: set[str] = set()
    batch: list[OutputRow] = []
    for slug, directory in _folders(store.paths.outputs):
        if stop is not None and stop.is_set():
            rows.upsert_many(batch)
            return result
        output_id = directory.name
        seen.add(output_id)
        known = copied_at.get(output_id)
        if known is not None:
            newest = _newest_mtime(directory)
            if newest is None or newest <= known:
                continue
        row, damaged = read_folder(directory, slug)
        if row is None:
            result.not_copied.append(f"{slug}/{output_id}")
            continue
        result.damaged.extend(damaged)
        if known is None:
            result.copied += 1
        else:
            result.refreshed += 1
        batch.append(row)
        if len(batch) >= BATCH:
            rows.upsert_many(batch)
            batch = []
    rows.upsert_many(batch)
    stale = rows.ids_copied_before(started) - seen
    if stale and not seen:
        # An outputs folder that reads empty is a volume not there, not every output
        # deleted: keep the rows.
        logger.warning(
            "the outputs folder lists no output; kept every row", extra={"rows": len(stale)}
        )
    else:
        for output_id in sorted(stale):
            rows.delete(output_id)
            result.dropped += 1
    rows.finish_run(
        run_id,
        copied=result.copied,
        refreshed=result.refreshed,
        dropped=result.dropped,
        not_copied=result.not_copied,
        damaged=result.damaged,
    )
    return result
