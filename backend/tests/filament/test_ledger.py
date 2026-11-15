"""The spool ledger and the grams-left rule (#6261, spec §5.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.ledger import grams_left, record
from scadbuddy.filament.models import Material, SpoolEvent
from tests.conftest import PgPool

pg = pytest.mark.requires_postgres

T0 = datetime(2026, 11, 14, 12, tzinfo=UTC)
SPOOL = UUID(int=1)


def _event(minute: int, kind: str, source: str | None, **over: Any) -> SpoolEvent:
    values: dict[str, Any] = {
        "id": uuid4(),
        "spool_id": SPOOL,
        "at": T0 + timedelta(minutes=minute),
        "kind": kind,
        "source": source,
        "printer_id": None,
        "ams_id": None,
        "tray_id": None,
        "grams_before": None,
        "grams_after": None,
        "delta_g": None,
        "ref_kind": None,
        "ref_id": None,
        "actor": None,
        "data": {},
    }
    return SpoolEvent.model_validate(values | over)


def test_grams_left_anchors_then_subtracts() -> None:
    events = [
        _event(1, "weighed", "scale", grams_after=800),
        _event(2, "print_ended", "print_actual", delta_g=-50, ref_kind="print_run", ref_id="r"),
    ]
    assert grams_left(events, weight_locked=False, label_weight_g=1000) == 750
    events.append(_event(3, "ams_reading", "ams_remain", grams_after=700))
    assert grams_left(events, weight_locked=False, label_weight_g=1000) == 700
    assert grams_left(events, weight_locked=True, label_weight_g=1000) == 750


def test_an_estimate_is_replaced_by_its_actual() -> None:
    run = {"ref_kind": "print_run", "ref_id": "r1"}
    started = [
        _event(1, "weighed", "scale", grams_after=500),
        _event(2, "print_started", "print_estimate", delta_g=-40, **run),
    ]
    assert grams_left(started, weight_locked=False, label_weight_g=None) == 460
    ended = [*started, _event(3, "print_ended", "print_actual", delta_g=-35, **run)]
    assert grams_left(ended, weight_locked=False, label_weight_g=None) == 465


def test_an_ams_reading_older_than_a_weighing_is_not_the_anchor() -> None:
    events = [
        _event(1, "ams_reading", "ams_remain", data={"remain_pct": 90}),
        _event(2, "weighed", "person", grams_after=600),
    ]
    assert grams_left(events, weight_locked=False, label_weight_g=1000) == 600
    assert grams_left(events[:1], weight_locked=False, label_weight_g=1000) == 900
    assert grams_left(events[:1], weight_locked=False, label_weight_g=None) is None


def test_no_reading_leaves_grams_left_unknown_and_never_below_zero() -> None:
    use = _event(1, "print_ended", "print_actual", delta_g=-5, ref_kind="print_run", ref_id="r")
    assert grams_left([use], weight_locked=False, label_weight_g=1000) is None
    events = [_event(0, "weighed", "scale", grams_after=3), use]
    assert grams_left(events, weight_locked=False, label_weight_g=1000) == 0


def _spool(pool: PgPool) -> UUID:
    material = next(r.id for r in CatalogStore(pool).list("materials") if isinstance(r, Material))
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_spools (material_id) VALUES (%s) RETURNING id", (material,)
        ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


@pg
def test_recording_the_same_print_twice_is_ignored(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool)
    with pg_pool.connection() as conn:
        record(conn, spool, "weighed", source="scale", absolute_g=900)
        args: dict[str, Any] = {"source": "print_actual", "delta_g": -20.0}
        first = record(conn, spool, "print_ended", ref_kind="print_run", ref_id="x", **args)
        again = record(conn, spool, "print_ended", ref_kind="print_run", ref_id="x", **args)
        assert first is not None
        assert again is None
        count = conn.execute(
            "SELECT count(*) AS n FROM filament_spool_events WHERE ref_id = 'x'"
        ).fetchone()
        derived = conn.execute(
            "SELECT grams_left, last_weighed_g, last_used_at FROM filament_spools WHERE id = %s",
            (spool,),
        ).fetchone()
    assert count is not None and count["n"] == 1
    assert derived is not None
    assert derived["grams_left"] == 880
    assert derived["last_weighed_g"] == 900
    assert derived["last_used_at"] == first.at
