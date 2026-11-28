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
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.models import SpoolCreate, SpoolPatch
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.orders.models import Actor
from scadbuddy.printers.models import PrinterCreate
from scadbuddy.printers.store import PrinterStore
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


def _wait_for_the_log_lock(pool: PgPool, waiters: int = 1) -> None:
    """Until ``waiters`` backends of this database wait for the event log's lock. A bigint
    advisory key is ``classid`` (its high 32 bits) and ``objid`` (its low 32 bits) with
    ``objsubid`` 1, so another test's advisory waiter under xdist is not counted."""
    deadline = time.monotonic() + 10
    with pool.connection() as conn:
        while time.monotonic() < deadline:
            row = conn.execute(
                "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                " AND classid = %s AND objid = %s AND objsubid = 1",
                (EVENT_LOG_LOCK >> 32, EVENT_LOG_LOCK & 0xFFFFFFFF),
            ).fetchone()
            if row is not None and row["n"] >= waiters:
                return
            time.sleep(0.02)
    raise AssertionError(f"fewer than {waiters} writers waited for the event log's lock")


def _row_is_free(pool: PgPool, table: str, row_id: str) -> bool:
    with pool.connection() as conn, conn.transaction():
        try:
            conn.execute(
                f"SELECT 1 FROM {table} WHERE id = %s FOR UPDATE NOWAIT".encode(), (row_id,)
            )
        except psycopg.errors.LockNotAvailable:
            return False
        return True


def _material(conn: Connection[DictRow]) -> str:
    row = conn.execute("SELECT id FROM filament_materials WHERE name = 'PLA'").fetchone()
    assert row is not None
    return str(row["id"])


@pytest.mark.parametrize("writer", ["apply_on", "merge", "sweep", "spool_update"])
def test_a_writer_takes_the_log_lock_before_the_entity_row(pg_pool: PgPool, writer: str) -> None:
    """The lock order of `pg_events.py`: the log first, then rows. While another
    transaction holds the log, the writer waits for it holding no entity row (and a spool
    save no spool row), so a merge and a write on the merged entity cannot deadlock
    (40P01)."""
    table = "entities"
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
        if writer == "spool_update":
            table = "filament_spools"
            spool = FilamentSpoolStore(pg_pool).create(
                SpoolCreate(material_id=uuid.UUID(_material(conn))), actor="t"
            )
            entity_id = str(spool.id)
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
            elif writer == "spool_update":
                FilamentSpoolStore(pg_pool).update(
                    uuid.UUID(entity_id),
                    SpoolPatch(properties={"lot_number": "L1"}),
                    actor="t",
                    by=AGENT,
                )
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
            assert _row_is_free(pg_pool, table, entity_id), f"{writer} locked a row before the log"
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors


SERIAL = "01P00A000006949"


def test_a_merge_and_a_spool_save_both_finish(pg_pool: PgPool) -> None:
    """The deadlock of the Task 2 review: a printer merge holds the log and then locks the
    gone printer's tray spools; a save of one of those spools with properties used to
    lock the spool and then wait for the log. Queued in that order behind a held log, both
    now finish, with no 40P01."""
    printers = PrinterStore(pg_pool, None)
    synthetic = printers.ensure_bambuddy(7, serial=None, name="H2C", model="H2C", by=PERSON)
    mine = printers.create(PrinterCreate(serial=SERIAL, name="Mine"))
    places, spools = LocationStore(pg_pool), FilamentSpoolStore(pg_pool)
    gone_tray = places.ensure_ams(uuid.UUID(synthetic.id), 0, 1, label="gone A1")
    places.ensure_ams(uuid.UUID(mine.id), 0, 1, label="kept A1")
    with pg_pool.connection() as conn:
        material = uuid.UUID(_material(conn))
    spool = spools.create(SpoolCreate(material_id=material), actor="t")
    places.move(spool.id, gone_tray.id, actor="t")
    errors: list[BaseException] = []

    def merge() -> None:
        try:
            printers.ensure_bambuddy(7, serial=SERIAL, name="H2C", model="H2C", by=AGENT)
        except BaseException as e:  # reported on the test's thread
            errors.append(e)

    def save() -> None:
        try:
            spools.update(
                spool.id, SpoolPatch(properties={"lot_number": "L1"}), actor="t", by=PERSON
            )
        except BaseException as e:  # reported on the test's thread
            errors.append(e)

    with pg_pool.connection() as holder:
        with holder.transaction():
            holder.execute("SELECT pg_advisory_xact_lock(%s)", (EVENT_LOG_LOCK,))
            threads = [threading.Thread(target=merge), threading.Thread(target=save)]
            threads[0].start()
            _wait_for_the_log_lock(pg_pool, 1)
            threads[1].start()
            _wait_for_the_log_lock(pg_pool, 2)
        for thread in threads:
            thread.join(timeout=20)
    assert not any(t.is_alive() for t in threads)
    assert not errors, errors
    saved = spools.get(spool.id)
    assert saved is not None


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


def test_a_source_only_change_is_published(pg_pool: PgPool) -> None:
    """#8190: a save that keeps the value but changes who owns it (a provider's value
    saved as `manual`) is an event, so the history's source is not left stale."""
    with pg_pool.connection() as conn:
        entity_id = _entity(conn, "printer", str(uuid.uuid4()), {"nozzle_note": "hardened"})
        mark = _newest(conn)
        with conn.transaction():
            set_actor(conn, AGENT)
            conn.execute(
                "UPDATE entity_properties SET source = 'bambuddy'"
                " WHERE entity_id = %s AND key = 'nozzle_note'",
                (entity_id,),
            )
        with conn.transaction():
            set_actor(conn, AGENT)
            conn.execute(
                "UPDATE entity_properties SET updated_at = clock_timestamp()"
                " WHERE entity_id = %s AND key = 'nozzle_note'",
                (entity_id,),
            )
        events = _events(conn, mark)
    assert [(e["op"], e["old"], e["new"], e["source"]) for e in events] == [
        ("set", "hardened", "hardened", "bambuddy")
    ]


def test_history_lists_a_row_both_reads_find_once(
    pg_pool: PgPool, history_rows: HistoryRows
) -> None:
    """#8190: the read is one query on the ref and one on the current entity. A row of
    the current entity under its own ref is found by both and listed once."""
    store = _store(pg_pool)
    ref = EntityRef("printer", str(uuid.uuid4()))
    with pg_pool.connection() as conn:
        entity_id = _entity(conn, ref.kind, ref.ref, {})
        base = {"entity_id": entity_id, "kind": ref.kind, "ref": ref.ref, "source": "manual"}
        history_rows(
            conn,
            {**base, "event_seq": 10, "key": "nozzle_note", "op": "set", "new": "hardened"},
            {**base, "event_seq": 11, "key": "other", "op": "set", "new": 1},
        )
    assert [(c.key, c.new) for c in store.history(ref, None, 100)] == [
        ("other", 1),
        ("nozzle_note", "hardened"),
    ]
    assert [c.key for c in store.history(ref, None, 1)] == ["other"]
    assert [c.key for c in store.history(ref, "nozzle_note", 100)] == ["nozzle_note"]


def test_history_lists_a_value_an_older_rule_allowed(
    pg_pool: PgPool, history_rows: HistoryRows
) -> None:
    """#8116: a stored value today's rule refuses (an object, a list of numbers) is
    still listed, not a 500. Each change carries its event's seq."""
    store = _store(pg_pool)
    ref = EntityRef("spool", str(uuid.uuid4()))
    base = {"entity_id": str(uuid.uuid4()), "kind": ref.kind, "ref": ref.ref, "source": "manual"}
    with pg_pool.connection() as conn:
        history_rows(
            conn,
            {**base, "event_seq": 20, "key": "dims", "op": "set", "new": {"w": 1}},
            {**base, "event_seq": 21, "key": "dims", "op": "set", "old": {"w": 1}, "new": [1, 2]},
        )
    changes = store.history(ref, None, 100)
    assert [(c.event_seq, c.old, c.new) for c in changes] == [
        (21, {"w": 1}, [1, 2]),
        (20, None, {"w": 1}),
    ]
    # The keyed read (the other branch of the UNION) lists them alike.
    keyed = store.history(ref, "dims", 100)
    assert [(c.event_seq, c.old, c.new) for c in keyed] == [
        (21, {"w": 1}, [1, 2]),
        (20, None, {"w": 1}),
    ]


def test_history_follows_an_entity_a_merge_renamed(
    pg_pool: PgPool, history_rows: HistoryRows
) -> None:
    """A merge into a printer with no entity row renames the entity (`repoint_entity`), and
    its history rows keep the old ref: the kept printer's history still lists them."""
    store = _store(pg_pool)
    with pg_pool.connection() as conn:
        entity_id = _entity(conn, "printer", "b", {"nozzle_note": "hardened"})
        base = {"entity_id": entity_id, "kind": "printer", "ref": "b", "source": "manual"}
        history_rows(
            conn,
            {**base, "event_seq": 10, "key": "nozzle_note", "op": "set", "new": "hardened"},
            {
                **base,
                "event_seq": 11,
                "entity_id": str(uuid.uuid4()),
                "ref": "c",
                "key": "other",
                "op": "set",
                "new": 1,
            },
        )
        with conn.transaction():
            repoint_entity(conn, "printer", "b", "a", AGENT)
    changes = store.history(EntityRef("printer", "a"), None, 100)
    assert [(c.key, c.op, c.new) for c in changes] == [("nozzle_note", "set", "hardened")]


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
