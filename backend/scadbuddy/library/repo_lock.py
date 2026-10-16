"""The models repository's write lock across API replicas, in Postgres (#2385).

Every replica mounts the same ``/data`` over NFS, and ``data/models`` is one git
repository they all commit to (#90). The ``flock`` in `history.ModelHistory` does
reach the NAS, but it serialises writers without making the next one's NFS client
read ``.git`` afresh: a replica could take the lock, read the HEAD it cached before
another's commit, and have git refuse (``cannot lock ref 'HEAD'``) or check an
edit's ``base`` against an old revision. So the lock here also carries the HEAD each
writer leaves, in ``models_repo_head``: the next writer waits until git shows it.

The flows repository (``data/flows``, #2503) is shared the same way, and takes a
lock of its own with its HEAD in ``flows_repo_head`` (:func:`flows_coordinator`).

A session-level advisory lock on a connection of its own, closed on the way out,
as `bambuddy.uploads.BambuddyUploadStore.copy_lock` takes its own: the hold spans
git calls, and closing the connection releases the lock however the section ends.

Recording a models HEAD is also the signal (#2507): in the same transaction as the
row, it ``NOTIFY``s `HEAD_CHANNEL` with the commit and the templates it touched, so
every API replica (`library/repo_signal.py`) refreshes its view of the share at once
rather than when its NFS cache expires. NFS itself tells other clients nothing. The
flows repository announces nothing: nothing caches it per replica.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import sql

from scadbuddy.library.history import LOCK_POLL_SECONDS, GitError, GitTimeoutError, HeadChange

logger = logging.getLogger(__name__)

#: Hashed into the advisory lock's key; no other lock hashes this text.
LOCK_KEY = "scadbuddy:models-repository"
FLOWS_LOCK_KEY = "scadbuddy:flows-repository"
#: The channel a recorded models HEAD is announced on.
HEAD_CHANNEL = "scadbuddy_models_head"
#: Postgres refuses a NOTIFY payload of 8000 bytes or more (the channel name is not
#: counted). Past half that, the templates are left out and every listener refreshes
#: everything.
MAX_PAYLOAD_BYTES = 4000


def encode_change(change: HeadChange) -> str:
    """The NOTIFY payload for ``change``: ``models`` null when too many to name."""
    payload = json.dumps({"head": change.commit, "models": change.models})
    if len(payload.encode()) <= MAX_PAYLOAD_BYTES:
        return payload
    # Why every replica is about to refresh every template (#2619).
    logger.debug(
        "a models HEAD names too many templates to announce; listeners refresh them all",
        extra={"commit": change.commit, "models": len(change.models or [])},
    )
    return json.dumps({"head": change.commit, "models": None})


def decode_change(payload: str) -> HeadChange:
    """A payload of :func:`encode_change`; ``ValueError`` for anything else."""
    data = json.loads(payload)
    if not isinstance(data, dict) or not isinstance(data.get("head"), str):
        raise ValueError("not a models HEAD payload")
    models = data.get("models")
    if models is not None and not (
        isinstance(models, list) and all(isinstance(item, str) for item in models)
    ):
        raise ValueError("not a models HEAD payload")
    return HeadChange(data["head"], models)


class RepoLockError(GitError):
    """Postgres could not be asked for the lock, or could not record the new HEAD."""


class _PgHold:
    def __init__(
        self,
        conn: psycopg.Connection[tuple[str]],
        expected: str | None,
        table: str,
        label: str,
        channel: str | None,
    ) -> None:
        self._conn = conn
        self.expected = expected
        self._table = table
        self._label = label
        self._channel = channel

    def record(self, change: HeadChange) -> None:
        commit = change.commit
        try:
            # One transaction: the NOTIFY is delivered when the row commits, never
            # without it, and before the lock is released.
            with self._conn.transaction():
                self._conn.execute(
                    sql.SQL(
                        "INSERT INTO {} (id, head) VALUES (true, %s)"
                        " ON CONFLICT (id) DO UPDATE SET head = excluded.head, updated_at = now()"
                    ).format(sql.Identifier(self._table)),
                    (commit,),
                )
                if self._channel is not None:
                    self._conn.execute(
                        "SELECT pg_notify(%s, %s)", (self._channel, encode_change(change))
                    )
        except psycopg.Error as error:
            raise RepoLockError(
                f"could not record {commit[:7]} as the {self._label} HEAD"
            ) from error
        self.expected = commit


class PgRepoCoordinator:
    """`history.RepoCoordinator` on the app's database.

    A connection per hold, never the shared pool: the lock is session-level, so a
    pooled connection would carry it to whoever borrows the connection next. Writes
    are a person's or an agent's saves, so a connect each costs nothing noticeable.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        lock_key: str = LOCK_KEY,
        table: str = "models_repo_head",
        label: str = "models",
        channel: str | None = HEAD_CHANNEL,
    ) -> None:
        self.conninfo = conninfo
        self.lock_key = lock_key
        self.table = table
        self.label = label
        #: Where a recorded HEAD is announced; None announces nothing.
        self.channel = channel

    @contextmanager
    def hold(self, timeout: float) -> Iterator[_PgHold]:
        deadline = time.monotonic() + timeout
        try:
            conn: psycopg.Connection[tuple[str]] = psycopg.connect(
                self.conninfo, autocommit=True, connect_timeout=max(1, round(timeout))
            )
        except psycopg.Error as error:
            raise RepoLockError(f"could not reach Postgres for the {self.label} lock") from error
        try:
            try:
                while True:
                    row = conn.execute(
                        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (self.lock_key,)
                    ).fetchone()
                    if row is not None and row[0]:
                        break
                    if time.monotonic() >= deadline:
                        raise GitTimeoutError(
                            f"waited {timeout:g}s for the {self.label} repository's lock"
                            " in Postgres"
                        )
                    time.sleep(LOCK_POLL_SECONDS)
                head = conn.execute(
                    sql.SQL("SELECT head FROM {} WHERE id").format(sql.Identifier(self.table))
                ).fetchone()
            except psycopg.Error as error:
                raise RepoLockError(f"could not take the {self.label} lock") from error
            yield _PgHold(
                conn, head[0] if head is not None else None, self.table, self.label, self.channel
            )
        finally:
            conn.close()


def flows_coordinator(conninfo: str) -> PgRepoCoordinator:
    """The flows repository's lock, apart from the models': its own key and HEAD row."""
    return PgRepoCoordinator(
        conninfo, lock_key=FLOWS_LOCK_KEY, table="flows_repo_head", label="flows", channel=None
    )
