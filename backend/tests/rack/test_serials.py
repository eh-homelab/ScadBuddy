"""Spec 2026-10-01 §7, §9: a full pick, enqueue and settle with invented serials,
including the failure paths of §5, leaves no serial in any log record."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime

import pytest

from scadbuddy.bambuddy.filaments import FilamentWarning, SpoolOption
from scadbuddy.bambuddy.models import ArchiveDetail
from scadbuddy.bambuddy.print_links import PrintLink
from scadbuddy.bambuddy.print_run import rack_chooser
from scadbuddy.core.problems import ApiError
from scadbuddy.rack.rank import Usage
from scadbuddy.rack.usage import PickedHotend, record_seen, record_settled, save_picks
from tests.bambuddy.test_rack_chooser import Reads, grouped
from tests.rack.helpers import INVENTED_SERIALS, requirement, slot, status
from tests.support.rack_guard import foreign_rack_errors


def leak(serial: str) -> str:
    return f'duplicate key value violates "rack_nozzle_picks_pkey": (serial)=({serial})'


class Leaky:
    """A store whose every call fails with a serial in its message, as a database's might."""

    async def seen(self, printer_id: int, serials: Iterable[str]) -> None:
        raise RuntimeError(leak(next(iter(serials))))

    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]:
        raise RuntimeError(leak(next(iter(serials))))

    async def record_picks(
        self, queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]
    ) -> int:
        raise RuntimeError(leak(picks[0].serial))

    async def picked_items(self, queue_item_ids: Iterable[int]) -> set[int]:
        return set(queue_item_ids)

    async def record_prints(
        self,
        *,
        archive_id: int,
        queue_item_id: int,
        settled_at: datetime,
        print_seconds: int | None,
        grams: float | None,
    ) -> int:
        raise RuntimeError(leak(INVENTED_SERIALS[2]))


class Archives:
    async def archive(self, archive_id: int) -> ArchiveDetail:
        if archive_id == 101:
            raise ApiError(503, f"archive read failed for hotend {INVENTED_SERIALS[3]}")
        return ArchiveDetail(id=archive_id, actual_time_seconds=60)


class Links:
    async def for_output(self, output_id: str) -> list[PrintLink]:
        return [
            PrintLink(archive_id=101, matched_by="queue_item", queue_item_id=51),
            PrintLink(archive_id=102, matched_by="queue_item", queue_item_id=51),
        ]


async def test_no_serial_reaches_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    rack = status(
        slot(2, serial_number=INVENTED_SERIALS[2]), slot(4, serial_number=INVENTED_SERIALS[4])
    )
    warnings: list[FilamentWarning] = []
    with caplog.at_level(logging.DEBUG):
        choose = rack_chooser(
            Reads(grouped(requirement()), rack),
            printer_id=1,
            plate_id=1,
            spools={1: SpoolOption(spool_id=9, material="PLA")},
            algorithm="least_used",
            manual_position=None,
            rack=Leaky(),
            warnings=warnings,
        )
        assert await choose(77) is None  # usage() failed: no choice, a warning
        await record_seen(Leaky(), 1, rack)
        await save_picks(
            Leaky(), 1, [51], [PickedHotend(group_id=0, position=2, serial=INVENTED_SERIALS[2])]
        )
        await record_settled("c" * 32, client=Archives(), links=Links(), store=Leaky())

    assert len(caplog.records) >= 4
    for record in caplog.records:
        text = f"{record.getMessage()} {record.__dict__!r}"
        assert not [serial for serial in INVENTED_SERIALS if serial in text], record.getMessage()
        assert record.exc_info is None and record.exc_text is None
    assert [w.kind for w in warnings] == ["rack-left-to-bambuddy"]
    assert not [s for s in INVENTED_SERIALS if s in repr(warnings)]


def test_the_guard_flags_a_programming_error_but_not_an_api_one() -> None:
    def record(error: str) -> logging.LogRecord:
        made = logging.LogRecord(
            "x", logging.WARNING, __file__, 1, "rack pick left to Bambuddy", (), None
        )
        made.error = error
        return made

    flagged = foreign_rack_errors(
        [
            record(name)
            for name in ("ApiError", "ReadTimeout", "StopIteration", "TypeError", "KeyError")
        ]
    )
    assert flagged == ["TypeError", "KeyError"]
