"""Property changes are events (#6949, design §4.8): a trigger publishes each set and
remove in the writer's transaction, and no writer inserts a history row (the history
subscriber in scadbuddy-core does)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from scadbuddy.core.events import PG_CHANNEL
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

PERSON = {"kind": "browser"}
SWEEP = {"kind": "provider", "principal": "entities_sweep"}
CHANGED = "entity_property.changed"
SCHEMA_CHANGED = "entity_property_schema.changed"


def events_of(conn: Connection[DictRow], kind: str, after: int = 0) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM events WHERE kind = %s AND seq > %s ORDER BY seq", (kind, after)
    ).fetchall()
    return [r["payload"] for r in rows]


def _newest(conn: Connection[DictRow]) -> int:
    row = conn.execute("SELECT coalesce(max(seq), 0) AS seq FROM events").fetchone()
    assert row is not None
    return int(row["seq"])


def _actor(conn: Connection[DictRow], actor: dict[str, Any] | None) -> None:
    if actor is not None:
        conn.execute("SELECT set_config('scadbuddy.actor', %s, true)", (json.dumps(actor),))


def _item(conn: Connection[DictRow]) -> tuple[str, str]:
    ref = str(uuid.uuid4())
    row = conn.execute(
        "INSERT INTO entities (kind, ref) VALUES ('item', %s) RETURNING id", (ref,)
    ).fetchone()
    assert row is not None
    return str(row["id"]), ref


def _set(
    conn: Connection[DictRow],
    entity_id: str,
    key: str,
    value: Any,
    actor: dict[str, Any] | None = PERSON,
) -> None:
    with conn.transaction():
        _actor(conn, actor)
        conn.execute(
            "INSERT INTO entity_properties (entity_id, key, value, updated_by)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT (entity_id, key) DO UPDATE"
            " SET value = EXCLUDED.value, updated_at = clock_timestamp(),"
            " updated_by = EXCLUDED.updated_by",
            (entity_id, key, Jsonb(value), Jsonb(actor or PERSON)),
        )


def _keys(conn: Connection[DictRow], entity_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT key FROM entity_properties WHERE entity_id = %s ORDER BY key", (entity_id,)
    ).fetchall()
    return [r["key"] for r in rows]


def test_a_set_publishes_one_event(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        entity_id, ref = _item(conn)
        mark = _newest(conn)
        _set(conn, entity_id, "colour_name", "Gold")
        events = events_of(conn, CHANGED, mark)
        assert len(events) == 1
        event = events[0]
        assert event["op"] == "set"
        assert event["old"] is None
        assert event["new"] == "Gold"
        assert event["entity_id"] == entity_id
        assert event["entity_kind"] == "item"
        assert event["ref"] == ref
        assert event["key"] == "colour_name"
        assert event["source"] == "manual"
        assert event["actor"] == PERSON
        assert event["retention_seconds"] == 0
        assert event["kind"] == CHANGED
        history = conn.execute("SELECT count(*) AS n FROM entity_property_history").fetchone()
        assert history == {"n": 0}


def test_a_set_to_the_same_value_publishes_nothing(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        entity_id, _ = _item(conn)
        mark = _newest(conn)
        _set(conn, entity_id, "colour_name", "Silk Gold")
        _set(conn, entity_id, "colour_name", "Silk Gold")
        assert len(events_of(conn, CHANGED, mark)) == 1


def test_a_change_keeps_the_old_value(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        entity_id, _ = _item(conn)
        mark = _newest(conn)
        _set(conn, entity_id, "colour_name", "Gold")
        _set(conn, entity_id, "colour_name", "Silk Gold")
        events = events_of(conn, CHANGED, mark)
        assert [(e["op"], e["old"], e["new"]) for e in events] == [
            ("set", None, "Gold"),
            ("set", "Gold", "Silk Gold"),
        ]


def test_the_set_actor_is_the_setting_before_updated_by(pg_pool: PgPool) -> None:
    """A merge copies `updated_by` from the old row: the actor who merged is credited."""
    merger = {"kind": "agent", "principal": "p"}
    with pg_pool.connection() as conn:
        entity_id, _ = _item(conn)
        mark = _newest(conn)
        with conn.transaction():
            _actor(conn, merger)
            conn.execute(
                "INSERT INTO entity_properties (entity_id, key, value, updated_by)"
                " VALUES (%s, 'a', '1', %s)",
                (entity_id, Jsonb(PERSON)),
            )
        _set(conn, entity_id, "b", 2, actor=None)
        events = events_of(conn, CHANGED, mark)
        assert [e["actor"] for e in events] == [merger, PERSON]


def test_a_remove_needs_an_actor(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        entity_id, _ = _item(conn)
        _set(conn, entity_id, "colour_name", "Gold")
        mark = _newest(conn)
        with pytest.raises(psycopg.errors.RaiseException, match=r"needs scadbuddy\.actor"):
            conn.execute(
                "DELETE FROM entity_properties WHERE entity_id = %s AND key = 'colour_name'",
                (entity_id,),
            )
        assert _keys(conn, entity_id) == ["colour_name"]
        assert events_of(conn, CHANGED, mark) == []
        with conn.transaction():
            _actor(conn, PERSON)
            conn.execute(
                "DELETE FROM entity_properties WHERE entity_id = %s AND key = 'colour_name'",
                (entity_id,),
            )
        events = events_of(conn, CHANGED, mark)
        assert [(e["op"], e["old"], e["new"], e["actor"]) for e in events] == [
            ("remove", "Gold", None, PERSON)
        ]


def test_a_cascade_publishes_one_remove_per_key(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        entity_id, ref = _item(conn)
        _set(conn, entity_id, "a", "1")
        _set(conn, entity_id, "b", 2)
        mark = _newest(conn)
        with pytest.raises(psycopg.errors.RaiseException, match=r"needs scadbuddy\.actor"):
            conn.execute("DELETE FROM entities WHERE id = %s", (entity_id,))
        assert _keys(conn, entity_id) == ["a", "b"]
        assert events_of(conn, CHANGED, mark) == []
        with conn.transaction():
            _actor(conn, SWEEP)
            conn.execute("DELETE FROM entities WHERE id = %s", (entity_id,))
        events = events_of(conn, CHANGED, mark)
        assert sorted(
            (e["key"], e["op"], e["old"], e["new"], e["entity_kind"], e["ref"], e["actor"])
            for e in events
        ) == [
            ("a", "remove", "1", None, "item", ref, SWEEP),
            ("b", "remove", 2, None, "item", ref, SWEEP),
        ]
        assert _keys(conn, entity_id) == []


def test_an_entity_with_no_property_deletes_with_no_actor(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        entity_id, _ = _item(conn)
        mark = _newest(conn)
        conn.execute("DELETE FROM entities WHERE id = %s", (entity_id,))
        assert events_of(conn, CHANGED, mark) == []


def test_a_rolled_back_write_publishes_nothing(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        entity_id, _ = _item(conn)
        mark = _newest(conn)
        conn.execute("BEGIN")
        _actor(conn, PERSON)
        conn.execute(
            "INSERT INTO entity_properties (entity_id, key, value, updated_by)"
            " VALUES (%s, 'colour_name', '\"Gold\"', %s)",
            (entity_id, Jsonb(PERSON)),
        )
        conn.execute("ROLLBACK")
        assert events_of(conn, CHANGED, mark) == []


def test_a_large_value_is_logged_whole(pg_pool: PgPool, pg_conninfo: str) -> None:
    large = "x" * 5000
    with psycopg.connect(pg_conninfo, autocommit=True) as listener:
        listener.execute(f"LISTEN {PG_CHANNEL}")
        with pg_pool.connection() as conn:
            entity_id, _ = _item(conn)
            mark = _newest(conn)
            _set(conn, entity_id, "note", large)
            events = events_of(conn, CHANGED, mark)
        assert [e["new"] for e in events] == [large]
        heard: list[dict[str, Any]] = []
        for notify in listener.notifies(timeout=5.0):
            payload = json.loads(notify.payload)
            if payload["kind"] == CHANGED:
                heard.append(payload)
                break
    assert len(heard) == 1
    assert heard[0]["values_in_log"] is True
    assert "new" not in heard[0]
    assert "old" not in heard[0]
    assert heard[0]["id"] == events[0]["id"]


def test_a_schema_change_publishes_one_event(pg_pool: PgPool) -> None:
    schema = {"type": "object", "properties": {"lot": {"type": "string"}}}
    with pg_pool.connection() as conn:
        mark = _newest(conn)
        for _ in range(2):
            with conn.transaction():
                _actor(conn, PERSON)
                conn.execute(
                    "INSERT INTO entity_property_schemas (kind, schema, updated_by)"
                    " VALUES ('spool', %s, %s) ON CONFLICT (kind) DO UPDATE"
                    " SET schema = EXCLUDED.schema, updated_by = EXCLUDED.updated_by",
                    (Jsonb(schema), Jsonb(PERSON)),
                )
        events = events_of(conn, SCHEMA_CHANGED, mark)
        assert [(e["owner"], e["new"], e["actor"]) for e in events] == [
            ("kind:spool", schema, PERSON)
        ]
        assert events[0]["old"]["properties"]["lot_number"]["type"] == "string"
        assert events[0]["retention_seconds"] == 0

        mark = _newest(conn)
        with conn.transaction():
            _actor(conn, PERSON)
            conn.execute(
                "UPDATE item_kinds SET attribute_schema = %s WHERE id = 'nozzle'", (Jsonb(schema),)
            )
            conn.execute("UPDATE item_kinds SET name = 'Nozzles' WHERE id = 'nozzle'")
        events = events_of(conn, SCHEMA_CHANGED, mark)
        assert [(e["owner"], e["new"], e["actor"]) for e in events] == [
            ("item_kind:nozzle", schema, PERSON)
        ]
