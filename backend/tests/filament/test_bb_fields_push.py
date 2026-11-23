"""The push of the spool fields Bambuddy keeps and of SB's consumed-counter reset (#7149,
plan `docs/superpowers/plans/2026-11-23-spool-bb-fields.md`, R-7149-1 and R-7149-5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from scadbuddy.core.settings import FilamentSync
from scadbuddy.filament import ledger
from scadbuddy.filament.bambuddy_pull import pull
from scadbuddy.filament.bb_sync_plan import field_hash
from scadbuddy.filament.bb_sync_results import reset_links
from scadbuddy.filament.models import SpoolPatch
from tests.conftest import PgPool
from tests.filament.test_bb_sync_plan import (
    SPOOLS,
    _filament,
    _link,
    _matching,
    _only,
    _plan,
    _snap,
    _spool,
    _type,
)
from tests.filament.test_bb_sync_results import _answer, _apply, _bb_now, _ok

pytestmark = pytest.mark.requires_postgres

TRAY = "0123456789ABCDEF0123456789ABCDEF"

WEIGHTS = FilamentSync(
    spools="sb_to_bb",
    weights="sb_to_bb",
    tags="off",
    presets="off",
    suppliers="off",
    locations="off",
)


def _origin(pool: PgPool, spool: Any, origin: str) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE filament_spools SET data_origin = %s WHERE id = %s", (origin, spool))


def _reset(pool: PgPool, spool: Any, source: str = "person") -> None:
    with pool.connection() as conn, conn.transaction():
        ledger.record(conn, spool, ledger.CONSUMED_RESET, source=source, actor="ann")  # type: ignore[arg-type]


def test_the_origin_is_pushed_and_none_never_clears_bambuddys(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    row = _matching(pg_pool, spool, 50) | {"data_origin": "rfid_auto"}
    _link(pg_pool, spool, 50)
    assert _plan(f, _snap([row])).writes == []
    _origin(pg_pool, spool, "nfc_link")
    (update,) = _plan(f, _snap([row])).writes
    assert update.body == {"data_origin": "nfc_link"}


def test_a_new_spool_is_created_with_its_origin_only(pg_pool: PgPool) -> None:
    """R-7149-2, R-7149-3: Bambuddy takes no write of ``added_full`` or ``encode_time``."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    f.spools.update(spool, SpoolPatch(added_full=True), actor="ann")
    _origin(pg_pool, spool, "nfc_link")
    create = _only(_plan(f, _snap([])).writes, "create")
    assert isinstance(create.body, dict)
    assert create.body["data_origin"] == "nfc_link"
    assert not {"added_full", "encode_time", "weight_used_baseline"} & create.body.keys()


def test_an_sb_reset_is_pushed_once(pg_pool: PgPool) -> None:
    """R-7149-5."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    row = _matching(pg_pool, spool, 50, WEIGHTS)
    _link(pg_pool, spool, 50)
    assert _plan(f, _snap([row]), WEIGHTS).writes == []
    _reset(pg_pool, spool)
    assert _plan(f, _snap([row]), SPOOLS).writes == []
    (write,) = _plan(f, _snap([row]), WEIGHTS).writes
    assert (write.op, write.bb_id, write.body) == ("reset_consumed", 50, {})
    assert set(write.hashes) == {"consumed_reset"}

    answer = _answer(50, **{k: v for k, v in row.items() if k != "id"})
    answer |= {"weight_used_baseline": answer["weight_used"], "updated_at": _bb_now()}
    report = _apply(f, [write], [_ok(write, answer)])
    assert report.failed == 0
    assert _plan(f, _snap([answer]), WEIGHTS).writes == []
    # The echo holds BB's new baseline: SB's own reset never comes back as BB's.
    pull(f, [_parse(answer)], now=datetime.now(UTC))
    kinds = [(e.kind, e.source) for e in f.spools.events(spool) if e.kind == "consumed_reset"]
    assert kinds == [("consumed_reset", "person")]


def test_a_bb_reset_and_an_archived_spool_push_no_reset(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    row = _matching(pg_pool, spool, 50, WEIGHTS)
    _link(pg_pool, spool, 50)
    _reset(pg_pool, spool, "bb")
    assert _plan(f, _snap([row]), WEIGHTS).writes == []
    _reset(pg_pool, spool)
    _link_pushed(pg_pool, spool, {})
    f.spools.archive(spool, True, actor="ann")
    archived = _plan(f, _snap([row]), WEIGHTS).writes
    assert [w.op for w in archived] == ["archive"]


def test_a_pushed_reset_hash_skips_the_write(pg_pool: PgPool) -> None:
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    row = _matching(pg_pool, spool, 50, WEIGHTS)
    _link(pg_pool, spool, 50)
    _reset(pg_pool, spool)
    newest = [e for e in f.spools.events(spool) if e.kind == "consumed_reset"][-1]
    _link_pushed(pg_pool, spool, {"consumed_reset": field_hash(str(newest.id))})
    assert _plan(f, _snap([row]), WEIGHTS).writes == []


def _resets(writes: Any) -> list[Any]:
    return [w for w in writes if w.op == "reset_consumed"]


def test_a_reset_before_the_pulls_link_is_never_sent(pg_pool: PgPool) -> None:
    """R-7149-11: the pull links a spool SB reset before; Bambuddy never shared that
    reset, so its counter is left alone."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    f.spools.bind_tag(spool, tray_uuid=TRAY, kind="bambu_mifare", actor="ann")
    _reset(pg_pool, spool)
    row = _matching(pg_pool, spool, 50, WEIGHTS) | {"tray_uuid": TRAY, "updated_at": _bb_now()}
    report = pull(f, [_parse(row)], now=datetime.now(UTC))
    assert report.linked_ids == [spool]
    assert _resets(_plan(f, _snap([row]), WEIGHTS).writes) == []
    _reset(pg_pool, spool)
    assert len(_resets(_plan(f, _snap([row]), WEIGHTS).writes)) == 1


def test_a_reset_before_a_relink_is_never_sent(pg_pool: PgPool) -> None:
    """R-7149-11: after ``reset-links`` the new link is newer than the reset."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    row = _matching(pg_pool, spool, 50, WEIGHTS)
    _link(pg_pool, spool, 50)
    _reset(pg_pool, spool)
    assert len(_resets(_plan(f, _snap([row]), WEIGHTS).writes)) == 1
    with pg_pool.connection() as conn:
        conn.execute("UPDATE filament_bb_sync_state SET instance = 'old'")
    reset_links(f, "old", "new", None, expect_kept=False)
    _link(pg_pool, spool, 50)
    assert _resets(_plan(f, _snap([row]), WEIGHTS).writes) == []


def _bb_change_then_unrelated_push(
    f: Any, pool: PgPool, spool: Any, change: dict[str, Any]
) -> dict[str, Any]:
    """R-7149-8: pull once, BB changes ``change``, then SB pushes an unrelated note before
    the next pull. Answers BB's row as the push's answer left it."""
    row = _matching(pool, spool, 50, WEIGHTS) | {
        "weight_used_baseline": 0.0,
        "added_full": None,
        "updated_at": _bb_now(-30),
    }
    _link(pool, spool, 50)
    pull(f, [_parse(row)], now=datetime.now(UTC))
    moved = row | change | {"updated_at": _bb_now(-20)}
    f.spools.update(spool, SpoolPatch(note="unrelated"), actor="ann")
    (write,) = _plan(f, _snap([moved]), WEIGHTS).writes
    assert (write.op, write.body) == ("update", {"note": "unrelated"})
    answer = moved | {"note": "unrelated", "updated_at": _bb_now(-10)}
    assert _apply(f, [write], [_ok(write, answer)]).failed == 0
    return answer


def test_a_bb_reset_behind_a_pushed_echo_is_booked_once(pg_pool: PgPool) -> None:
    """R-7149-8: the echo already holds BB's new baseline; the link's ``bb_seen`` does not."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    answer = _bb_change_then_unrelated_push(f, pg_pool, spool, {"weight_used_baseline": 75.0})
    pull(f, [_parse(answer)], now=datetime.now(UTC))
    pull(f, [_parse(answer)], now=datetime.now(UTC))
    resets = [e.source for e in f.spools.events(spool) if e.kind == "consumed_reset"]
    assert resets == ["bb"]


def test_a_bb_added_full_behind_a_pushed_echo_is_taken(pg_pool: PgPool) -> None:
    """R-7149-8."""
    f = _filament(pg_pool)
    spool = _spool(f, _type(f))
    answer = _bb_change_then_unrelated_push(f, pg_pool, spool, {"added_full": True})
    pull(f, [_parse(answer)], now=datetime.now(UTC))
    got = f.spools.get(spool)
    assert got is not None
    assert got.added_full is True


def _link_pushed(pool: PgPool, spool: Any, pushed: dict[str, str]) -> None:
    from psycopg.types.json import Jsonb

    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_bb_links SET pushed_fields = %s WHERE spool_id = %s",
            (Jsonb(pushed), spool),
        )


def _parse(row: dict[str, Any]) -> Any:
    from scadbuddy.bambuddy.models import Spool as BbSpool

    return BbSpool.model_validate(row)
