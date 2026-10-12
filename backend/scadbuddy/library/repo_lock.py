"""The models repository's write lock across API replicas, in Postgres (#2385).

Every replica mounts the same ``/data`` over NFS, and ``data/models`` is one git
repository they all commit to (#90). The ``flock`` in `history.ModelHistory` does
reach the NAS, but it serialises writers without making the next one's NFS client
read ``.git`` afresh: a replica could take the lock, read the HEAD it cached before
another's commit, and have git refuse (``cannot lock ref 'HEAD'``) or check an
edit's ``base`` against an old revision. So the lock here also carries the HEAD each
writer leaves, in ``models_repo_head``: the next writer waits until git shows it.

A session-level advisory lock on a connection of its own, closed on the way out,
as `bambuddy.uploads.BambuddyUploadStore.copy_lock` takes its own: the hold spans
git calls, and closing the connection releases the lock however the section ends.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from scadbuddy.library.history import LOCK_POLL_SECONDS, GitError, GitTimeoutError

#: Hashed into the advisory lock's key; no other lock hashes this text.
LOCK_KEY = "scadbuddy:models-repository"


class RepoLockError(GitError):
    """Postgres could not be asked for the lock, or could not record the new HEAD."""


class _PgHold:
    def __init__(self, conn: psycopg.Connection[tuple[str]], expected: str | None) -> None:
        self._conn = conn
        self.expected = expected

    def record(self, commit: str) -> None:
        try:
            self._conn.execute(
                "INSERT INTO models_repo_head (id, head) VALUES (true, %s)"
                " ON CONFLICT (id) DO UPDATE SET head = excluded.head, updated_at = now()",
                (commit,),
            )
        except psycopg.Error as error:
            raise RepoLockError(f"could not record {commit[:7]} as the models HEAD") from error
        self.expected = commit


class PgRepoCoordinator:
    """`history.RepoCoordinator` on the app's database.

    A connection per hold, never the shared pool: the lock is session-level, so a
    pooled connection would carry it to whoever borrows the connection next. Writes
    are a person's or an agent's saves, so a connect each costs nothing noticeable.
    """

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    @contextmanager
    def hold(self, timeout: float) -> Iterator[_PgHold]:
        deadline = time.monotonic() + timeout
        try:
            conn: psycopg.Connection[tuple[str]] = psycopg.connect(
                self.conninfo, autocommit=True, connect_timeout=max(1, round(timeout))
            )
        except psycopg.Error as error:
            raise RepoLockError("could not reach Postgres for the models lock") from error
        try:
            try:
                while True:
                    row = conn.execute(
                        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (LOCK_KEY,)
                    ).fetchone()
                    if row is not None and row[0]:
                        break
                    if time.monotonic() >= deadline:
                        raise GitTimeoutError(
                            f"waited {timeout:g}s for the models repository's lock in Postgres"
                        )
                    time.sleep(LOCK_POLL_SECONDS)
                head = conn.execute("SELECT head FROM models_repo_head WHERE id").fetchone()
            except psycopg.Error as error:
                raise RepoLockError("could not take the models lock") from error
            yield _PgHold(conn, head[0] if head is not None else None)
        finally:
            conn.close()
