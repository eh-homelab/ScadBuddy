"""The id claim in a table (#7257, plan Task A4, D6): `OutputStore.claim` claims the
``output_claims`` row first, then makes the folder, so a release A pod and a release B
pod (the table only) share one claim during release B's roll."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.output_rows import OutputRows
from scadbuddy.library.outputs import OutputStore, reap_orphan_holds
from scadbuddy.store.refs import BlobRefs
from tests.support.store import store_pool

pytestmark = pytest.mark.requires_postgres

Pool = ConnectionPool[Connection[DictRow]]

SLUG = "cube"


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened


def _store(paths: DataPaths, pool: Pool) -> OutputStore:
    store = OutputStore(paths)
    store.rows = OutputRows(pool)
    return store


def _claims(pool: Pool) -> dict[str, str]:
    with pool.connection() as conn:
        found = conn.execute("SELECT output_id, slug FROM output_claims").fetchall()
    return {row["output_id"]: row["slug"] for row in found}


def test_claim_takes_row_then_folder(tmp_path: Path, pool: Pool) -> None:
    """One claim wins across two pods. A row claim that fails makes no folder. A folder
    that is there after the row claim succeeded releases the row claim. With no rows,
    the folder only."""
    paths = DataPaths(root=tmp_path)
    one, two = _store(paths, pool), _store(paths, pool)
    first, second, third = "a" * 32, "b" * 32, "c" * 32

    assert one.claim(SLUG, first) is True
    assert _claims(pool) == {first: SLUG}
    assert paths.output_dir(SLUG, first).is_dir()
    assert two.claim(SLUG, first) is False

    # A release B pod holds the row claim and makes no folder: the row refuses.
    assert OutputRows(pool).claim(second, SLUG) is True
    assert two.claim(SLUG, second) is False
    assert not paths.output_dir(SLUG, second).exists()

    # A phase 2 pod made the folder and no row claim: the folder refuses, and the
    # row claim this call took is released.
    paths.output_dir(SLUG, third).mkdir(parents=True)
    assert two.claim(SLUG, third) is False
    assert third not in _claims(pool)

    files_only = OutputStore(DataPaths(root=tmp_path / "files"))
    assert files_only.claim(SLUG, first) is True
    assert files_only.paths.output_dir(SLUG, first).is_dir()
    assert files_only.claim(SLUG, first) is False
    assert _claims(pool) == {first: SLUG, second: SLUG}


def test_reaper_prunes_stale_claims(tmp_path: Path, pool: Pool) -> None:
    """A save that died between its claim and its row leaves a claim; the hold reaper
    deletes it past the grace, so a later attempt can claim the id again."""
    store = _store(DataPaths(root=tmp_path), pool)
    rows = OutputRows(pool)
    assert rows.claim("a" * 32, SLUG) is True
    refs = BlobRefs(pool)
    assert reap_orphan_holds(refs, store) == 0
    assert _claims(pool) == {"a" * 32: SLUG}
    assert reap_orphan_holds(refs, store, grace=timedelta(0)) == 0
    assert _claims(pool) == {}
    assert rows.claim("a" * 32, SLUG) is True


class _ReleaseFails(OutputRows):
    def release_claim(self, output_id: str) -> None:
        raise psycopg.OperationalError("the database is gone")


def test_failed_release_in_claim_is_logged_not_raised(
    tmp_path: Path,
    pool: Pool,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row claim's release that fails (A4 review M2) is logged and left to the
    reaper's prune. A folder that is there still answers False, so a quote takes a fresh
    id. Another mkdir error raises as that error, not as the database's."""
    paths = DataPaths(root=tmp_path)
    store = OutputStore(paths)
    store.rows = _ReleaseFails(pool)
    taken = "d" * 32
    paths.output_dir(SLUG, taken).mkdir(parents=True)
    with caplog.at_level(logging.ERROR, logger="scadbuddy.library.outputs"):
        assert store.claim(SLUG, taken) is False
    assert "could not release a claim" in caplog.text

    # A refusal by mode would not show as root (CI's pytest), so the mkdir raises here.
    blocked = paths.output_dir(SLUG, "e" * 32)
    real_mkdir = Path.mkdir

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        if self == blocked:
            raise PermissionError(13, "Permission denied", str(self))
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "mkdir", refuse)
    with pytest.raises(PermissionError):
        store.claim(SLUG, "e" * 32)
