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
from scadbuddy.bambuddy.print_run import (
    RACK_PICK_FALLBACK,
    RACK_USAGE_FALLBACK,
    rack_chooser,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.rack.rank import Usage
from scadbuddy.rack.usage import (
    RACK_PICKS_FALLBACK,
    RACK_SEEN_FALLBACK,
    RACK_SETTLE_FALLBACK,
    RACK_SETTLE_READ_FALLBACK,
    RACK_STORE_FALLBACKS,
    PickedHotend,
    record_seen,
    record_settled,
    save_picks,
)
from tests.bambuddy.test_rack_chooser import Reads, grouped
from tests.rack.helpers import INVENTED_SERIALS, requirement, slot, status
from tests.support.rack_guard import MESSAGES, foreign_rack_errors


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

    async def recorded_archives(self, archive_ids: Iterable[int]) -> set[int]:
        return set()

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


class UnreadablePicks(Leaky):
    """The settle's read of the picked items fails, as a database error would."""

    async def picked_items(self, queue_item_ids: Iterable[int]) -> set[int]:
        raise RuntimeError(leak(INVENTED_SERIALS[4]))


class Archives:
    async def archive(self, archive_id: int) -> ArchiveDetail:
        if archive_id == 101:
            raise ApiError(503, f"archive read failed for hotend {INVENTED_SERIALS[3]}")
        return ArchiveDetail(id=archive_id, status="completed", actual_time_seconds=60)


class Links:
    async def for_output(self, output_id: str) -> list[PrintLink]:
        return [
            PrintLink(archive_id=101, matched_by="queue_item", queue_item_id=51),
            PrintLink(archive_id=102, matched_by="queue_item", queue_item_id=51),
        ]


EXPECTED_MESSAGES = [
    RACK_USAGE_FALLBACK,
    RACK_SEEN_FALLBACK,
    RACK_PICK_FALLBACK,
    RACK_SEEN_FALLBACK,
    RACK_PICKS_FALLBACK,
    RACK_SETTLE_FALLBACK,
    RACK_SETTLE_FALLBACK,
    RACK_SETTLE_READ_FALLBACK,
]


@pytest.mark.rack_injects_errors
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
        # usage() and seen() fail: ranked without usage, so a choice is still made.
        assert await choose(77) is not None
        unreadable = rack_chooser(
            Reads(grouped(requirement()), RuntimeError(leak(INVENTED_SERIALS[4]))),
            printer_id=1,
            plate_id=1,
            spools={1: SpoolOption(spool_id=9, material="PLA")},
            algorithm="least_used",
            manual_position=None,
            rack=Leaky(),
            warnings=warnings,
        )
        assert await unreadable(77) is None  # status failed: no choice, a warning
        await record_seen(Leaky(), 1, rack)
        await save_picks(
            Leaky(), 1, [51], [PickedHotend(group_id=0, position=2, serial=INVENTED_SERIALS[2])]
        )
        await record_settled("c" * 32, client=Archives(), links=Links(), store=Leaky())
        assert (
            await record_settled(
                "d" * 32, client=Archives(), links=Links(), store=UnreadablePicks()
            )
            == 0
        )

    assert sorted(r.getMessage() for r in caplog.records) == sorted(EXPECTED_MESSAGES)
    for record in caplog.records:
        text = f"{record.getMessage()} {record.__dict__!r}"
        assert not [serial for serial in INVENTED_SERIALS if serial in text], record.getMessage()
        assert record.exc_info is None and record.exc_text is None
    assert [w.kind for w in warnings] == ["rack-left-to-bambuddy"]
    assert not [s for s in INVENTED_SERIALS if s in repr(warnings)]


@pytest.mark.parametrize("message", sorted(MESSAGES))
def test_the_guard_covers_every_rack_fallback(message: str) -> None:
    """#1081, #1112: the preview, the usage read and the store's writes swallow
    exceptions too."""
    made = logging.LogRecord("x", logging.WARNING, __file__, 1, message, (), None)
    made.error = "KeyError"
    assert foreign_rack_errors([made]) == ["KeyError"]


def test_the_guard_expects_postgres_failures_only_from_the_usage_read() -> None:
    """#1086 review: a usage read's Postgres failure is infrastructure, not a bug; the
    pick and the preview make no Postgres read, so there it still trips the guard."""

    def record(message: str, error: str) -> logging.LogRecord:
        made = logging.LogRecord("x", logging.WARNING, __file__, 1, message, (), None)
        made.error = error
        return made

    usage = [
        record(RACK_USAGE_FALLBACK, name)
        for name in (
            "QueryCanceled",
            "OperationalError",
            "PoolTimeout",
            "TypeError",
            "UndefinedColumn",
            "DataError",
        )
    ]
    assert foreign_rack_errors(usage) == ["TypeError", "UndefinedColumn", "DataError"]
    assert foreign_rack_errors([record(RACK_PICK_FALLBACK, "QueryCanceled")]) == ["QueryCanceled"]


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
    # claude-review on #1043, finding 5: StopIteration is a real bug's error, never expected.
    assert flagged == ["StopIteration", "TypeError", "KeyError"]


def test_the_guard_covers_the_store_fallbacks_and_expects_only_infrastructure() -> None:
    """#1112: the store's writes and reads swallow exceptions too. A Postgres outage or
    timeout there is expected; a programming error, such as a typo in the seen upsert, is
    not. Only the per-archive settle also reads Bambuddy, so only it expects Bambuddy's
    errors and the archive read's ``TimeoutError``."""
    assert RACK_STORE_FALLBACKS <= MESSAGES

    def record(message: str, error: str) -> logging.LogRecord:
        made = logging.LogRecord("x", logging.WARNING, __file__, 1, message, (), None)
        made.error = error
        return made

    for message in sorted(RACK_STORE_FALLBACKS):
        expected = [record(message, name) for name in ("QueryCanceled", "PoolTimeout")]
        assert foreign_rack_errors(expected) == [], message
        bugs = [record(message, name) for name in ("UndefinedColumn", "KeyError", "TypeError")]
        assert foreign_rack_errors(bugs) == ["UndefinedColumn", "KeyError", "TypeError"], message
    bambuddy = ("ApiError", "ConnectError", "TimeoutError")
    assert foreign_rack_errors([record(RACK_SETTLE_FALLBACK, name) for name in bambuddy]) == []
    for message in sorted(RACK_STORE_FALLBACKS - {RACK_SETTLE_FALLBACK}):
        records = [record(message, name) for name in bambuddy]
        assert foreign_rack_errors(records) == list(bambuddy), message
