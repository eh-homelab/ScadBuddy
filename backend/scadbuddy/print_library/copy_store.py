"""`library_copies` and `library_copy_files`: a library copy's record (#5355). Written
by the API process (the ``library`` worker runs inside it) over the settings pool, as
`BlobIndex` records a store migration; nothing else holds these tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from scadbuddy.print_library.copy_models import (
    MAX_LISTED_FILES,
    LibraryCopyCounts,
    LibraryCopyFileResult,
    LibraryCopyFileStatus,
    LibraryCopyRequest,
    LibraryCopyRunStatus,
    LibraryCopySummary,
)
from scadbuddy.store.index import Pool

#: What a file row keeps of why its copy failed.
REASON_CHARS = 500
#: How long a copy waits for another copy's put of the same bytes into the same library
#: (`TargetLocks`): the batch's own start-to-close timeout.
TARGET_LOCK_TIMEOUT_MS = 30 * 60 * 1000
TARGET_LOCK_CONNECT_TIMEOUT = 5


class PlannedFile(BaseModel):
    """A source file the plan found, and where under the selected root it sits. One the
    plan could not read (a file id the source no longer has) carries its ``reason`` and
    is recorded ``failed`` at once."""

    source_id: int
    path: str
    name: str
    size: int | None = None
    reason: str | None = None


class LibraryCopyStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    def start(self, copy_id: str, request: LibraryCopyRequest) -> None:
        """The job's row, ``running`` from now, its earlier file rows gone: a rerun of
        the same request starts its record over."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO library_copies (id, direction, request, status, error, started_at,"
                " finished_at, run_id) VALUES (%s, %s, %s, 'running', NULL, now(), NULL, NULL)"
                " ON CONFLICT (id) DO UPDATE SET direction = EXCLUDED.direction,"
                " request = EXCLUDED.request, status = 'running', error = NULL,"
                " started_at = now(), finished_at = NULL, run_id = NULL",
                (copy_id, request.direction, Jsonb(request.model_dump(mode="json"))),
            )

    def started(self, copy_id: str, run_id: str) -> None:
        """The workflow run the job's start began: the status route reads the job as
        ended only when the closed execution is this run's."""
        with self._pool.connection() as conn:
            conn.execute("UPDATE library_copies SET run_id = %s WHERE id = %s", (run_id, copy_id))

    def plan(self, copy_id: str, files: list[PlannedFile]) -> int:
        """The walked files as ``pending`` rows, in place of any the job had."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM library_copy_files WHERE copy_id = %s", (copy_id,))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO library_copy_files (copy_id, source_id, path, name, size, status,"
                    " reason) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    [
                        (
                            copy_id,
                            f.source_id,
                            f.path,
                            f.name,
                            f.size,
                            "pending" if f.reason is None else "failed",
                            None if f.reason is None else f.reason[:REASON_CHARS],
                        )
                        for f in files
                    ],
                )
        return len(files)

    def pending(self, copy_id: str, *, limit: int) -> list[PlannedFile]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT source_id, path, name, size FROM library_copy_files"
                " WHERE copy_id = %s AND status = 'pending'"
                " ORDER BY path, name, source_id LIMIT %s",
                (copy_id, limit),
            ).fetchall()
        return [PlannedFile.model_validate(r) for r in rows]

    def begin(self, copy_id: str, source_id: int) -> tuple[int, str | None]:
        """One more try begun on a pending file: the tries begun so far, this one
        included, and the reason the last one did not end, if it said."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE library_copy_files SET attempts = attempts + 1, updated_at = now()"
                " WHERE copy_id = %s AND source_id = %s RETURNING attempts, reason",
                (copy_id, source_id),
            ).fetchone()
        assert row is not None
        return int(row["attempts"]), row["reason"]

    def note(self, copy_id: str, source_id: int, reason: str) -> None:
        """Why a try on a file that stays pending did not end: shown on the row while
        the batch is retried."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE library_copy_files SET reason = %s, updated_at = now()"
                " WHERE copy_id = %s AND source_id = %s",
                (reason[:REASON_CHARS], copy_id, source_id),
            )

    def held(self, direction: str, sha256: str) -> list[int]:
        """The target ids that copies in ``direction`` recorded for these bytes, newest
        first. The caller checks that the target still holds each one."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT f.target_id, max(f.updated_at) AS at FROM library_copy_files f"
                " JOIN library_copies c ON c.id = f.copy_id"
                " WHERE f.sha256 = %s AND c.direction = %s AND f.target_id IS NOT NULL"
                " AND f.status IN ('copied', 'skipped_same_hash')"
                " GROUP BY f.target_id ORDER BY at DESC",
                (sha256, direction),
            ).fetchall()
        return [int(r["target_id"]) for r in rows]

    def locks(self) -> TargetLocks:
        """A batch's `TargetLocks`. Close them when the batch ends."""
        return TargetLocks(self._pool.conninfo)

    def record(
        self,
        copy_id: str,
        source_id: int,
        status: LibraryCopyFileStatus,
        *,
        reason: str | None = None,
        target_id: int | None = None,
        target_folder_id: int | None = None,
        sha256: str | None = None,
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE library_copy_files SET status = %s, reason = %s, target_id = %s,"
                " target_folder_id = %s, sha256 = %s, updated_at = now()"
                " WHERE copy_id = %s AND source_id = %s",
                (
                    status,
                    None if reason is None else reason[:REASON_CHARS],
                    target_id,
                    target_folder_id,
                    sha256,
                    copy_id,
                    source_id,
                ),
            )

    def finish(self, copy_id: str, status: LibraryCopyRunStatus, error: str | None = None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE library_copies SET status = %s, error = %s, finished_at = now()"
                " WHERE id = %s",
                (status, None if error is None else error[:REASON_CHARS], copy_id),
            )

    def counts(self, copy_id: str) -> LibraryCopyCounts:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT status, count(*) AS n FROM library_copy_files WHERE copy_id = %s"
                " GROUP BY status",
                (copy_id,),
            ).fetchall()
        by_status = {r["status"]: int(r["n"]) for r in rows}
        return LibraryCopyCounts(
            total=sum(by_status.values()),
            pending=by_status.get("pending", 0),
            copied=by_status.get("copied", 0),
            skipped_same_hash=by_status.get("skipped_same_hash", 0),
            failed=by_status.get("failed", 0),
        )

    def summary(self, copy_id: str) -> LibraryCopySummary | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT id, direction, request, status, error, started_at, finished_at, run_id"
                " FROM library_copies WHERE id = %s",
                (copy_id,),
            ).fetchone()
        return None if row is None else self._summary(row, self.counts(copy_id))

    def recent(self, *, limit: int = 20) -> list[LibraryCopySummary]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT c.id, c.direction, c.request, c.status, c.error, c.started_at,"
                " c.finished_at, c.run_id FROM library_copies c"
                " ORDER BY c.started_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [self._summary(row, self.counts(row["id"])) for row in rows]

    def files(self, copy_id: str, *, limit: int = MAX_LISTED_FILES) -> list[LibraryCopyFileResult]:
        """Failed and pending rows first, then the rest, each in path order."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT source_id, path, name, size, status, reason, target_id, target_folder_id,"
                " attempts FROM library_copy_files WHERE copy_id = %s"
                " ORDER BY CASE status WHEN 'failed' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,"
                " path, name, source_id LIMIT %s",
                (copy_id, limit),
            ).fetchall()
        return [LibraryCopyFileResult.model_validate(r) for r in rows]

    @staticmethod
    def _summary(row: dict[str, Any], counts: LibraryCopyCounts) -> LibraryCopySummary:
        started: datetime = row["started_at"]
        return LibraryCopySummary(
            copy_id=row["id"],
            direction=row["direction"],
            request=LibraryCopyRequest.model_validate(row["request"]),
            status=row["status"],
            error=row["error"],
            started_at=started,
            finished_at=row["finished_at"],
            run_id=row["run_id"],
            **counts.model_dump(),
        )


class TargetLocks:
    """Session advisory locks, one per target library and SHA-256, held from the hash
    check until the file's row is recorded: two copies into one library never both miss
    the check and both put the same bytes. They live on a connection of their own, never
    the row pool's: a wait for another copy's put must not hold a pooled connection that
    the row writes need. Closing the connection ends every lock on it, so a worker that
    stops mid-put frees them. The calls block: run them on a thread."""

    def __init__(self, conninfo: Any) -> None:
        self._conninfo = conninfo
        self._conn: psycopg.Connection[Any] | None = None
        self._held: list[str] = []

    def acquire(self, direction: str, sha256: str) -> None:
        if self._conn is None:
            info = self._conninfo
            self._conn = psycopg.connect(
                info() if callable(info) else info,
                autocommit=True,
                connect_timeout=TARGET_LOCK_CONNECT_TIMEOUT,
                options=f"-c lock_timeout={TARGET_LOCK_TIMEOUT_MS}",
            )
        key = _key(direction, sha256)
        self._conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
        self._held.append(key)

    def release_all(self) -> None:
        """Every lock taken since the last release. The caller releases them once the
        file's row is recorded."""
        while self._held and self._conn is not None:
            key = self._held.pop()
            self._conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _key(direction: str, sha256: str) -> str:
    return f"scadbuddy:library-copy:{direction}:{sha256}"
