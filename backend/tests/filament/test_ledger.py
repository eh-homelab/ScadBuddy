"""The spool ledger and the grams-left rule (#6261, spec §5.5)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from scadbuddy.core.authorship import AgentAuthor, authored_as
from scadbuddy.filament import ledger
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.ledger import grams_left, job_of, record
from scadbuddy.filament.models import Material, SpoolEvent
from tests.conftest import PgPool

pg = pytest.mark.requires_postgres

T0 = datetime(2026, 11, 14, 12, tzinfo=UTC)
SPOOL = UUID(int=1)


_SEQ = itertools.count(1)


def _event(minute: int, kind: str, source: str | None, **over: Any) -> SpoolEvent:
    values: dict[str, Any] = {
        "id": uuid4(),
        "seq": next(_SEQ),
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


def test_events_of_one_time_are_taken_in_seq_order() -> None:
    later = _event(1, "weighed", "scale", grams_after=500, seq=2)
    earlier = _event(1, "weighed", "scale", grams_after=800, seq=1)
    assert grams_left([later, earlier], weight_locked=False, label_weight_g=1000) == 500


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


def _left(events: list[SpoolEvent]) -> float | None:
    return grams_left(events, weight_locked=False, label_weight_g=1000)


def _sb(minute: int, kind: str, delta: float, ref: str = "r/J/0", **over: Any) -> SpoolEvent:
    source = "print_estimate" if kind == "print_started" else "print_actual"
    return _event(
        minute, kind, source, delta_g=delta, ref_kind=ledger.PRINT_RUN, ref_id=ref, **over
    )


def _bb(minute: int, delta: float, job: str = "J") -> SpoolEvent:
    return _event(
        minute,
        "print_ended",
        "print_actual",
        delta_g=delta,
        ref_kind=ledger.BB_USAGE,
        ref_id=f"usage-{job}-{minute}",
        data={"job": job},
    )


def test_a_reading_at_a_plate_end_holds_that_plate() -> None:
    reading = _event(1, "ams_reading", "ams_remain", data={"remain_pct": 70})
    events = [_event(0, "weighed", "scale", grams_after=800), reading, _sb(1, "print_ended", -50)]
    assert _left(events) == 700


def test_an_ams_reading_while_printing_is_no_anchor() -> None:
    base = [_event(0, "weighed", "scale", grams_after=800), _sb(1, "print_started", -40)]
    printing = _event(2, "ams_reading", "ams_remain", data={"remain_pct": 74, "printing": True})
    assert _left([*base, printing]) == 760
    idle = _event(2, "ams_reading", "ams_remain", data={"remain_pct": 74})
    assert _left([*base, idle]) == 740


def test_bb_usage_and_sb_end_of_one_plate_count_once() -> None:
    events = [
        _event(0, "weighed", "scale", grams_after=800),
        _sb(1, "print_started", -40),
        _bb(2, -41),
        _sb(3, "print_ended", -38),
    ]
    assert _left(events) == 759


def _bb_reading(minute: int) -> SpoolEvent:
    return _event(minute, "synced_from_bb", "bb", grams_after=500, ref_kind="bb_spool")


def test_a_bb_reading_holds_its_own_usage_row() -> None:
    ahead = [_sb(5, "print_ended", -38), _bb(10, -41), _bb_reading(10)]
    assert _left(ahead) == 500
    behind = [_sb(2, "print_ended", -38), _bb(2, -41), _bb_reading(10)]
    assert _left(behind) == 500


def test_a_bb_reading_given_before_its_usage_row_holds_it() -> None:
    events = [_sb(5, "print_ended", -38), _bb_reading(10), _bb(10, -41)]
    assert _left(events) == 500


def test_a_bb_reading_holds_a_pair_whose_sb_end_is_later() -> None:
    events = [_bb(2, -41), _bb_reading(2), _sb(5, "print_ended", -38)]
    assert _left(events) == 500


def test_an_ams_reading_after_the_sb_end_holds_a_pair_whose_bb_time_is_later() -> None:
    events = [
        _sb(5, "print_ended", -38),
        _event(6, "ams_reading", "ams_remain", data={"remain_pct": 50}),
        _bb(9, -41),
    ]
    assert _left(events) == 500


def test_a_failed_plate_takes_bbs_partial_grams() -> None:
    events = [
        _event(0, "weighed", "scale", grams_after=600),
        _sb(1, "print_ended", 0, data={"basis": "not_applied", "job": "J"}),
        _bb(2, -23.4),
    ]
    assert _left(events) == pytest.approx(576.6)


def test_a_failed_plate_counts_bbs_grams_by_sbs_end() -> None:
    events = [
        _sb(0, "print_started", -40),
        _bb(1, -23.4),
        _event(2, "ams_reading", "ams_remain", data={"remain_pct": 60}),
        _sb(3, "print_ended", 0, data={"basis": "not_applied", "job": "J"}),
    ]
    assert _left(events) == pytest.approx(576.6)


def test_a_non_string_job_falls_back_to_the_ref_id() -> None:
    assert job_of(_sb(1, "print_ended", -38, ref="run1/J/2", data={"job": None})) == "J"


def test_one_spool_in_two_slots_is_one_bb_row() -> None:
    events = [
        _event(0, "weighed", "scale", grams_after=300),
        _sb(1, "print_ended", -10, ref="r/J/0"),
        _sb(1, "print_ended", -15, ref="r/J/1"),
        _bb(2, -26),
    ]
    assert _left(events) == 274


def test_a_job_from_an_old_ref_id_pairs() -> None:
    old = _sb(1, "print_ended", -38, ref="run1/J/2")
    assert "job" not in old.data
    assert job_of(old) == "J"
    assert job_of(_bb(2, -41)) == "J"
    assert job_of(_event(1, "weighed", "scale", ref_kind="bb_scale", ref_id="a/b/c")) is None
    events = [_event(0, "weighed", "scale", grams_after=800), old, _bb(2, -41)]
    assert _left(events) == 759


def _spool(pool: PgPool, label_weight_g: int | None = None) -> UUID:
    material = next(r.id for r in CatalogStore(pool).list("materials") if isinstance(r, Material))
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_spools (material_id, label_weight_g) VALUES (%s, %s)"
            " RETURNING id",
            (material, label_weight_g),
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


_DERIVED = (
    "grams_left, weight_locked, last_weighed_g, last_weighed_at, last_dried_at,"
    " last_dried_temp_c, last_dried_hours, last_used_at"
)


def _derived(conn: ledger.Conn, spool: UUID) -> dict[str, Any]:
    row = conn.execute(f"SELECT {_DERIVED} FROM filament_spools WHERE id = %s", (spool,)).fetchone()
    assert row is not None
    return dict(row)


def _counting(monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    """Every read of a spool's whole history, by spool."""
    reads: list[UUID] = []
    real = ledger.events

    def counted(conn: ledger.Conn, spool_id: UUID) -> list[SpoolEvent]:
        reads.append(spool_id)
        return real(conn, spool_id)

    monkeypatch.setattr(ledger, "events", counted)
    return reads


@pg
def test_an_ams_reading_on_top_does_not_read_the_history(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = _spool(pg_pool, label_weight_g=1000)
    with pg_pool.connection() as conn:
        record(conn, spool, "weighed", source="scale", absolute_g=900, at=T0)
        reads = _counting(monkeypatch)
        record(
            conn,
            spool,
            "ams_reading",
            source="ams_remain",
            at=T0 + timedelta(minutes=1),
            data={"remain_pct": 60},
        )
        assert reads == []
        assert _derived(conn, spool)["grams_left"] == 600
        assert _derived(conn, spool)["last_weighed_g"] == 900


@pg
def test_an_event_with_an_older_time_takes_the_full_path(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = _spool(pg_pool, label_weight_g=1000)
    with pg_pool.connection() as conn:
        record(conn, spool, "weighed", source="scale", absolute_g=900, at=T0)
        reads = _counting(monkeypatch)
        record(
            conn,
            spool,
            "synced_from_bb",
            source="bb",
            absolute_g=700,
            ref_kind="bb_spool",
            ref_id="1@old",
            at=T0 - timedelta(minutes=1),
        )
        assert reads == [spool]
        assert _derived(conn, spool)["grams_left"] == 900


def _book(conn: ledger.Conn, spool: UUID, event: SpoolEvent) -> None:
    record(
        conn,
        spool,
        event.kind,
        source=event.source,
        delta_g=event.delta_g,
        absolute_g=event.grams_after,
        ref_kind=event.ref_kind,
        ref_id=event.ref_id,
        at=event.at,
        data=event.data,
    )


def _assert_full_path_agrees(conn: ledger.Conn, spool: UUID) -> None:
    """The columns the booking left are what the full walk and a full refresh give."""
    booked = _derived(conn, spool)
    label = conn.execute(
        "SELECT label_weight_g FROM filament_spools WHERE id = %s", (spool,)
    ).fetchone()
    assert label is not None
    walked = grams_left(
        ledger.events(conn, spool),
        weight_locked=booked["weight_locked"],
        label_weight_g=label["label_weight_g"],
    )
    ledger.refresh_derived(conn, spool)
    assert booked == _derived(conn, spool)
    assert booked["grams_left"] == walked


@pg
def test_the_fast_path_matches_a_full_recompute(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = _spool(pg_pool, label_weight_g=1000)
    newest = 9
    steps: list[SpoolEvent | str] = [
        _event(0, "weighed", "scale", grams_after=900),
        _event(1, "ams_reading", "ams_remain", data={"remain_pct": 85}),
        _sb(2, "print_started", -40),
        _event(3, "ams_reading", "ams_remain", data={"remain_pct": 80, "printing": True}),
        _sb(4, "print_ended", -38),
        _event(5, "ams_reading", "ams_remain", data={"remain_pct": 79}),
        _event(6, "loaded", None),
        _event(7, "weighed", "scale", ref_kind="bb_scale", ref_id="w1"),
        _event(newest, "dried", None, data={"temp_c": 55, "hours": 6}),
        _event(newest - 1, "weighed", "scale", grams_after=760),
        "lock",
        _event(newest + 1, "ams_reading", "ams_remain", data={"remain_pct": 75}),
    ]
    reads = _counting(monkeypatch)
    fast = 0
    with pg_pool.connection() as conn:
        for step in steps:
            reads.clear()
            if step == "lock":
                conn.execute(
                    "UPDATE filament_spools SET weight_locked = true WHERE id = %s", (spool,)
                )
                changes = {"weight_locked": [False, True]}
                at = T0 + timedelta(minutes=newest, seconds=30)
                record(conn, spool, "edited", at=at, data={"changes": changes})
            else:
                assert isinstance(step, SpoolEvent)
                _book(conn, spool, step)
            fast += not reads
            _assert_full_path_agrees(conn, spool)
            if step is steps[7]:
                assert _derived(conn, spool)["last_weighed_g"] == 900
        final = _derived(conn, spool)
    # The weighing with no reading keeps the scale's 900 g as the last weighing until
    # the 760 g one, booked older than the drying, takes its place.
    assert final["last_weighed_g"] == 760
    assert final["last_dried_temp_c"] == 55
    # With the lock, the 75 % reading is no anchor: the 760 g weighing stays one.
    assert final["grams_left"] == 760
    assert fast >= 5


@pg
def test_a_lock_change_takes_the_full_path(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, label_weight_g=1000)
    with pg_pool.connection() as conn:
        record(conn, spool, "weighed", source="scale", absolute_g=800, at=T0)
        reading = {"remain_pct": 70}
        at = T0 + timedelta(minutes=1)
        record(conn, spool, "ams_reading", source="ams_remain", at=at, data=reading)
        assert _derived(conn, spool)["grams_left"] == 700
        conn.execute("UPDATE filament_spools SET weight_locked = true WHERE id = %s", (spool,))
        changes = {"weight_locked": [False, True]}
        at = T0 + timedelta(minutes=2)
        record(conn, spool, "edited", at=at, data={"changes": changes})
        assert _derived(conn, spool)["grams_left"] == 800
        _assert_full_path_agrees(conn, spool)


_RULE_CASES: dict[str, list[SpoolEvent]] = {
    "anchors_then_subtracts": [
        _event(1, "weighed", "scale", grams_after=800),
        _event(2, "print_ended", "print_actual", delta_g=-50, ref_kind="print_run", ref_id="r"),
        _event(3, "ams_reading", "ams_remain", grams_after=700),
    ],
    "estimate_replaced": [
        _event(1, "weighed", "scale", grams_after=500),
        _sb(2, "print_started", -40, ref="r1"),
        _sb(3, "print_ended", -35, ref="r1"),
    ],
    "ams_older_than_weighing": [
        _event(1, "ams_reading", "ams_remain", data={"remain_pct": 90}),
        _event(2, "weighed", "person", grams_after=600),
    ],
    "reading_at_plate_end": [
        _event(0, "weighed", "scale", grams_after=800),
        _event(1, "ams_reading", "ams_remain", data={"remain_pct": 70}),
        _sb(1, "print_ended", -50),
    ],
    "plate_end_then_reading": [
        _event(0, "weighed", "scale", grams_after=800),
        _sb(1, "print_ended", -50),
        _event(1, "ams_reading", "ams_remain", data={"remain_pct": 70}),
    ],
    "printing_reading": [
        _event(0, "weighed", "scale", grams_after=800),
        _sb(1, "print_started", -40),
        _event(2, "ams_reading", "ams_remain", data={"remain_pct": 74, "printing": True}),
        _event(3, "ams_reading", "ams_remain", data={"remain_pct": 74}),
    ],
    "bb_and_sb_once": [
        _event(0, "weighed", "scale", grams_after=800),
        _sb(1, "print_started", -40),
        _bb(2, -41),
        _sb(3, "print_ended", -38),
    ],
    "bb_reading_holds_usage_ahead": [_sb(5, "print_ended", -38), _bb(10, -41), _bb_reading(10)],
    "bb_reading_holds_usage_behind": [_sb(2, "print_ended", -38), _bb(2, -41), _bb_reading(10)],
    "bb_reading_before_usage": [_sb(5, "print_ended", -38), _bb_reading(10), _bb(10, -41)],
    "bb_reading_sb_end_later": [_bb(2, -41), _bb_reading(2), _sb(5, "print_ended", -38)],
    "ams_after_sb_end_bb_later": [
        _sb(5, "print_ended", -38),
        _event(6, "ams_reading", "ams_remain", data={"remain_pct": 50}),
        _bb(9, -41),
    ],
    "failed_plate": [
        _event(0, "weighed", "scale", grams_after=600),
        _sb(1, "print_ended", 0, data={"basis": "not_applied", "job": "J"}),
        _bb(2, -23.4),
    ],
    "failed_plate_by_sb_end": [
        _sb(0, "print_started", -40),
        _bb(1, -23.4),
        _event(2, "ams_reading", "ams_remain", data={"remain_pct": 60}),
        _sb(3, "print_ended", 0, data={"basis": "not_applied", "job": "J"}),
    ],
    "two_slots": [
        _event(0, "weighed", "scale", grams_after=300),
        _sb(1, "print_ended", -10, ref="r/J/0"),
        _sb(1, "print_ended", -15, ref="r/J/1"),
        _bb(2, -26),
    ],
    "old_ref_id": [
        _event(0, "weighed", "scale", grams_after=800),
        _sb(1, "print_ended", -38, ref="run1/J/2"),
        _bb(2, -41),
    ],
}


@pg
@pytest.mark.parametrize("case", list(_RULE_CASES))
def test_the_fast_path_matches_the_full_walk_on_the_rule_cases(
    pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Booked one by one, each event leaves what the full walk gives, also when it
    sits on top of the history (the fast path)."""
    spool = _spool(pg_pool, label_weight_g=1000)
    reads = _counting(monkeypatch)
    fast: set[int] = set()
    with pg_pool.connection() as conn:
        for i, event in enumerate(_RULE_CASES[case]):
            reads.clear()
            _book(conn, spool, event)
            if not reads:
                fast.add(i)
            _assert_full_path_agrees(conn, spool)
    # The cases built to land on top take the fast path there.
    assert _ON_TOP.get(case, set()) <= fast


#: The bookings of a rule case that land on top of its history (by index).
_ON_TOP: dict[str, set[int]] = {
    "plate_end_then_reading": {2},
    "printing_reading": {2, 3},
    "bb_reading_holds_usage_ahead": {2},
    "anchors_then_subtracts": {2},
    "ams_older_than_weighing": {1},
}


@pg
def test_a_negative_reading_on_top_leaves_zero(pg_pool: PgPool) -> None:
    """`record` takes any float (only the API's `Grams` is ``ge=0``); the fast path
    floors a negative reading at zero, as the walk does."""
    spool = _spool(pg_pool, label_weight_g=1000)
    with pg_pool.connection() as conn:
        record(conn, spool, "weighed", source="scale", absolute_g=500, at=T0)
        at = T0 + timedelta(minutes=1)
        record(conn, spool, "weighed", source="person", absolute_g=-5, at=at)
        assert _derived(conn, spool)["grams_left"] == 0
        _assert_full_path_agrees(conn, spool)


@pg
def test_record_adds_the_agent_author(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool)
    author = AgentAuthor(principal="user:a", session="s1", tool="filament_spool_upsert")
    with authored_as(author), pg_pool.connection() as conn:
        event = record(conn, spool, "weighed", source="person", absolute_g=700)
    assert event is not None
    assert event.data["author"] == {
        "principal": "user:a",
        "session": "s1",
        "tool": "filament_spool_upsert",
    }


@pg
def test_record_without_an_author_adds_nothing(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool)
    with pg_pool.connection() as conn:
        event = record(conn, spool, "weighed", source="person", absolute_g=700)
    assert event is not None
    assert "author" not in event.data


def test_pairs_are_jobs_with_a_bb_usage_end_and_an_sb_plate_event() -> None:
    events = [
        _event(1, "print_started", "print_estimate", ref_kind="print_run", data={"job": "a"}),
        _event(2, "print_ended", "print_actual", ref_kind="bb_usage", data={"job": "a"}),
        _event(3, "print_started", "print_estimate", ref_kind="print_run", data={"job": "b"}),
        _event(4, "print_ended", "print_actual", ref_kind="bb_usage", data={"job": "c"}),
    ]
    assert ledger.pairs(events) == {"a"}


def test_is_anchor_takes_a_reading_but_not_a_locked_or_printing_ams_one() -> None:
    scale = _event(1, "weighed", "scale", grams_after=500)
    ams = _event(2, "ams_reading", "ams_remain", data={"remain_pct": 50})
    printing = _event(3, "ams_reading", "ams_remain", grams_after=400, data={"printing": True})
    plate = _event(4, "print_ended", "print_actual", delta_g=-5)
    assert ledger.is_anchor(scale, weight_locked=True, label_weight_g=None)
    assert ledger.is_anchor(ams, weight_locked=False, label_weight_g=1000)
    assert not ledger.is_anchor(ams, weight_locked=False, label_weight_g=None)
    assert not ledger.is_anchor(ams, weight_locked=True, label_weight_g=1000)
    assert not ledger.is_anchor(printing, weight_locked=False, label_weight_g=1000)
    assert not ledger.is_anchor(plate, weight_locked=False, label_weight_g=1000)
