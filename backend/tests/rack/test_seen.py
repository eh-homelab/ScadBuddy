"""``record_seen`` (#836, spec §4): the rack's hotends, advisory, never logged by serial."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import psycopg
import pytest

from scadbuddy.rack.usage import RACK_SEEN_FALLBACK, record_seen
from tests.rack.helpers import mounted, serial, slot, status


class Seen:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[int, list[str]]] = []
        self.error = error

    async def seen(self, printer_id: int, serials: Iterable[str]) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append((printer_id, sorted(serials)))


async def test_the_racks_hotends_are_recorded_and_the_left_hotend_is_not() -> None:
    store = Seen()
    left = mounted().model_copy(update={"id": 1, "serial_number": serial(1)})
    await record_seen(store, 1, status(mounted(), left, *(slot(p) for p in (2, 3, 4, 5, 6))))  # type: ignore[arg-type]
    assert store.calls == [(1, sorted([serial(0), *(serial(p + 15) for p in (2, 3, 4, 5, 6))]))]


async def test_no_status_or_no_store_records_nothing() -> None:
    store = Seen()
    await record_seen(store, 1, None)  # type: ignore[arg-type]
    await record_seen(None, 1, status(slot(2)))
    assert store.calls == []


async def test_a_failed_write_is_logged_by_type_only(caplog: pytest.LogCaptureFixture) -> None:
    store = Seen(psycopg.OperationalError(f"duplicate key (serial)=({serial(17)})"))
    with caplog.at_level(logging.DEBUG):
        await record_seen(store, 1, status(slot(2)))  # type: ignore[arg-type]
    [record] = [r for r in caplog.records if r.name == "scadbuddy.rack.usage"]
    assert record.getMessage() == RACK_SEEN_FALLBACK
    assert record.__dict__["error"] == "OperationalError"
    assert serial(17) not in repr(record.__dict__) and record.exc_info is None
