"""Every Python writer of properties names its actor (#6949, K1): `set_actor` sets
`scadbuddy.actor` for the triggers and takes the event log's lock first, before any row
lock, so a writer never waits for the log while it holds an entity row. The history reads
(`EntityPropertyStore.history`, `schema_history`) read the rows the Go subscriber writes."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from scadbuddy.core.pg_events import EVENT_LOG_LOCK
from scadbuddy.entities.actor import SWEEP_ACTOR, set_actor
from scadbuddy.entities.attachments import EntityAttachmentStore
from scadbuddy.entities.properties import EntityPropertyStore, PropertyPatch
from scadbuddy.entities.refs import EntityRef
from scadbuddy.entities.registry import EntityStore, repoint_entity
from scadbuddy.entities.sweep import sweep_orphan_entities
from scadbuddy.orders.models import Actor
from tests.conftest import HistoryRows, PgPool

pytestmark = pytest.mark.requires_postgres

AGENT = Actor(kind="agent", principal="p")
PERSON = Actor(kind="browser")
CHANGED = "entity_property.changed"


def _events(conn: Connection[DictRow], after: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM events WHERE kind = %s AND seq > %s ORDER BY seq", (CHANGED, after)
    ).fetchall()
    return [r["payload"] for r in rows]


def _newest(conn: Connection[DictRow]) -> int:
    row = conn.execute("SELECT coalesce(max(seq), 0) AS seq FROM events").fetchone()
    assert row is not None
    return int(row["seq"])


def _spool_type(conn: Connection[DictRow]) -> EntityRef:
    row = conn.execute(
        "INSERT INTO filament_spool_types (code, material_id, rgba, label_weight_g)"
        " SELECT %s, id, '#FFFFFFFF', 1000 FROM filament_materials WHERE name = 'PLA'"
        " RETURNING id",
        (f"T{uuid.uuid4().hex[:10]}",),
    ).fetchone()
    assert row is not None
    return EntityRef("spool_type", str(row["id"]))


def _entity(conn: Connection[DictRow], kind: str, ref: str, values: dict[str, Any]) -> str:
    """An entity row with ``values``, written as a person would."""
    with conn.transaction():
        set_actor(conn, PERSON)
        row = conn.execute(
            "INSERT INTO entities (kind, ref) VALUES (%s, %s) RETURNING id", (kind, ref)
        ).fetchone()
        assert row is not None
        for key, value in values.items():
            conn.execute(
                "INSERT INTO entity_properties (entity_id, key, value, updated_by)"
                " VALUES (%s, %s, %s, %s)",
                (row["id"], key, Jsonb(value), Jsonb(PERSON.model_dump(mode="json"))),
            )
    return str(row["id"])


def _store(pool: PgPool) -> EntityPropertyStore:
    return EntityPropertyStore(pool, EntityStore(pool))


def test_set_actor_names_the_actor_for_the_transaction(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        with conn.transaction():
            set_actor(conn, AGENT)
            row = conn.execute("SELECT current_setting('scadbuddy.actor') AS a").fetchone()
            assert row is not None
            assert Actor.model_validate_json(row["a"]) == AGENT
        row = conn.execute("SELECT current_setting('scadbuddy.actor', true) AS a").fetchone()
        assert row is not None
        assert not row["a"]
    assert Actor(kind="provider", principal="entities_sweep") == SWEEP_ACTOR


def test_apply_on_publishes_a_remove_with_its_actor(pg_pool: PgPool) -> None:
    store = _store(pg_pool)
    with pg_pool.connection() as conn:
        ref = _spool_type(conn)
    store.apply(ref, PropertyPatch(set={"material": "PLA"}), PERSON)
    with pg_pool.connection() as conn:
        mark = _newest(conn)
    store.apply(ref, PropertyPatch(set={"material": None}), AGENT)
    with pg_pool.connection() as conn:
        events = _events(conn, mark)
    assert [(e["op"], e["key"], e["old"]) for e in events] == [("remove", "material", "PLA")]
    assert Actor.model_validate(events[0]["actor"]) == AGENT


def test_a_merge_publishes_each_key_it_moves(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        kept = _entity(conn, "printer", "a", {"x": 1})
        gone = _entity(conn, "printer", "b", {"x": 2, "y": 3})
        mark = _newest(conn)
        with conn.transaction():
            repoint_entity(conn, "printer", "b", "a", AGENT)
        events = _events(conn, mark)
    by_entity = {
        (e["entity_id"], e["op"], e["key"]): Actor.model_validate(e["actor"]) for e in events
    }
    assert by_entity == {
        (kept, "set", "y"): AGENT,
        (gone, "remove", "x"): AGENT,
        (gone, "remove", "y"): AGENT,
    }


def _wait_for_the_log_lock(pool: PgPool) -> None:
    """Until another backend waits for an advisory lock (the event log's)."""
    deadline = time.monotonic() + 10
    with pool.connection() as conn:
        while time.monotonic() < deadline:
            row = conn.execute(
                "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            ).fetchone()
            if row is not None and row["n"]:
                return
            time.sleep(0.02)
    raise AssertionError("no writer waited for the event log's lock")


def _row_is_free(pool: PgPool, entity_id: str) -> bool:
    with pool.connection() as conn, conn.transaction():
        try:
            conn.execute("SELECT 1 FROM entities WHERE id = %s FOR UPDATE NOWAIT", (entity_id,))
        except psycopg.errors.LockNotAvailable:
            return False
        return True


@pytest.mark.parametrize("writer", ["apply_on", "merge", "sweep"])
def test_a_writer_takes_the_log_lock_before_the_entity_row(pg_pool: PgPool, writer: str) -> None:
    """The lock order of `pg_events.py`: the log first, then rows. While another
    transaction holds the log, the writer waits for it holding no entity row, so a merge
    and a write on the merged entity cannot deadlock (40P01)."""
    with pg_pool.connection() as conn:
        if writer == "apply_on":
            # `apply_on` checks its owner: a printer row whose id is the ref.
            printer = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO printers (id, name, serial) VALUES (%s, 'P', %s)",
                (printer, f"S{uuid.uuid4().hex[:12]}"),
            )
        entity_id = _entity(conn, "printer", "b", {"y": 3})
        if writer == "apply_on":
            conn.execute("UPDATE entities SET ref = %s WHERE id = %s", (printer, entity_id))
        _entity(conn, "printer", "a", {"x": 1})
    errors: list[BaseException] = []

    def target() -> None:
        try:
            if writer == "apply_on":
                _store(pg_pool).apply(
                    EntityRef("printer", printer), PropertyPatch(set={"y": None}), AGENT
                )
            elif writer == "merge":
                with pg_pool.connection() as conn, conn.transaction():
                    repoint_entity(conn, "printer", "b", "a", AGENT)
            else:
                entities = EntityStore(pg_pool)
                sweep_orphan_entities(entities, EntityAttachmentStore(pg_pool, entities))
        except BaseException as e:  # reported on the test's thread
            errors.append(e)

    with pg_pool.connection() as holder:
        with holder.transaction():
            holder.execute("SELECT pg_advisory_xact_lock(%s)", (EVENT_LOG_LOCK,))
            thread = threading.Thread(target=target)
            thread.start()
            _wait_for_the_log_lock(pg_pool)
            assert _row_is_free(pg_pool, entity_id), f"{writer} locked the row before the log"
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors


def test_history_lists_a_keys_changes_newest_first(
    pg_pool: PgPool, history_rows: HistoryRows
) -> None:
    store = _store(pg_pool)
    ref = EntityRef("spool", str(uuid.uuid4()))
    entity_id = str(uuid.uuid4())
    base = {"entity_id": entity_id, "kind": ref.kind, "ref": ref.ref, "source": "manual"}
    with pg_pool.connection() as conn:
        history_rows(
            conn,
            {**base, "event_seq": 10, "key": "colour_name", "op": "set", "new": "Gold"},
            {**base, "event_seq": 12, "key": "other", "op": "set", "new": 1},
            {
                **base,
                "event_seq": 14,
                "key": "colour_name",
                "op": "remove",
                "old": "Gold",
                "source": None,
                "actor": {"kind": "agent", "principal": "p"},
            },
            {**base, "event_seq": 16, "ref": str(uuid.uuid4()), "key": "colour_name", "op": "set"}
            | {"new": "Red"},
        )
    changes = store.history(ref, None, 100)
    assert [(c.key, c.op, c.old, c.new) for c in changes] == [
        ("colour_name", "remove", "Gold", None),
        ("other", "set", None, 1),
        ("colour_name", "set", None, "Gold"),
    ]
    assert changes[0].actor == AGENT
    assert changes[0].source is None
    assert [c.op for c in store.history(ref, "colour_name", 1)] == ["remove"]


def test_schema_history_lists_an_owners_changes_newest_first(
    pg_pool: PgPool, history_rows: HistoryRows
) -> None:
    store = _store(pg_pool)
    one = {"type": "object"}
    two = {"type": "object", "properties": {"a": {"type": "string"}}}
    with pg_pool.connection() as conn:
        history_rows(
            conn,
            {"event_seq": 1, "owner": "kind:spool", "new": one},
            {"event_seq": 2, "owner": "kind:printer", "new": one},
            {"event_seq": 3, "owner": "kind:spool", "old": one, "new": two},
        )
    changes = store.schema_history("kind:spool", 100)
    assert [(c.old, c.new) for c in changes] == [(one, two), (None, one)]
    assert changes[0].actor == PERSON
