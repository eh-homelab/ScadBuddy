"""`PrinterStore.get`'s ``timeout`` is one deadline (#6037 review): the pool wait and the
statement share it, so the statement gets what the wait left. No Postgres: the pool is a
fake whose connection takes a while to come."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

from scadbuddy.printers.store import Pool, PrinterStore

WAIT = 0.3
TIMEOUT = 1.0


class Conn:
    def __init__(self) -> None:
        self.statements: list[str] = []

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def execute(self, query: str, params: object = None) -> Any:
        self.statements.append(query)
        return self

    def fetchone(self) -> None:
        return None


class SlowPool:
    """A pool whose connection comes after `WAIT` seconds."""

    def __init__(self) -> None:
        self.conn = Conn()
        self.timeouts: list[float | None] = []

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[Conn]:
        self.timeouts.append(timeout)
        time.sleep(WAIT)
        yield self.conn


def _statement_ms(conn: Conn) -> int:
    (setting,) = [s for s in conn.statements if "statement_timeout" in s]
    return int(setting.rsplit("=", 1)[1])


def test_the_statement_gets_what_the_pool_wait_left() -> None:
    pool = SlowPool()
    store = PrinterStore(cast(Pool, pool), None)
    assert store.get(str(uuid.uuid4()), timeout=TIMEOUT) is None
    assert pool.timeouts == [TIMEOUT]
    # One deadline: the wait spent at least WAIT of it, so the statement gets the rest.
    assert 1 <= _statement_ms(pool.conn) <= int((TIMEOUT - WAIT) * 1000)


def test_a_wait_that_spends_the_deadline_leaves_the_statement_a_millisecond() -> None:
    pool = SlowPool()
    store = PrinterStore(cast(Pool, pool), None)
    store.get(str(uuid.uuid4()), timeout=WAIT / 2)
    # 0 would mean no bound at all; the smallest bound is 1 ms.
    assert _statement_ms(pool.conn) == 1
