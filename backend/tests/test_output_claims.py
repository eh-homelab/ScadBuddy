"""The id claim in a table (#7257, plan Task A4, D6): `OutputStore.claim` claims the
``output_claims`` row first, then makes the folder, so a release A pod and a release B
pod (the table only) share one claim during release B's roll."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

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
    return OutputStore(paths, rows=OutputRows(pool), bytes=None)


def _claims(pool: Pool) -> dict[str, str]:
    with pool.connection() as conn:
        found = conn.execute("SELECT output_id, slug FROM output_claims").fetchall()
    return {row["output_id"]: row["slug"] for row in found}


def test_claim_takes_a_row_and_makes_no_folder(tmp_path: Path, pool: Pool) -> None:
    """One claim wins across two pods (#7257, plan D6). The claim is a row only: no
    folder is made, and a folder there from an older pod does not matter."""
    paths = DataPaths(root=tmp_path)
    one, two = _store(paths, pool), _store(paths, pool)
    first, second = "a" * 32, "b" * 32

    assert one.claim(SLUG, first) is True
    assert _claims(pool) == {first: SLUG}
    assert not (paths.root / "outputs" / SLUG / first).exists()
    assert two.claim(SLUG, first) is False

    (paths.root / "outputs" / SLUG / second).mkdir(parents=True)
    assert two.claim(SLUG, second) is True
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
