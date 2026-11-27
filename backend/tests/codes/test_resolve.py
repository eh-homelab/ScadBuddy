"""`codes/resolve.py` (#6955): `K-<n>` is an item kind (#6949)."""

from __future__ import annotations

import pytest

from scadbuddy.codes.parse import Code
from scadbuddy.codes.resolve import resolve
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


def test_a_kind_resolves_by_its_number(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO item_kinds (id, name, parent_id, tracking)"
            " VALUES ('fastener', 'Fastener', 'part', 'unit') RETURNING number"
        ).fetchone()
        assert row is not None
        got = resolve(conn, Code("K", row["number"]))
    assert got is not None
    assert got.model_dump() == {
        "kind": "item_kind",
        "id": "fastener",
        "code": f"K-{row['number']}",
        "name": "Fastener",
        "archived": False,
    }


def test_an_archived_kind_still_resolves(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO item_kinds (id, name, parent_id, tracking, archived_at)"
            " VALUES ('gone', 'Gone', 'part', 'unit', now()) RETURNING number"
        ).fetchone()
        assert row is not None
        got = resolve(conn, Code("K", row["number"]))
    assert got is not None and got.archived is True


def test_an_unknown_kind_number_is_none(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        assert resolve(conn, Code("K", 999_999)) is None
