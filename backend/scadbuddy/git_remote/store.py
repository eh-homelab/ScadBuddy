"""``git_remote_pushes`` (migration ``20261101T1728Z_git_remote_pushes``): per repository,
the last commit a push landed at the remote, and how the last attempt went."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class PushRecord:
    repository: str
    #: The remote this row is about; a row for another URL than the one set is stale.
    url: str
    #: The last commit a push of this repository landed at ``url``.
    pushed_commit: str | None
    pushed_at: datetime | None
    attempted_at: datetime
    #: Why the last attempt failed, scrubbed of the credential; None once one lands.
    error: str | None


class PushRecords:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def get(self, repository: str) -> PushRecord | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT repository, url, pushed_commit, pushed_at, attempted_at, error"
                " FROM git_remote_pushes WHERE repository = %s",
                (repository,),
            ).fetchone()
        return None if row is None else PushRecord(**row)

    def pushed(self, repository: str, url: str, commit: str) -> None:
        """``commit`` landed at ``url``."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO git_remote_pushes"
                " (repository, url, pushed_commit, pushed_at, attempted_at, error)"
                " VALUES (%s, %s, %s, now(), now(), NULL)"
                " ON CONFLICT (repository) DO UPDATE SET url = excluded.url,"
                " pushed_commit = excluded.pushed_commit, pushed_at = excluded.pushed_at,"
                " attempted_at = excluded.attempted_at, error = NULL",
                (repository, url, commit),
            )

    def failed(self, repository: str, url: str, error: str) -> None:
        """A push to ``url`` failed: what it last landed stays, unless that was another
        URL's."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO git_remote_pushes (repository, url, attempted_at, error)"
                " VALUES (%s, %s, now(), %s)"
                " ON CONFLICT (repository) DO UPDATE SET"
                " pushed_commit = CASE WHEN git_remote_pushes.url = excluded.url"
                "   THEN git_remote_pushes.pushed_commit END,"
                " pushed_at = CASE WHEN git_remote_pushes.url = excluded.url"
                "   THEN git_remote_pushes.pushed_at END,"
                " url = excluded.url, attempted_at = excluded.attempted_at,"
                " error = excluded.error",
                (repository, url, error),
            )
