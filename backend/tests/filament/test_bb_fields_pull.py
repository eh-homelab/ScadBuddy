"""The pull takes the spool fields Bambuddy keeps, and BB's consumed-counter resets (#7149,
plan `docs/superpowers/plans/2026-11-23-spool-bb-fields.md`). Bambuddy's rows are the
recorded shape (`tests/bambuddy/recordings/inventory-spools.json`), built by `bb`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scadbuddy.filament.bambuddy_pull import Pulls, pull
from scadbuddy.filament.models import SpoolPatch
from tests.conftest import PgPool
from tests.filament.test_bambuddy_pull import NOW, _filament, bb

pytestmark = pytest.mark.requires_postgres


def test_a_new_spool_takes_the_origin_added_full_and_encode_time(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(
        f,
        [bb(data_origin="rfid_auto", added_full=True, encode_time="2026-11-13T09:00:00")],
        now=NOW,
    ).created_ids
    spool = f.spools.get(made)
    assert spool is not None
    assert (spool.data_origin, spool.added_full) == ("rfid_auto", True)
    assert spool.tag_encoded_at == datetime(2026, 11, 13, 9, tzinfo=UTC)


def test_a_linked_spool_takes_bbs_changes_and_a_bb_null_clears_no_origin(
    pg_pool: PgPool,
) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(data_origin="rfid_auto")], now=NOW).created_ids
    moved = bb(data_origin="rfid_linked", added_full=False, updated_at="2026-11-14T11:00:00")
    assert pull(f, [moved], now=NOW).updated_ids == [made]
    spool = f.spools.get(made)
    assert spool is not None
    assert (spool.data_origin, spool.added_full) == ("rfid_linked", False)
    cleared = bb(data_origin=None, added_full=False, updated_at="2026-11-14T12:00:00")
    pull(f, [cleared], now=NOW)
    again = f.spools.get(made)
    assert again is not None
    assert again.data_origin == "rfid_linked"


def test_a_persons_newer_added_full_stays(pg_pool: PgPool) -> None:
    """The newer change wins per field (§8.2)."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(added_full=None)], now=NOW).created_ids
    f.spools.update(made, SpoolPatch(added_full=False), actor="ann")
    pull(f, [bb(added_full=True, updated_at="2026-11-14T11:00:00")], now=NOW)
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.added_full is False


def test_an_origin_outside_bambuddys_form_is_not_taken(pg_pool: PgPool) -> None:
    """R-7149-1: the column takes ``^[a-z0-9_]{1,20}$``; the spool is still pulled."""
    f = _filament(pg_pool)
    report = pull(f, [bb(data_origin="Not An Origin!")], now=NOW)
    (made,) = report.created_ids
    spool = f.spools.get(made)
    assert spool is not None
    assert spool.data_origin is None


def _resets(f: object, spool: object) -> list[tuple[str | None, datetime, dict[str, object]]]:
    events = f.spools.events(spool)  # type: ignore[attr-defined]
    return [(e.source, e.at, e.data) for e in events if e.kind == "consumed_reset"]


def test_a_bb_reset_is_booked_once_and_a_first_reading_books_none(pg_pool: PgPool) -> None:
    """R-7149-6."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(weight_used_baseline=40.0)], now=NOW).created_ids
    assert _resets(f, made) == []
    reset = bb(weight_used=100.0, weight_used_baseline=100.0, updated_at="2026-11-14T11:00:00")
    pull(f, [reset], now=NOW)
    pull(f, [reset], now=NOW)
    ((source, at, data),) = _resets(f, made)
    # R-7149-9: at the newest reading that still held the old baseline (10:00), not BB's
    # ``updated_at`` (11:00), so no plate between the two is dropped.
    assert (source, at) == ("bb", datetime(2026, 11, 14, 10, tzinfo=UTC))
    assert data["bb_baseline"] == 100.0
    assert f.metrics.history(made) is not None


def test_a_bb_reset_waits_while_weights_do_not_pull(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    (made,) = pull(f, [bb()], now=NOW).created_ids
    reset = bb(weight_used_baseline=100.0, updated_at="2026-11-14T11:00:00")
    pull(f, [reset], now=NOW, pulls=Pulls(weights=False))
    assert _resets(f, made) == []
    pull(f, [reset], now=NOW)
    assert [s for s, _, _ in _resets(f, made)] == ["bb"]


def test_a_reset_seen_after_a_later_bb_change_is_dated_at_the_last_old_reading(
    pg_pool: PgPool,
) -> None:
    """R-7149-9: BB keeps no reset time. Its row changed again after the reset, so its
    ``updated_at`` is too late; the reset is dated at the newest reading that still held
    the old baseline, and a plate between the two counts."""
    f = _filament(pg_pool)
    (made,) = pull(f, [bb(weight_used_baseline=40.0)], now=NOW).created_ids
    later = bb(weight_used_baseline=100.0, note="edited after", updated_at="2026-11-14T15:00:00")
    pull(f, [later], now=NOW)
    ((_, at, _),) = _resets(f, made)
    assert at == datetime(2026, 11, 14, 10, tzinfo=UTC)


def _strip_new_keys(pool: PgPool, spool: object) -> None:
    """A link and its readings as a build before #7149 left them."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spool_events SET data = jsonb_set(data, '{bb}', (data -> 'bb')"
            " - 'data_origin' - 'added_full' - 'tag_encoded_at' - 'consumed_baseline')"
            " WHERE spool_id = %s AND kind = 'synced_from_bb'",
            (spool,),
        )
        conn.execute(
            "UPDATE filament_bb_links SET bb_seen = bb_seen - 'data_origin' - 'added_full'"
            " - 'encode_time' - 'weight_used_baseline' WHERE spool_id = %s",
            (spool,),
        )


def _synced(f: object, spool: object) -> int:
    events = f.spools.events(spool)  # type: ignore[attr-defined]
    return sum(e.kind == "synced_from_bb" for e in events)


@pytest.mark.parametrize("moved", [False, True])
def test_the_first_pull_after_the_deploy_books_nothing_for_the_new_keys(
    pg_pool: PgPool, moved: bool
) -> None:
    """Review nit 2 on #7181: a reading that lacks only the new keys is seeded quietly;
    no ``synced_from_bb`` event and no reset."""
    f = _filament(pg_pool)
    row = bb(data_origin="rfid_auto", weight_used_baseline=40.0)
    (made,) = pull(f, [row], now=NOW).created_ids
    _strip_new_keys(pg_pool, made)
    before = _synced(f, made)
    again = bb(
        data_origin="rfid_auto",
        weight_used_baseline=40.0,
        updated_at="2026-11-14T11:00:00" if moved else "2026-11-14T10:00:00",
    )
    pull(f, [again], now=NOW)
    pull(f, [again], now=NOW)
    assert _synced(f, made) == before
    assert _resets(f, made) == []
