"""The entity registry: one row per thing a file or link can hang off (#6950, spec 6.1)."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import (
    KINDS,
    EntityNotFound,
    EntityStore,
    UnknownEntityKind,
    repoint_entity,
)
from scadbuddy.render.pg_store import MIGRATIONS, migrate
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


def _insert(pool: PgPool, sql: str) -> str:
    with pool.connection() as conn:
        row = conn.execute(sql.encode()).fetchone()
    assert row is not None
    return str(row["id"])


def _customer(pool: PgPool) -> str:
    return _insert(pool, "INSERT INTO customers (display_name) VALUES ('C') RETURNING id")


def _supplier(pool: PgPool) -> str:
    return _insert(pool, "INSERT INTO filament_suppliers (name) VALUES ('S') RETURNING id")


def _entity_rows(pool: PgPool) -> int:
    with pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM entities").fetchone()
    assert row is not None
    return int(row["n"])


def test_every_kind_names_a_real_table_and_id_column(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        for kind in KINDS.values():
            conn.execute(f"SELECT {kind.id_column} FROM {kind.table} LIMIT 0".encode())


def test_ensure_returns_the_same_id_for_an_existing_row(
    store: EntityStore, pg_pool: PgPool
) -> None:
    ref = EntityRef("customer", _customer(pg_pool))
    with pg_pool.connection() as conn:
        first = store.ensure(conn, ref)
        assert store.ensure(conn, ref) == first
        assert store.find(conn, ref) == first
        assert store.exists(conn, ref)
    assert _entity_rows(pg_pool) == 1


def test_ensure_refuses_a_row_that_is_not_there(store: EntityStore, pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn, pytest.raises(EntityNotFound):
        store.ensure(conn, EntityRef("spool", str(uuid.uuid4())))
    assert _entity_rows(pg_pool) == 0


def test_ensure_refuses_an_unknown_kind(store: EntityStore, pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn, pytest.raises(UnknownEntityKind):
        store.ensure(conn, EntityRef("banana", "1"))


def test_an_id_that_does_not_cast_is_not_found_and_leaves_the_connection_usable(
    store: EntityStore, pg_pool: PgPool
) -> None:
    with pg_pool.connection() as conn:
        with pytest.raises(EntityNotFound):
            store.ensure(conn, EntityRef("spool", "not-a-uuid"))
        assert not store.exists(conn, EntityRef("spool", "not-a-uuid"))
        assert conn.execute("SELECT 1 AS one").fetchone() == {"one": 1}


def test_find_is_none_for_an_unregistered_ref(store: EntityStore, pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        assert store.find(conn, EntityRef("customer", "nope")) is None


def test_orphans_lists_an_entity_whose_owner_row_is_gone(
    store: EntityStore, pg_pool: PgPool
) -> None:
    kept = EntityRef("customer", _customer(pg_pool))
    gone = EntityRef("supplier", _supplier(pg_pool))
    with pg_pool.connection() as conn:
        store.ensure(conn, kept)
        gone_id = store.ensure(conn, gone)
        assert store.orphans(conn, 10) == []
        conn.execute("DELETE FROM filament_suppliers WHERE id = %s", (gone.ref,))
        assert store.orphans(conn, 10) == [(gone_id, gone)]
        assert store.orphans(conn, 0) == []


@pytest.mark.pg_empty
def test_the_migration_backfills_an_entity_for_each_attachment_subject(pg_conninfo: str) -> None:
    # Applies every file before the entities one, writes attachments as the order-only
    # table held them, then applies the rest: the real backfill runs.
    at = next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith("_entities"))
    with psycopg.connect(pg_conninfo) as conn:
        migrate(conn, MIGRATIONS[:at])
        customer = conn.execute(
            "INSERT INTO customers (display_name) VALUES ('C') RETURNING id"
        ).fetchone()
        assert customer is not None
        order = conn.execute(
            "INSERT INTO orders (number, customer_id, currency, created_by)"
            " VALUES ('N1', %s, 'USD', '{}') RETURNING id",
            (customer[0],),
        ).fetchone()
        assert order is not None
        subjects = [f"order:{order[0]}", "order_item:item1", "order_item:item1"]
        for n, subject in enumerate(subjects):
            conn.execute(
                "INSERT INTO order_attachments"
                " (order_id, subject, kind, filename, media_type, size_bytes, sha256, blob_key,"
                "  created_by) VALUES (%s, %s, 'other', %s, 'text/plain', 1, %s, 'k', '{}')",
                (order[0], subject, f"f{n}", "0" * 64),
            )
        assert migrate(conn)[0] == MIGRATIONS[at].id
        rows = conn.execute(
            "SELECT a.subject, e.kind, e.ref FROM entity_attachments a"
            " JOIN entities e ON e.id = a.entity_id ORDER BY a.filename"
        ).fetchall()
        assert rows == [
            (f"order:{order[0]}", "order", order[0]),
            ("order_item:item1", "order_item", "item1"),
            ("order_item:item1", "order_item", "item1"),
        ]
        assert conn.execute("SELECT count(*) FROM entities").fetchone() == (2,)


def _file_on(pool: PgPool, file_id: str, ref: str) -> str:
    """A file on the entity `printer:<ref>`, which this makes; returns the entity's id."""
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO entities (kind, ref) VALUES ('printer', %s)"
            " ON CONFLICT (kind, ref) DO UPDATE SET ref = EXCLUDED.ref RETURNING id",
            (ref,),
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO entity_attachments (id, subject, entity_id, kind, filename, media_type,"
            " size_bytes, sha256, blob_key, created_by) VALUES (%s, %s, %s, 'image', 'a.png',"
            " 'image/png', 1, repeat('a', 64), %s, '{}')",
            (file_id, f"printer:{ref}", row["id"], f"attachment-{file_id}"),
        )
    return str(row["id"])


def _files(pool: PgPool) -> list[tuple[str, str, str, str]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT a.id, a.subject, a.entity_id::text AS entity_id, e.ref"
            " FROM entity_attachments a JOIN entities e ON e.id = a.entity_id ORDER BY a.id"
        ).fetchall()
    return [(r["id"], r["subject"], r["entity_id"], r["ref"]) for r in rows]


def test_a_repoint_onto_a_new_ref_keeps_each_files_entity(pg_pool: PgPool) -> None:
    """#7193: with no entity for `kept`, the repoint renames `gone`'s entity and moves each
    file's `subject` alone. No trigger fills `entity_id` since #6960, so the file must keep
    the same entity, now named `kept`."""
    entity = _file_on(pg_pool, "f1", "p1")
    with pg_pool.connection() as conn:
        repoint_entity(conn, "printer", "p1", "p2")
    assert _files(pg_pool) == [("f1", "printer:p2", entity, "p2")]


def test_a_repoint_onto_an_existing_ref_moves_each_file_to_its_entity(pg_pool: PgPool) -> None:
    """#7193: with an entity for `kept`, the files move to it and `gone`'s entity goes."""
    _file_on(pg_pool, "f1", "p1")
    kept = _file_on(pg_pool, "f2", "p2")
    with pg_pool.connection() as conn:
        repoint_entity(conn, "printer", "p1", "p2")
    assert _files(pg_pool) == [
        ("f1", "printer:p2", kept, "p2"),
        ("f2", "printer:p2", kept, "p2"),
    ]
    assert _entity_rows(pg_pool) == 1
