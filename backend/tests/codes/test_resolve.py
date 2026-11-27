"""`codes/resolve.py` (#6955): `K-<n>` is an item kind (#6949)."""

from __future__ import annotations

from uuid import UUID

import pytest

from scadbuddy.codes.parse import Code
from scadbuddy.codes.resolve import Conn, resolve
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


def _type(conn: Conn, colour_name: str | None) -> tuple[UUID, int]:
    row = conn.execute(
        "INSERT INTO filament_spool_types (code, material_id, rgba, label_weight_g, colour_name)"
        " SELECT '10101', id, '#FFFFFFFF', 1000, %s FROM filament_materials WHERE name = 'PLA'"
        " RETURNING id, number",
        (colour_name,),
    ).fetchone()
    assert row is not None
    return row["id"], row["number"]


def _item(conn: Conn, archived: bool = False) -> tuple[UUID, int]:
    row = conn.execute(
        "INSERT INTO items (kind_id, name, archived_at)"
        " VALUES ('hardware', 'Grease', CASE WHEN %s THEN now() END) RETURNING id, number",
        (archived,),
    ).fetchone()
    assert row is not None
    return row["id"], row["number"]


def test_a_type_resolves_by_its_number(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        type_id, number = _type(conn, "Charcoal")
        got = resolve(conn, Code("T", number))
    assert got is not None
    assert got.model_dump() == {
        "kind": "spool_type",
        "id": type_id,
        "code": f"T-{number}",
        "name": "10101 Charcoal",
        "archived": False,
    }


def test_a_type_with_no_colour_name_is_its_code(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        _, number = _type(conn, None)
        got = resolve(conn, Code("T", number))
    assert got is not None and got.name == "10101"


def test_an_item_resolves_by_its_number(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        item_id, number = _item(conn)
        got = resolve(conn, Code("I", number))
    assert got is not None
    assert got.model_dump() == {
        "kind": "item",
        "id": item_id,
        "code": f"I-{number}",
        "name": "Grease",
        "archived": False,
    }


def test_an_archived_item_still_resolves(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        _, number = _item(conn, archived=True)
        got = resolve(conn, Code("I", number))
    assert got is not None and got.archived is True


def test_a_type_code_never_names_an_item(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        _, type_number = _type(conn, None)
        _, item_number = _item(conn)
        assert resolve(conn, Code("T", item_number)) is None
        assert resolve(conn, Code("I", type_number)) is None
