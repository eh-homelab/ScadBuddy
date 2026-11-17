"""The plan spool-numbers migration (#6262 Task 8, F14, F15, F30): every stored spool id
that names a Bambuddy spool becomes the number of the SB spool it is linked to; an id
with no link is dropped, never read as SB spool #N (Review Focus 1). The SB numbers here
differ from every Bambuddy id, and SB #99 exists while BB 99 is unlinked, so an id left
unmapped shows."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from scadbuddy.render.pg_store import MIGRATIONS, migrate

pytestmark = pytest.mark.requires_postgres

Conn = psycopg.Connection[tuple[Any, ...]]

SLUG = "_filament_plan_spool_numbers"
#: BB 41 is linked to SB #3, BB 99 is unlinked (SB #99 exists), -2 is a tray chosen for
#: itself, BB 55 is known only by a record link to SB #5.
PLAN = [
    {"slot_id": 1, "spool_id": 41},
    {"slot_id": 2, "spool_id": 99},
    {"slot_id": 3, "spool_id": -2},
    {"slot_id": 4, "spool_id": 55},
]
MAPPED = [
    {"slot_id": 1, "spool_id": 3},
    {"slot_id": 3, "spool_id": -2},
    {"slot_id": 4, "spool_id": 5},
]


def _before(conn: Conn) -> None:
    index = next(i for i, m in enumerate(MIGRATIONS) if m.id.endswith(SLUG))
    migrate(conn, MIGRATIONS[:index])


def _one(conn: Conn, sql: str, *args: object) -> Any:
    row = conn.execute(sql, args).fetchone()
    assert row is not None
    return row[0]


def _spool(conn: Conn, number: int) -> Any:
    return _one(
        conn,
        "INSERT INTO filament_spools (number, material_id) OVERRIDING SYSTEM VALUE"
        " SELECT %s, id FROM filament_materials WHERE name = 'PLA' RETURNING id",
        number,
    )


def _record_link(conn: Conn, record_id: str, physical: Any) -> None:
    conn.execute("INSERT INTO physical_spools (id) VALUES (%s) ON CONFLICT DO NOTHING", (physical,))
    conn.execute(
        "INSERT INTO spool_record_links (provider, record_id, physical_spool_id, linked_by)"
        " VALUES ('bambuddy', %s, %s, 'rfid')",
        (record_id, physical),
    )


def _spools(conn: Conn) -> dict[int, Any]:
    """SB #3 linked to BB 41, #5 (record link 55), #99 (no link), and the record links
    the carry must skip."""
    by_number = {n: _spool(conn, n) for n in (3, 5, 7, 8, 9, 99)}
    conn.execute(
        "INSERT INTO filament_bb_links (spool_id, bb_spool_id) VALUES (%s, 41)", (by_number[3],)
    )
    _record_link(conn, "55", by_number[5])
    # BB 41 is already linked to SB #3: this record link to SB #7 is skipped.
    _record_link(conn, "41", by_number[7])
    # SB #3 already has a link: its record link to BB 77 is skipped.
    _record_link(conn, "77", by_number[3])
    # Not a number, and too big for an integer: skipped, not errors.
    _record_link(conn, "abc", by_number[8])
    _record_link(conn, "99999999999", by_number[9])
    # A physical spool that is no SB spool.
    _record_link(conn, "66", "00000000-0000-0000-0000-000000000066")
    return by_number


def _order_item(conn: Conn) -> str:
    customer = _one(conn, "INSERT INTO customers (display_name) VALUES ('C') RETURNING id")
    order = _one(
        conn,
        "INSERT INTO orders (number, customer_id, currency, created_by)"
        " VALUES ('O-1', %s, 'USD', '{}') RETURNING id",
        customer,
    )
    return str(
        _one(
            conn,
            "INSERT INTO order_items (order_id, position, quantity, title, kind)"
            " VALUES (%s, 0, 1, 'T', 'custom') RETURNING id",
            order,
        )
    )


def _quote(conn: Conn, item: str, plan: Any) -> str:
    return str(
        _one(
            conn,
            "INSERT INTO order_item_quotes (order_item_id, source, unit_cost_minor,"
            " unit_price_minor, quantity, total_minor, created_by, filament_plan)"
            " VALUES (%s, 'manual', 1, 1, 1, 1, '{}', %s) RETURNING id",
            item,
            plan,
        )
    )


def _operation(conn: Conn, op_id: str, status: str, request: Any) -> None:
    conn.execute(
        "INSERT INTO operations (id, kind, subject, operation_key, status, request,"
        " workflow_id, workflow_run_id) VALUES (%s, 'print', 's', %s, %s, %s, %s, %s)",
        (op_id, op_id, status, Jsonb(request), op_id, op_id),
    )


def test_stored_bambuddy_ids_become_sb_numbers(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        spools = _spools(conn)

        conn.execute(
            "INSERT INTO model_print_choices (model_id, choices) VALUES"
            " ('m1', %s), ('m2', %s), ('m3', %s), ('m4', %s)",
            (
                Jsonb({"filament_plan": PLAN, "tier": "fine"}),
                Jsonb({"tier": "fine"}),
                Jsonb({"filament_plan": "x"}),
                Jsonb(
                    {
                        "filament_plan": [
                            {"slot_id": 1, "spool_id": "41"},
                            {"slot_id": 2, "spool_id": "abc"},
                            7,
                        ]
                    }
                ),
            ),
        )

        item = _order_item(conn)
        quote = _quote(conn, item, Jsonb({"slots": PLAN, "force_colour_match": False}))
        quote_bb = _quote(conn, item, Jsonb({"slots": PLAN, "ids": "bb"}))
        sb_plan = {"slots": [{"slot_id": 1, "spool_id": 41}], "ids": "sb"}
        quote_sb = _quote(conn, item, Jsonb(sb_plan))
        quote_none = _quote(conn, item, None)
        quote_null = _quote(conn, item, Jsonb(None))
        quote_no_slots = _quote(conn, item, Jsonb({"force_colour_match": True}))

        conn.execute("INSERT INTO print_batches (id) VALUES ('b1')")
        request = {"plate_id": 1, "filament_plan": {"slots": PLAN}}
        conn.execute(
            "INSERT INTO print_batch_items (batch_id, position, library_file_id, filename,"
            " request, refused_request, outcome) VALUES"
            " ('b1', 0, 1, 'f', %s, %s, '{\"status\": \"refused\"}'),"
            " ('b1', 1, 2, 'g', NULL, NULL, NULL),"
            " ('b1', 2, 3, 'h', %s, %s, '{\"status\": \"pending\"}'),"
            " ('b1', 3, 4, 'i', %s, %s, '{\"status\": \"started\"}')",
            (Jsonb(request),) * 6,
        )
        # A file whose run may have started keeps its request byte for byte: Queue all
        # resends it, and its run key hashes it (Task 7 maps its unmarked plan at read).
        in_flight = conn.execute(
            "SELECT position, request::text, refused_request::text FROM print_batch_items"
            " WHERE position >= 2 ORDER BY position"
        ).fetchall()

        _operation(conn, "op-run", "running", request)
        _operation(conn, "op-preview", "running", {"request": request, "plate": 1})
        _operation(conn, "op-done", "succeeded", request)
        _operation(conn, "op-other", "running", {"slug": "s"})

        conn.execute(
            "INSERT INTO settings (name, value) VALUES ('storefront_slice_profile', %s)",
            (Jsonb({"printer": "p", "spools": {"pla": 41, "petg": 99, "abs": -2, "tpu": 55}}),),
        )

        conn.execute(
            "INSERT INTO filaments (material, colour, cost_per_kg, spool_ids)"
            " VALUES ('PLA', '#FFFFFF', 20, '{41,99}'), ('PETG', '#000000', 20, '{}')"
        )

        conn.execute(
            "INSERT INTO rack_nozzle_pick_spools (queue_item_id, group_id, slot_id, spool_id,"
            " label) VALUES (1, 1, 1, 99, 'Bambuddy PLA'), (1, 1, 2, 41, 'B'),"
            " (1, 1, 3, NULL, 'untracked'), (1, 1, 4, -2, 'tray')"
        )

        migrate(conn)

        # The record-link carry (F15, F30): one row, BB 55 to SB #5; the rest skipped.
        links = conn.execute(
            "SELECT s.number, l.bb_spool_id, l.bb_updated_at FROM filament_bb_links l"
            " JOIN filament_spools s ON s.id = l.spool_id ORDER BY s.number"
        ).fetchall()
        assert links == [(3, 41, None), (5, 55, None)]
        assert spools[99] is not None

        choices: dict[str, Any] = dict(
            conn.execute("SELECT model_id, choices FROM model_print_choices").fetchall()
        )
        assert choices["m1"] == {"filament_plan": MAPPED, "tier": "fine"}
        assert choices["m2"] == {"tier": "fine"}
        assert choices["m3"] == {"filament_plan": "x"}
        assert choices["m4"] == {
            "filament_plan": [{"slot_id": 1, "spool_id": 3}, {"slot_id": 2, "spool_id": "abc"}, 7]
        }

        def plan_of(quote_id: str) -> Any:
            return _one(conn, "SELECT filament_plan FROM order_item_quotes WHERE id = %s", quote_id)

        assert plan_of(quote) == {"slots": MAPPED, "force_colour_match": False, "ids": "sb"}
        assert plan_of(quote_bb) == {"slots": MAPPED, "ids": "sb"}
        assert plan_of(quote_sb) == sb_plan
        assert plan_of(quote_none) is None
        assert plan_of(quote_null) is None
        assert plan_of(quote_no_slots) == {"force_colour_match": True}

        rewritten = {"plate_id": 1, "filament_plan": {"slots": MAPPED, "ids": "sb"}}
        rows = conn.execute(
            "SELECT request, refused_request FROM print_batch_items WHERE position < 2"
            " ORDER BY position"
        ).fetchall()
        assert rows == [(rewritten, rewritten), (None, None)]
        assert (
            conn.execute(
                "SELECT position, request::text, refused_request::text FROM print_batch_items"
                " WHERE position >= 2 ORDER BY position"
            ).fetchall()
            == in_flight
        )

        ops: dict[str, Any] = dict(conn.execute("SELECT id, request FROM operations").fetchall())
        assert ops["op-run"] == rewritten
        assert ops["op-preview"] == {"request": rewritten, "plate": 1}
        assert ops["op-done"] == request
        assert ops["op-other"] == {"slug": "s"}

        assert _one(conn, "SELECT value FROM settings WHERE name = 'storefront_slice_profile'") == {
            "printer": "p",
            "spools": {"pla": 3, "abs": -2, "tpu": 5},
        }

        assert conn.execute(
            "SELECT material, spool_ids FROM filaments ORDER BY material"
        ).fetchall() == [("PETG", []), ("PLA", [3])]

        assert conn.execute(
            "SELECT slot_id, spool_id, label FROM rack_nozzle_pick_spools ORDER BY slot_id"
        ).fetchall() == [
            (1, None, "Bambuddy PLA"),
            (2, 3, "B"),
            (3, None, "untracked"),
            (4, -2, "tray"),
        ]

        # A second run (the file's SQL again) does not fail, and a marked plan stays as it
        # is. The unmarked stores (remembered choices, `spool_ids`, rack picks, the slice
        # profile) are not idempotent: they rely on the migration ledger to run once.
        sql = next(m.sql for m in MIGRATIONS if m.id.endswith(SLUG))
        conn.execute(sql.encode())
        assert plan_of(quote) == {"slots": MAPPED, "force_colour_match": False, "ids": "sb"}


def test_with_nothing_stored_it_is_a_no_op(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        _before(conn)
        conn.execute(
            "INSERT INTO settings (name, value) VALUES ('storefront_slice_profile', 'null')"
        )
        migrate(conn)
        assert (
            _one(conn, "SELECT value FROM settings WHERE name = 'storefront_slice_profile'") is None
        )
        assert _one(conn, "SELECT count(*) FROM filament_bb_links") == 0
