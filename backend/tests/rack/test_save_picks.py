"""``save_picks`` (#836, spec §5): advisory, and silent about serials."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import pytest

from scadbuddy.rack.usage import PickedHotend, save_picks
from tests.rack.helpers import serial


class Picks:
    def __init__(self, *, error: Exception | None = None, written: int | None = None) -> None:
        self.rows: list[tuple[int, int, list[PickedHotend]]] = []
        self.error = error
        self.written = written

    async def record_picks(
        self, queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]
    ) -> int:
        if self.error is not None:
            raise self.error
        self.rows.append((queue_item_id, printer_id, list(picks)))
        return len(picks) if self.written is None else self.written


PICK = PickedHotend(group_id=0, position=4, serial=serial(19))


async def test_each_queue_item_gets_the_picks() -> None:
    store = Picks()
    await save_picks(store, 1, [51, 52], [PICK])  # type: ignore[arg-type]
    assert [(item, printer) for item, printer, _ in store.rows] == [(51, 1), (52, 1)]


async def test_no_store_writes_nothing() -> None:
    await save_picks(None, 1, [51], [PICK])


async def test_a_pick_with_no_serial_is_sent_but_not_recorded() -> None:
    """Nothing can be attributed to an empty serial."""
    store = Picks()
    blank = PickedHotend(group_id=1, position=2, serial="")
    await save_picks(store, 1, [51], [PICK, blank])  # type: ignore[arg-type]
    assert [p.group_id for p in store.rows[0][2]] == [0]


@pytest.mark.rack_injects_errors
async def test_a_failed_write_is_logged_by_type_and_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = Picks(error=RuntimeError(f"violates key (serial)=({serial(19)})"))
    with caplog.at_level(logging.DEBUG):
        await save_picks(store, 1, [51], [PICK])  # type: ignore[arg-type]
    [record] = [r for r in caplog.records if r.name == "scadbuddy.rack.usage"]
    assert record.getMessage() == "could not record the rack picks"
    assert getattr(record, "error", None) == "RuntimeError"
    assert serial(19) not in repr(record.__dict__) and record.exc_info is None


async def test_fewer_rows_than_picks_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """#1015: a conflict is never expected; when one happens it is visible."""
    with caplog.at_level(logging.WARNING):
        await save_picks(Picks(written=0), 1, [51], [PICK])  # type: ignore[arg-type]
    assert [r.getMessage() for r in caplog.records] == [
        "a rack pick was already recorded for this queue item"
    ]
