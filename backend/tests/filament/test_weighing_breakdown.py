"""A weighing records its reading, empty weight, expected grams and drift (#7682)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament import ledger
from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.spools import (
    ContinuedFromNotArchivedError,
    FilamentDefaults,
    FilamentSpoolStore,
    NoBelowLabelError,
    NoEmptyWeightError,
    ReadingBelowEmptyError,
    SpoolCopySourceError,
)
from scadbuddy.filament.types import SpoolTypeStore
from tests.conftest import PgPool

pg = pytest.mark.requires_postgres

T0 = datetime(2026, 11, 20, 12, tzinfo=UTC)
H = timedelta(hours=1)


# -- the model -----------------------------------------------------------------------


def test_only_a_weighing_has_a_reading() -> None:
    with pytest.raises(ValueError, match="only a weighing has reading_g"):
        m.SpoolEventAdd(kind="dried", reading_g=1180, temp_c=50, hours=Decimal(4))


def test_a_reading_below_the_grams_is_refused() -> None:
    with pytest.raises(ValueError, match="reading_g is below grams"):
        m.SpoolEventAdd(kind="weighed", reading_g=900, grams=980)


def test_a_weighing_with_a_reading_alone_needs_no_grams() -> None:
    assert m.SpoolEventAdd(kind="weighed", reading_g=1180).grams is None
    with pytest.raises(ValueError, match="a weighing needs grams"):
        m.SpoolEventAdd(kind="weighed")


def test_a_resolution_takes_the_reading_alone() -> None:
    with pytest.raises(ValueError, match="a resolution takes reading_g alone"):
        m.SpoolEventAdd(kind="weighed", reading_g=900, grams=900, resolution="tared")


def test_a_resolution_needs_a_reading() -> None:
    with pytest.raises(ValueError, match="a resolution needs reading_g"):
        m.SpoolEventAdd(kind="weighed", grams=900, resolution="tared")


# -- the store -----------------------------------------------------------------------


def _material(pool: PgPool) -> UUID:
    rows = CatalogStore(pool).list("materials")
    return next(r.id for r in rows if isinstance(r, m.Material) and r.name == "PLA")


def _insert(pool: PgPool) -> UUID:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO filament_insert_types (name, empty_weight_g) VALUES ('Plastic', 200)"
            " RETURNING id"
        ).fetchone()
    assert row is not None
    made: UUID = row["id"]
    return made


def _spool(pool: PgPool, insert: UUID | None, **over: Any) -> UUID:
    body = {"material_id": _material(pool), "label_weight_g": 1000, "insert_type_id": insert} | over
    return FilamentSpoolStore(pool).create(m.SpoolCreate.model_validate(body), actor="ann").id


def _print(pool: PgPool, spool: UUID, grams: float, at: datetime, ref: str) -> None:
    with pool.connection() as conn:
        ledger.record(
            conn,
            spool,
            "print_ended",
            source="print_actual",
            delta_g=-grams,
            ref_kind="print_run",
            ref_id=ref,
            at=at,
        )


def _weighed_once(pool: PgPool, spool: UUID) -> None:
    """A scale weighing of 1000 g at T0, then a print of 8 g an hour later: 992 g left."""
    store = FilamentSpoolStore(pool)
    store.add_event(
        spool, m.SpoolEventAdd(kind="weighed", source="scale", grams=1000, at=T0), actor="t"
    )
    _print(pool, spool, 8, T0 + H, "r")


def _weigh(pool: PgPool, spool: UUID, **body: Any) -> m.SpoolEvent:
    event = FilamentSpoolStore(pool).add_event(
        spool, m.SpoolEventAdd(kind="weighed", source="scale", **body), actor="t"
    )
    assert event is not None
    return event


@pg
def test_a_weighing_with_a_reading_keeps_its_breakdown(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    event = _weigh(pg_pool, spool, reading_g=1180, grams=980)
    assert event.grams_after == 980
    assert event.data["reading_g"] == 1180 and event.data["empty_g"] == 200
    assert event.data["expected_g"] == 992 and event.data["drift_g"] == -12.0
    assert event.data["empty_source"] == "insert" and event.data["empty_name"] == "Plastic"
    assert event.data["empty_basis"] == "catalog"


@pg
def test_a_reading_alone_takes_the_insert_off(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    event = _weigh(pg_pool, spool, reading_g=1180)
    assert event.grams_after == 980 and event.data["empty_g"] == 200
    assert FilamentSpoolStore(pg_pool).get(spool).grams_left == 980  # type: ignore[union-attr]


@pg
def test_a_reading_alone_needs_an_empty_weight(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, None)
    with pytest.raises(NoEmptyWeightError):
        _weigh(pg_pool, spool, reading_g=1180)
    with pg_pool.connection() as conn:
        assert ledger.events(conn, spool)[-1].kind == "created"


@pg
def test_a_reading_below_the_empty_weight_is_refused(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    with pytest.raises(ReadingBelowEmptyError):
        _weigh(pg_pool, spool, reading_g=150)
    with pg_pool.connection() as conn:
        assert ledger.events(conn, spool)[-1].kind == "created"


@pg
def test_first_weighing_has_no_expected(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    event = _weigh(pg_pool, spool, reading_g=1180, grams=980)
    assert event.data["expected_g"] is None and event.data["drift_g"] is None
    assert event.data["empty_source"] == "insert"


@pg
def test_expected_is_the_grams_left_before_a_back_dated_weighing(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    _print(pg_pool, spool, 20, T0 + 3 * H, "s")
    event = _weigh(pg_pool, spool, reading_g=1180, grams=980, at=T0 + 2 * H)
    assert event.data["expected_g"] == 992 and event.data["drift_g"] == -12.0


@pg
def test_a_client_that_subtracted_its_own_empty_weight_is_recorded_as_person(
    pg_pool: PgPool,
) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    event = _weigh(pg_pool, spool, reading_g=1180, grams=962)
    assert event.data["empty_source"] == "person" and event.data["empty_g"] == 218
    assert event.data["empty_basis"] is None and event.data["empty_name"] is None


@pg
def test_a_weighing_without_a_reading_records_no_breakdown(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    event = _weigh(pg_pool, spool, grams=980, note="by hand")
    assert "reading_g" not in event.data and event.data == {"note": "by hand"}


@pg
def test_detail_carries_the_newest_weighing(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(pg_pool, _insert(pg_pool))
    before = store.detail(spool)
    assert before is not None and before.last_weighing is None
    _weigh(pg_pool, spool, grams=1000, at=T0)
    event = _weigh(pg_pool, spool, reading_g=1180, grams=980)
    detail = store.detail(spool)
    assert detail is not None and detail.last_weighing is not None
    assert detail.last_weighing.id == event.id


# -- a new spool under its label weight (#7683) --------------------------------------


def _new_spool(pool: PgPool, **over: Any) -> UUID:
    """A new spool by #7280's rule: 1000 g left of a 1000 g label, nothing consumed."""
    return _spool(pool, _insert(pool), grams_left=1000, added_full=True, **over)


def _changes(pool: PgPool, spool: UUID) -> list[tuple[str, Any]]:
    return [(e.kind, e.data.get("changes")) for e in FilamentSpoolStore(pool).events(spool)]


@pg
def test_tared_books_the_reading_as_filament(pg_pool: PgPool) -> None:
    spool = _new_spool(pg_pool)
    event = _weigh(pg_pool, spool, reading_g=900, resolution="tared")
    assert event.grams_after == 900
    assert event.data["resolution"] == "tared" and event.data["shortfall_g"] == 100
    assert event.data["empty_source"] == "tared" and event.data["empty_g"] == 0
    assert event.data["empty_basis"] is None and event.data["empty_name"] is None
    assert [k for k, _ in _changes(pg_pool, spool)] == ["created", "weighed"]


@pg
def test_a_resolved_weighing_has_no_drift(pg_pool: PgPool) -> None:
    """The person just explained the gap: what the ledger believed stays on the record,
    the drift does not, and the spool gets no Drift mark."""
    spool = _new_spool(pg_pool)
    event = _weigh(pg_pool, spool, reading_g=900, resolution="tared")
    assert event.data["expected_g"] == 1000 and event.data["drift_g"] is None
    assert _get(pg_pool, spool).last_drift_g is None


@pg
def test_short_marks_the_spool_not_added_full(pg_pool: PgPool) -> None:
    spool = _new_spool(pg_pool)
    event = _weigh(pg_pool, spool, reading_g=800, resolution="short")
    assert event.grams_after == 600
    assert event.data["resolution"] == "short" and event.data["shortfall_g"] == 200
    assert event.data["empty_source"] == "insert" and event.data["empty_g"] == 200
    changes = _changes(pg_pool, spool)
    assert changes[-2] == ("edited", {"added_full": [True, False]})
    assert changes[-1][0] == "weighed"
    assert FilamentSpoolStore(pg_pool).get(spool).added_full is False  # type: ignore[union-attr]


@pg
def test_short_on_a_spool_not_added_full_books_no_edit(pg_pool: PgPool) -> None:
    """New by the check's reading of it (nothing consumed, grams at the label) though not
    added full: `short` changes nothing on the row, so no `edited` event is booked."""
    spool = _spool(pg_pool, _insert(pg_pool), grams_left=1000, added_full=False)
    event = _weigh(pg_pool, spool, reading_g=800, resolution="short")
    assert event.grams_after == 600 and event.data["resolution"] == "short"
    assert [k for k, _ in _changes(pg_pool, spool)] == ["created", "weighed"]
    assert FilamentSpoolStore(pg_pool).get(spool).added_full is False  # type: ignore[union-attr]


@pg
def test_label_wrong_sets_the_spools_own_label(pg_pool: PgPool) -> None:
    kind = SpoolTypeStore(pg_pool).create(
        m.SpoolTypeCreate(
            code="PLA-LBL", material_id=_material(pg_pool), rgba="#FF0000", label_weight_g=1000
        )
    )
    spool = (
        FilamentSpoolStore(pg_pool)
        .create(
            m.SpoolCreate(
                type_id=kind.id, insert_type_id=_insert(pg_pool), grams_left=1000, added_full=True
            ),
            actor="ann",
        )
        .id
    )
    event = _weigh(pg_pool, spool, reading_g=900, resolution="label_wrong")
    assert event.grams_after == 700 and event.data["resolution"] == "label_wrong"
    row = FilamentSpoolStore(pg_pool).get(spool)
    assert row is not None
    assert row.label_weight_g == 700 and "label_weight_g" in row.overridden
    assert _changes(pg_pool, spool)[-2] == ("edited", {"label_weight_g": [None, 700]})
    assert SpoolTypeStore(pg_pool).get(kind.id).label_weight_g == 1000  # type: ignore[union-attr]


@pg
def test_label_wrong_from_the_types_label_records_a_null_before(pg_pool: PgPool) -> None:
    """The spool had no label of its own: the `edited` event's `before` is null, the type's
    label stands, and the spool now overrides it."""
    kind = SpoolTypeStore(pg_pool).create(
        m.SpoolTypeCreate(
            code="PLA-TYPED", material_id=_material(pg_pool), rgba="#00FF00", label_weight_g=1000
        )
    )
    store = FilamentSpoolStore(pg_pool)
    spool = store.create(
        m.SpoolCreate(
            type_id=kind.id, insert_type_id=_insert(pg_pool), grams_left=1000, added_full=True
        ),
        actor="ann",
    ).id
    assert store.get(spool).label_weight_g is None  # type: ignore[union-attr]
    _weigh(pg_pool, spool, reading_g=850, resolution="label_wrong")
    edited = store.events(spool)[-2]
    assert edited.kind == "edited"
    before, after = edited.data["changes"]["label_weight_g"]
    assert before is None and after == 650
    row = store.get(spool)
    assert row is not None and row.label_weight_g == 650 and row.effective.label_weight_g == 650
    assert "label_weight_g" in row.overridden
    assert SpoolTypeStore(pg_pool).get(kind.id).label_weight_g == 1000  # type: ignore[union-attr]


@pg
def test_a_resolution_without_a_below_label_answer_is_refused(pg_pool: PgPool) -> None:
    spool = _new_spool(pg_pool)
    _print(pg_pool, spool, 12, T0, "r")
    with pytest.raises(NoBelowLabelError) as refused:
        _weigh(pg_pool, spool, reading_g=900, resolution="tared")
    assert refused.value.reason == "used"
    assert _changes(pg_pool, spool)[-1][0] != "weighed"


@pg
def test_a_resolution_the_check_does_not_offer_is_refused(pg_pool: PgPool) -> None:
    spool = _new_spool(pg_pool)
    with pytest.raises(NoBelowLabelError) as refused:
        _weigh(pg_pool, spool, reading_g=150, resolution="short")
    assert refused.value.reason == "below_label"
    assert _changes(pg_pool, spool) == [("created", None)]


# -- the spool's last drift and the notice (Task 3) ----------------------------------


def _get(pool: PgPool, spool: UUID, **defaults: Any) -> m.Spool:
    reader = lambda: FilamentDefaults(low_stock_pct=20, cost_per_kg=None, **defaults)  # noqa: E731
    found = FilamentSpoolStore(pool, reader).get(spool)
    assert found is not None
    return found


@pg
def test_the_newest_weighings_drift_is_on_the_spool(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    _weigh(pg_pool, spool, reading_g=1180, grams=980)
    assert _get(pg_pool, spool).last_drift_g == -12.0


@pg
def test_an_old_weighing_leaves_last_drift_null(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    _weigh(pg_pool, spool, reading_g=1180, grams=980)
    _weigh(pg_pool, spool, grams=975)
    assert _get(pg_pool, spool).last_drift_g is None
    assert _get(pg_pool, spool).drift_notice is False


@pg
def test_a_back_dated_weighing_leaves_the_newest_ones_drift(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    _weigh(pg_pool, spool, reading_g=1180, grams=980)
    _weigh(pg_pool, spool, reading_g=1190, grams=990, at=T0 + timedelta(minutes=30))
    assert _get(pg_pool, spool).last_drift_g == -12.0


@pg
def test_drift_notice_follows_the_setting(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    _weigh(pg_pool, spool, reading_g=1180, grams=980)
    assert _get(pg_pool, spool).drift_notice is True
    assert _get(pg_pool, spool, drift_notice_g=20).drift_notice is False
    assert _get(pg_pool, spool, drift_notice_g=12).drift_notice is True


# -- the closing preview (#7684) -----------------------------------------------------


def _store(pool: PgPool, **defaults: Any) -> FilamentSpoolStore:
    reader = lambda: FilamentDefaults(low_stock_pct=20, cost_per_kg=None, **defaults)  # noqa: E731
    return FilamentSpoolStore(pool, reader)


@pg
def test_the_preview_names_filament_left_on_a_finished_spool(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    preview = _store(pg_pool).closing_preview(
        spool, "finished", final_reading_g=230, final_grams=None
    )
    assert preview is not None
    assert [n.code for n in preview.notices] == ["filament_left"]
    assert preview.notices[0].text == "30 g of filament are still on it."
    assert preview.final_filament_g == 30 and preview.empty_g == 200
    assert preview.closing.started_g == 1000 and preview.closing.started_from == "reading"
    assert preview.closing.ledger_consumed_g == 8 and preview.closing.scale_consumed_g == 970
    assert preview.closing.drift_g == -962


@pg
def test_the_preview_notices_an_empty_spool_not_finished(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    preview = _store(pg_pool).closing_preview(
        spool, "damaged", final_reading_g=203, final_grams=None
    )
    assert preview is not None
    assert [n.code for n in preview.notices] == ["not_finished_but_empty"]
    assert preview.notices[0].text == "It reads empty; is it finished?"
    assert preview.final_filament_g == 3


@pg
def test_the_preview_follows_the_setting(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    preview = _store(pg_pool, finished_left_g=50).closing_preview(
        spool, "finished", final_reading_g=230, final_grams=None
    )
    assert preview is not None
    assert preview.notices == []


@pg
def test_the_preview_without_a_reading_closes_on_the_ledger(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    preview = _store(pg_pool).closing_preview(
        spool, "finished", final_reading_g=None, final_grams=None
    )
    assert preview is not None
    assert preview.final_filament_g == 992 and preview.closing.scale_consumed_g is None
    assert preview.closing.final_filament_g == 992 and preview.closing.drift_g is None
    assert [n.code for n in preview.notices] == ["filament_left"]


@pg
def test_the_preview_of_a_reading_under_the_empty_weight_has_no_final(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    preview = _store(pg_pool).closing_preview(
        spool, "finished", final_reading_g=150, final_grams=None
    )
    assert preview is not None
    assert preview.final_filament_g is None and preview.closing.final_filament_g is None
    assert preview.notices == []


@pg
def test_the_preview_of_no_spool_is_none(pg_pool: PgPool) -> None:
    assert _store(pg_pool).closing_preview(uuid4(), "finished", None, None) is None


# -- the archive (#7684) -------------------------------------------------------------


def test_a_restore_takes_no_reason_or_reading() -> None:
    with pytest.raises(ValueError, match="a restore takes no reason or reading"):
        m.SpoolArchive(archived=False, reason="finished")
    with pytest.raises(ValueError, match="a restore takes no reason or reading"):
        m.SpoolArchive(archived=False, final_reading_g=230)
    with pytest.raises(ValueError, match="final_reading_g is below final_grams"):
        m.SpoolArchive(reason="finished", final_reading_g=200, final_grams=230)
    assert m.SpoolArchive(reason="damaged", final_grams=12).archived is True


@pg
def test_archiving_with_a_final_reading_books_the_weighing_then_the_closing(
    pg_pool: PgPool,
) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    store = FilamentSpoolStore(pg_pool)
    archived_spool = store.archive(spool, True, actor="t", reason="finished", final_reading_g=230)
    assert archived_spool is not None and archived_spool.archived_at is not None
    events = store.events(spool)
    assert [e.kind for e in events][-2:] == ["weighed", "archived"]
    weighed, archived = events[-2], events[-1]
    assert weighed.grams_after == 30 and weighed.data["reading_g"] == 230
    assert weighed.data["expected_g"] == 992 and weighed.at < archived.at
    assert archived.data["reason"] == "finished" and archived.data["final_filament_g"] == 30
    assert archived.data["started_g"] == 1000 and archived.data["started_from"] == "reading"
    assert archived.data["ledger_consumed_g"] == 8 and archived.data["scale_consumed_g"] == 970
    assert archived.data["drift_g"] == pytest.approx(-962)
    assert archived_spool.grams_left == 30


@pg
def test_archiving_without_a_reading_closes_on_the_ledger(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    store = FilamentSpoolStore(pg_pool)
    store.archive(spool, True, actor="t", reason="damaged")
    events = store.events(spool)
    assert [e.kind for e in events][-1] == "archived" and "weighed" not in [
        e.kind for e in events[-2:]
    ]
    data = events[-1].data
    assert data["reason"] == "damaged" and data["final_filament_g"] == 992
    assert data["ledger_consumed_g"] == 8
    assert data["scale_consumed_g"] is None and data["drift_g"] is None


@pg
def test_a_final_reading_under_the_empty_weight_archives_nothing(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    store = FilamentSpoolStore(pg_pool)
    with pytest.raises(ReadingBelowEmptyError):
        store.archive(spool, True, actor="t", reason="damaged", final_reading_g=150)
    assert store.get(spool).archived_at is None  # type: ignore[union-attr]
    assert [e.kind for e in store.events(spool)][-1] not in {"weighed", "archived"}


@pg
def test_a_final_reading_needs_an_empty_weight(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, None)
    store = FilamentSpoolStore(pg_pool)
    with pytest.raises(NoEmptyWeightError):
        store.archive(spool, True, actor="t", reason="finished", final_reading_g=230)
    assert store.get(spool).archived_at is None  # type: ignore[union-attr]
    assert [e.kind for e in store.events(spool)] == ["created"]


@pg
def test_final_grams_are_the_filament_and_a_reading_with_them_is_the_net(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    store = FilamentSpoolStore(pg_pool)
    store.archive(spool, True, actor="t", reason="finished", final_reading_g=240, final_grams=30)
    weighed = store.events(spool)[-2]
    assert weighed.grams_after == 30 and weighed.data["empty_g"] == 210
    assert weighed.data["empty_source"] == "person"


@pg
def test_an_archived_spool_archived_again_books_nothing(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    store = FilamentSpoolStore(pg_pool)
    store.archive(spool, True, actor="t", reason="finished", final_reading_g=230)
    count = len(store.events(spool))
    store.archive(spool, True, actor="t", reason="finished", final_reading_g=230)
    assert len(store.events(spool)) == count


@pg
def test_a_patch_archive_closes_with_no_reason(pg_pool: PgPool) -> None:
    spool = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, spool)
    store = FilamentSpoolStore(pg_pool)
    store.update(spool, m.SpoolPatch(archived=True), actor="t")
    data = store.events(spool)[-1].data
    assert data["reason"] is None and data["final_filament_g"] == 992
    assert data["started_g"] == 1000 and data["scale_consumed_g"] is None


# -- a respooled spool continues as a new one (#7684) --------------------------------


def _continue(pool: PgPool, source: UUID, grams: float = 480, quantity: int = 1) -> list[UUID]:
    store = FilamentSpoolStore(pool)
    body = m.SpoolBulkCreate(
        spool=m.SpoolCreate(material_id=_material(pool), grams_left=grams, added_full=False),
        copy_of=source,
        continued_from=source,
        quantity=quantity,
    )
    with store.transaction() as conn:
        return store.bulk_on(conn, body, actor="t")


@pg
def test_a_continued_spool_links_both_ledgers(pg_pool: PgPool) -> None:
    source = _spool(pg_pool, _insert(pg_pool))
    _weighed_once(pg_pool, source)
    store = FilamentSpoolStore(pg_pool)
    store.archive(source, True, actor="t", reason="respooled", final_reading_g=680)
    (made,) = _continue(pg_pool, source)
    created = store.events(made)[0]
    assert created.kind == "created" and created.data["continued_from"] == str(source)
    assert created.grams_after == 480
    archived = store.events(source)[-1]
    assert archived.kind == "archived" and archived.data["continued_as"] == str(made)
    assert archived.data["reason"] == "respooled"


@pg
def test_an_unarchived_source_cannot_be_continued(pg_pool: PgPool) -> None:
    source = _spool(pg_pool, _insert(pg_pool))
    with pytest.raises(ContinuedFromNotArchivedError):
        _continue(pg_pool, source)
    assert FilamentSpoolStore(pg_pool).list(include_archived=True) == [
        FilamentSpoolStore(pg_pool).get(source)
    ]


@pg
def test_a_missing_source_cannot_be_continued(pg_pool: PgPool) -> None:
    with pytest.raises(SpoolCopySourceError):
        _continue(pg_pool, uuid4())


@pg
def test_only_one_spool_continues_a_source(pg_pool: PgPool) -> None:
    source = _spool(pg_pool, _insert(pg_pool))
    FilamentSpoolStore(pg_pool).archive(source, True, actor="t", reason="respooled")
    with pytest.raises(ValueError, match="one spool"):
        _continue(pg_pool, source, quantity=2)
