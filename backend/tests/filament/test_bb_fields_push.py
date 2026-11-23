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
    _reset(pg_pool, spool)
    newest = [e for e in f.spools.events(spool) if e.kind == "consumed_reset"][-1]
    _link(pg_pool, spool, 50, pushed={"consumed_reset": field_hash(str(newest.id))})
    assert _plan(f, _snap([row]), WEIGHTS).writes == []


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
