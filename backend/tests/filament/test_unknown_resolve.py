"""#7143 Task 3B: the activity `filament_resolve_unknown_tray` links a spool to an unknown
tray, books its first weight and clears the row, in one transaction."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.filament import ledger
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.models import SpoolCreate
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.filament.unknown_resolve import (
    RESOLVE_UNKNOWN_TRAY_ACTIVITY,
    ResolveInput,
    ResolveOutput,
    resolve_unknown_tray,
)
from scadbuddy.filament.unknown_tray_rows import note_on, rows_on
from scadbuddy.main import _unknown_tray_activities
from scadbuddy.spools.models import SeenTray
from tests.conftest import PgPool
from tests.filament.test_tray_reconcile import _material

pytestmark = pytest.mark.requires_postgres

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _filament(pool: PgPool) -> Any:
    return SimpleNamespace(inventory=FilamentInventory(pool), spools=FilamentSpoolStore(pool))


def _printer(pool: PgPool) -> UUID:
    printer = uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES (%s, %s, 'Garage')",
            (printer, f"SN{printer.hex[:8]}"),
        )
    return printer


def _spool(pool: PgPool, label: int | None = 1000) -> tuple[UUID, int]:
    body = SpoolCreate(material_id=_material(pool, "PLA"), rgba="#FF0000FF", label_weight_g=label)
    made = FilamentSpoolStore(pool).create(body, actor="ann")
    return made.id, made.number


def _tray(pool: PgPool, printer: UUID, tray_id: int = 1, **keys: Any) -> UUID:
    seen = SeenTray(
        ams_id=0, tray_id=tray_id, material="PLA", colour="#FF0000FF", rgba="#FF0000FF", **keys
    )
    with pool.connection() as conn:
        return note_on(conn, printer, seen, T0).id


def _body(row: UUID, **fields: Any) -> ResolveInput:
    base: dict[str, Any] = {"tray_id": row, "net_weight_g": 980, "seen_at": T0, "actor": "ann"}
    return ResolveInput.model_validate(base | fields)


def _events(pool: PgPool, spool: UUID) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT kind, grams_after, ref_kind, source, actor FROM filament_spool_events"
            " WHERE spool_id = %s ORDER BY at, seq",
            (spool,),
        ).fetchall()
    return [dict(r) for r in rows]


def _label(pool: PgPool, spool: UUID) -> int | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT label_weight_g FROM filament_spools WHERE id = %s", (spool,)
        ).fetchone()
    assert row is not None
    label: int | None = row["label_weight_g"]
    return label


def _problem(pool: PgPool, body: ResolveInput) -> ApplicationError:
    with pytest.raises(ApplicationError) as caught:
        resolve_unknown_tray(_filament(pool), pool, body)
    assert caught.value.non_retryable
    return caught.value


def test_resolve_by_code_links_books_and_clears(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    spool, number = _spool(pg_pool)
    row = _tray(pg_pool, printer)

    out = resolve_unknown_tray(_filament(pg_pool), pg_pool, _body(row, code=f"s-{number}"))

    assert out.id == spool and out.number == number and out.code == f"S-{number}"
    with pg_pool.connection() as conn:
        place = conn.execute(
            "SELECT id FROM filament_locations WHERE printer_id = %s AND ams_id = 0"
            " AND tray_id = 1",
            (printer,),
        ).fetchone()
        assert rows_on(conn, printer) == []
    assert place is not None and out.location_id == place["id"]
    weighed = [e for e in _events(pg_pool, spool) if e["kind"] == "weighed"]
    assert len(weighed) == 1
    assert weighed[0]["grams_after"] == 980 and weighed[0]["ref_kind"] == "unknown_tray"
    assert weighed[0]["source"] == "person" and weighed[0]["actor"] == "ann"


def test_a_first_weight_that_differs_sets_the_label_weight(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    spool, _ = _spool(pg_pool, 1000)
    row = _tray(pg_pool, printer)

    resolve_unknown_tray(_filament(pg_pool), pg_pool, _body(row, spool_id=spool, net_weight_g=1012))

    assert _label(pg_pool, spool) == 1012
    kinds = [e["kind"] for e in _events(pg_pool, spool)]
    assert "edited" in kinds and kinds.count("weighed") == 1


def test_a_used_spool_keeps_its_label_weight(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    spool, _ = _spool(pg_pool, 1000)
    with pg_pool.connection() as conn:
        ledger.record(conn, spool, "weighed", source="scale", absolute_g=900, actor="ann")
    row = _tray(pg_pool, printer)

    resolve_unknown_tray(_filament(pg_pool), pg_pool, _body(row, spool_id=spool, net_weight_g=700))

    assert _label(pg_pool, spool) == 1000
    weighed = [e["grams_after"] for e in _events(pg_pool, spool) if e["kind"] == "weighed"]
    assert weighed == [900, 700]


def test_a_stale_seen_at_is_tray_changed(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    spool, _ = _spool(pg_pool)
    row = _tray(pg_pool, printer)

    error = _problem(pg_pool, _body(row, spool_id=spool, seen_at=T0 - timedelta(seconds=1)))

    assert error.type == "tray_changed"
    assert [e["kind"] for e in _events(pg_pool, spool)] == ["created"]
    with pg_pool.connection() as conn:
        assert len(rows_on(conn, printer)) == 1


async def test_two_resolves_of_one_tray_one_wins(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    first, _ = _spool(pg_pool)
    second, _ = _spool(pg_pool)
    row = _tray(pg_pool, printer)
    filament = _filament(pg_pool)

    results = await asyncio.gather(
        asyncio.to_thread(resolve_unknown_tray, filament, pg_pool, _body(row, spool_id=first)),
        asyncio.to_thread(resolve_unknown_tray, filament, pg_pool, _body(row, spool_id=second)),
        return_exceptions=True,
    )

    wins = [r for r in results if isinstance(r, ResolveOutput)]
    losses = [r for r in results if isinstance(r, ApplicationError)]
    assert len(wins) == 1 and len(losses) == 1 and losses[0].type == "tray_changed"
    weighed = [e for s in (first, second) for e in _events(pg_pool, s) if e["kind"] == "weighed"]
    assert len(weighed) == 1


def test_an_archived_spool_is_refused(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    spool, _ = _spool(pg_pool)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET archived_at = now() WHERE id = %s", (spool,))
    row = _tray(pg_pool, printer)

    assert _problem(pg_pool, _body(row, spool_id=spool)).type == "spool_archived"

    with pg_pool.connection() as conn:
        assert len(rows_on(conn, printer)) == 1


def test_a_place_code_is_code_unknown(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    row = _tray(pg_pool, printer)
    assert _problem(pg_pool, _body(row, code="L-1")).type == "code_unknown"
    assert _problem(pg_pool, _body(row, code="S-999999")).type == "code_unknown"


def test_an_unknown_spool_id_is_spool_missing(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    row = _tray(pg_pool, printer)
    assert _problem(pg_pool, _body(row, spool_id=uuid4())).type == "spool_missing"


def test_no_ams_kind_is_refused(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    spool, _ = _spool(pg_pool)
    row = _tray(pg_pool, printer)
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_location_kinds SET archived_at = now() WHERE ams")

    assert _problem(pg_pool, _body(row, spool_id=spool)).type == "no_ams_kind"

    with pg_pool.connection() as conn:
        assert len(rows_on(conn, printer)) == 1


def test_a_tag_on_another_spool_is_tag_clash(pg_pool: PgPool) -> None:
    printer = _printer(pg_pool)
    holder, _ = _spool(pg_pool)
    other, _ = _spool(pg_pool)
    uid = "AABBCCDD00000100"
    first = _tray(pg_pool, printer, 1, tag_uid=uid)
    resolve_unknown_tray(_filament(pg_pool), pg_pool, _body(first, spool_id=holder))
    second = _tray(pg_pool, printer, 2, tag_uid=uid)

    error = _problem(pg_pool, _body(second, spool_id=other))

    assert error.type == "tag_clash" and "tag" in error.message


def test_the_library_worker_serves_the_activity() -> None:
    state = cast(Any, SimpleNamespace())
    names = [
        activity._Definition.must_from_callable(fn).name for fn in _unknown_tray_activities(state)
    ]
    assert names == [RESOLVE_UNKNOWN_TRAY_ACTIVITY]
