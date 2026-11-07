"""The idempotency ledger of the CRUD routes' non-natural writes (#5298, plan D5)."""

from __future__ import annotations

import threading
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, idempotent, idempotent_write, prune, record
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
    """A route's write under ``key``, through `idempotent_write`."""
    with pool.connection() as conn:

        def write() -> Replay:
            conn.execute("INSERT INTO writes (n) VALUES (%s)", (body["n"],))
            if fail:
                raise RuntimeError("the write failed")
            return Replay(201, {"wrote": body["n"], "count": _count(conn)})

        return idempotent_write(conn, "render", key, ROUTE, body, write)


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


def test_a_write_that_never_records_leaves_nothing_to_replay(pool: PgPool) -> None:
    """Only `record` inserts a ledger row, and only with the reply: a route that wrote
    without it leaves no row, so a retry writes again rather than replaying no reply."""
    with pool.connection() as conn, conn.transaction():
        assert idempotent(conn, "render", "k1", ROUTE, {"n": 7}) is None
    with pool.connection() as conn:
        assert _count(conn, "crud_requests") == 0
    assert _write(pool, "k1", {"n": 7}).status == 201


def test_a_reply_outside_http_statuses_is_refused(pool: PgPool) -> None:
    with pool.connection() as conn, conn.transaction(), pytest.raises(Exception, match="check"):
        record(conn, "render", "k1", ROUTE, {"n": 7}, Replay(0, None))


def _second(pool: PgPool, key: str, body: dict[str, Any]) -> tuple[threading.Thread, list[Any]]:
    """`_write` in a thread; the list gets its reply (or error)."""
    out: list[Any] = []

    def run() -> None:
        try:
            out.append(_write(pool, key, body))
        except Exception as error:
            out.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, out


@pytest.mark.parametrize("commit", [True, False])
def test_a_second_request_with_the_key_waits_for_the_first(pool: PgPool, commit: bool) -> None:
    """B blocks on A's lock; it replays A's reply once A commits, and writes once A rolls
    back."""
    body = {"n": 7}
    with pool.connection() as a:
        a.execute("BEGIN")
        assert idempotent(a, "render", "k1", ROUTE, body) is None
        a.execute("INSERT INTO writes (n) VALUES (7)")
        thread, out = _second(pool, "k1", body)
        thread.join(timeout=1.0)
        assert thread.is_alive(), "B did not wait for A's transaction"
        if commit:
            record(a, "render", "k1", ROUTE, body, Replay(201, {"by": "a"}))
            a.execute("COMMIT")
        else:
            a.execute("ROLLBACK")
        thread.join(timeout=10)
    assert not thread.is_alive()
    if commit:
        assert out == [Replay(201, {"by": "a"})]
    else:
        assert out == [Replay(201, {"wrote": 7, "count": 1})]
    with pool.connection() as conn:
        assert _count(conn) == 1
        assert _count(conn, "crud_requests") == 1
