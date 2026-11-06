"""The idempotency ledger of the CRUD routes' non-natural writes (#5298, plan D5)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, idempotent, prune, record
from scadbuddy.main import _prune_crud_requests
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

ROUTE = "POST /blob-index/{key}"


@pytest.fixture
def pool(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        conn.execute("CREATE TABLE writes (n int)")
    return pg_pool


def _write(pool: PgPool, key: str, body: dict[str, Any], *, fail: bool = False) -> Replay:
    """A route's write under ``key``: the ledger, then the write, then its reply."""
    with pool.connection() as conn, conn.transaction():
        stored = idempotent(conn, "render", key, ROUTE, body)
        if stored is not None:
            return stored
        conn.execute("INSERT INTO writes (n) VALUES (%s)", (body["n"],))
        reply = Replay(201, {"wrote": body["n"], "count": _count(conn)})
        if fail:
            raise RuntimeError("the write failed after its ledger row")
        record(conn, "render", key, reply)
        return reply


def _count(conn: Any, table: str = "writes") -> int:
    return int(conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"])


def test_one_key_is_one_write_and_its_reply_again(pool: PgPool) -> None:
    first = _write(pool, "k1", {"n": 7})
    again = _write(pool, "k1", {"n": 7})
    assert first == Replay(201, {"wrote": 7, "count": 1})
    assert again == first
    with pool.connection() as conn:
        assert _count(conn) == 1
        assert _count(conn, "crud_requests") == 1


def test_another_body_under_the_same_key_is_422(pool: PgPool) -> None:
    _write(pool, "k1", {"n": 7})
    with pytest.raises(ApiError) as refused:
        _write(pool, "k1", {"n": 8})
    assert refused.value.status == 422
    with pool.connection() as conn:
        assert _count(conn) == 1


def test_the_same_key_on_another_route_is_422(pool: PgPool) -> None:
    _write(pool, "k1", {"n": 7})
    with pool.connection() as conn, conn.transaction(), pytest.raises(ApiError) as refused:
        idempotent(conn, "render", "k1", "DELETE /blob-index/{key}", {"n": 7})
    assert refused.value.status == 422


def test_keys_are_per_caller(pool: PgPool) -> None:
    _write(pool, "k1", {"n": 7})
    with pool.connection() as conn, conn.transaction():
        assert idempotent(conn, "print", "k1", ROUTE, {"n": 9}) is None


def test_a_rolled_back_write_leaves_no_ledger_row(pool: PgPool) -> None:
    with pytest.raises(RuntimeError):
        _write(pool, "k1", {"n": 7}, fail=True)
    with pool.connection() as conn:
        assert _count(conn) == 0
        assert _count(conn, "crud_requests") == 0
    # The retry writes, as the first attempt never did.
    assert _write(pool, "k1", {"n": 7}) == Replay(201, {"wrote": 7, "count": 1})


def test_the_prune_drops_rows_past_the_retention_only(pool: PgPool) -> None:
    _write(pool, "old", {"n": 1})
    _write(pool, "new", {"n": 2})
    with pool.connection() as conn:
        conn.execute(
            "UPDATE crud_requests SET created_at = now() - interval '25 hours' WHERE key = 'old'"
        )
        assert prune(conn, timedelta(hours=24)) == 1
        keys = [r["key"] for r in conn.execute("SELECT key FROM crud_requests").fetchall()]
    assert keys == ["new"]


def test_the_auth_sweep_prunes_the_ledger(pool: PgPool) -> None:
    _write(pool, "old", {"n": 1})
    with pool.connection() as conn:
        conn.execute("UPDATE crud_requests SET created_at = now() - interval '25 hours'")
    state = SimpleNamespace(settings_store=SimpleNamespace(pool=pool))
    _prune_crud_requests(state)  # type: ignore[arg-type]
    with pool.connection() as conn:
        assert _count(conn, "crud_requests") == 0
