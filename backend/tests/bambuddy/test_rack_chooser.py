"""The run's ``choose_rack`` callback (#836, spec 2026-10-01 §5). It never raises."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime

import pytest

from scadbuddy.bambuddy import print_run
from scadbuddy.bambuddy.filaments import FilamentWarning, SpoolOption
from scadbuddy.bambuddy.models import (
    FilamentRequirement,
    FilamentRequirements,
    PrinterStatus,
    RackAlgorithm,
)
from scadbuddy.bambuddy.print_run import ChooseRack, rack_chooser
from scadbuddy.core.problems import ApiError
from scadbuddy.rack.rank import Usage
from scadbuddy.rack.usage import PickedHotend
from tests.rack.helpers import requirement, serial, slot, status

SPOOLS = {1: SpoolOption(spool_id=9, material="PLA")}


class Reads:
    def __init__(
        self,
        requirements: FilamentRequirements | Exception,
        rack: PrinterStatus | Exception,
    ) -> None:
        self.requirements = requirements
        self.rack = rack
        self.status_reads = 0
        self.requirement_reads: list[tuple[int, int | None]] = []

    async def filament_requirements(
        self, file_id: int, *, plate_id: int | None = None
    ) -> FilamentRequirements:
        self.requirement_reads.append((file_id, plate_id))
        if isinstance(self.requirements, Exception):
            raise self.requirements
        return self.requirements

    async def printer_status(self, printer_id: int) -> PrinterStatus:
        self.status_reads += 1
        if isinstance(self.rack, Exception):
            raise self.rack
        return self.rack


class Usages:
    """A ``RackUsage`` that remembers the order of its calls."""

    def __init__(self, usage: dict[str, Usage] | None = None) -> None:
        self.calls: list[str] = []
        self._usage = usage or {}

    async def seen(self, printer_id: int, serials: Iterable[str]) -> None:
        self.calls.append("seen")

    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]:
        self.calls.append("usage")
        return self._usage

    async def record_picks(
        self, queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]
    ) -> int:
        return len(picks)

    async def picked_items(self, queue_item_ids: Iterable[int]) -> set[int]:
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
        return 0


def grouped(*filaments: FilamentRequirement) -> FilamentRequirements:
    return FilamentRequirements(filaments=list(filaments))


def chooser(
    reads: Reads,
    warnings: list[FilamentWarning],
    *,
    usages: Usages | None = None,
    manual: int | None = None,
    algorithm: RackAlgorithm = "least_used",
    plate_id: int = 1,
) -> ChooseRack:
    return rack_chooser(
        reads,
        printer_id=1,
        plate_id=plate_id,
        spools=SPOOLS,
        algorithm=algorithm,
        manual_position=manual,
        rack=usages if usages is not None else Usages(),
        warnings=warnings,
    )


async def test_the_ranked_choice_is_returned_keyed_by_group_id() -> None:
    warnings: list[FilamentWarning] = []
    reads = Reads(grouped(requirement(color="#FF6A13")), status(slot(2), slot(4, color="FF6A13FF")))
    choice = await chooser(reads, warnings)(77)
    assert choice is not None
    assert choice.nozzle_rack_choice == {"0": 4}
    assert [(p.group_id, p.position, p.serial) for p in choice.picks] == [(0, 4, serial(19))]
    assert warnings == []


async def test_the_sliced_files_requirements_are_read_for_the_plate() -> None:
    """Plate 2's slice is read for plate 2: a multi-plate 3MF answers plate 1 otherwise."""
    reads = Reads(grouped(requirement()), status(slot(2)))
    await chooser(reads, [], plate_id=2)(77)
    assert reads.requirement_reads == [(77, 2)]


async def test_usage_is_read_before_the_read_is_recorded_as_seen() -> None:
    """#1015: ``first_seen_at`` is ranked as it stood before this read."""
    usages = Usages()
    await chooser(Reads(grouped(requirement()), status(slot(2))), [], usages=usages)(77)
    assert usages.calls == ["usage", "seen"]


async def test_two_filaments_on_one_group_send_one_entry() -> None:
    reads = Reads(
        grouped(requirement(1), requirement(2, color="#00B1B7")), status(slot(2), slot(3))
    )
    choice = await chooser(reads, [])(77)
    assert choice is not None and choice.nozzle_rack_choice == {"0": 2}


async def test_each_call_reads_the_rack_afresh() -> None:
    """A two-plate print: the second plate ranks a rack read after the first sliced."""
    reads = Reads(grouped(requirement(color="#FF6A13")), status(slot(2, color="FF6A13FF"), slot(3)))
    choose = chooser(reads, [])
    first = await choose(77)
    reads.rack = status(slot(2), slot(3, color="FF6A13FF"))
    second = await choose(78)
    assert first is not None and second is not None
    assert (first.nozzle_rack_choice, second.nozzle_rack_choice) == ({"0": 2}, {"0": 3})
    assert reads.status_reads == 2


async def test_a_plate_with_no_rack_group_reads_no_status_and_says_nothing() -> None:
    warnings: list[FilamentWarning] = []
    reads = Reads(grouped(requirement(on_rack=False)), RuntimeError("never read"))
    assert await chooser(reads, warnings)(77) is None
    assert (reads.status_reads, warnings) == (0, [])


async def test_a_manual_pick_on_a_plate_with_no_rack_group_is_unused() -> None:
    warnings: list[FilamentWarning] = []
    reads = Reads(grouped(requirement(on_rack=False)), status())
    assert await chooser(reads, warnings, manual=3)(77) is None
    assert [w.kind for w in warnings] == ["rack-left-to-bambuddy"]


@pytest.mark.parametrize(
    ("requirements", "rack", "reason"),
    [
        (ApiError(503, "down"), status(slot(2)), "requirements unreadable"),
        (grouped(requirement()), ApiError(503, "down"), "status unreadable"),
    ],
)
async def test_an_unreadable_read_sends_no_choice_and_says_why(
    requirements: FilamentRequirements | Exception, rack: PrinterStatus | Exception, reason: str
) -> None:
    warnings: list[FilamentWarning] = []
    assert await chooser(Reads(requirements, rack), warnings)(77) is None
    assert [(w.kind, w.message, w.slot_id) for w in warnings] == [
        ("rack-left-to-bambuddy", f"Rack pick left to Bambuddy: {reason}.", None)
    ]


async def test_a_partial_manual_pick_says_nothing_when_a_later_read_fails() -> None:
    """F8: the partial-manual note is added only once the pick is made; a status read
    that fails leaves one warning, the reason Bambuddy picks."""
    warnings: list[FilamentWarning] = []
    reads = Reads(
        grouped(requirement(1, group_id=0), requirement(2, group_id=1)), ApiError(503, "down")
    )
    assert await chooser(reads, warnings, manual=2)(77) is None
    assert [w.kind for w in warnings] == ["rack-left-to-bambuddy"]


async def test_a_partial_manual_pick_is_noted_once_the_pick_is_made() -> None:
    warnings: list[FilamentWarning] = []
    reads = Reads(
        grouped(requirement(1, group_id=0), requirement(2, group_id=1)),
        status(slot(2), slot(3)),
    )
    choice = await chooser(reads, warnings, manual=2)(77)
    assert choice is not None and choice.nozzle_rack_choice == {"0": 2, "1": 3}
    assert [w.kind for w in warnings] == ["rack-manual-partial"]


async def test_a_refused_multi_group_manual_pick_is_not_also_reported_as_given() -> None:
    """Final review minor 1: when the slice's group does not fit the manual position,
    the notes must not say both "group 0 got position 3" and "position 3 does not fit
    group 0"."""
    warnings: list[FilamentWarning] = []
    reads = Reads(
        grouped(requirement(1, group_id=0), requirement(2, group_id=1)),
        status(slot(2), slot(3, "HH01"), slot(4)),
    )
    choice = await chooser(reads, warnings, manual=3)(77)
    assert choice is not None and 3 not in choice.nozzle_rack_choice.values()
    [note] = warnings
    assert note.kind == "rack-manual-partial"
    assert note.message.startswith("Rack position 3 does not fit group 0")
    assert "got position" not in note.message


async def test_rank_rack_raising_queues_without_a_choice_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §5: the whole body is one ``try``; nothing escapes the callback."""

    def broken(*args: object, **kwargs: object) -> object:
        raise ZeroDivisionError

    monkeypatch.setattr(print_run, "rank_rack", broken)
    warnings: list[FilamentWarning] = []
    assert await chooser(Reads(grouped(requirement()), status(slot(2))), warnings)(77) is None
    assert [w.message for w in warnings] == ["Rack pick left to Bambuddy: rack pick failed."]


async def test_a_failure_is_logged_by_type_and_never_by_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec §4, §7: an error's own text can carry a serial, so only its type is logged."""
    leaked = serial(19)
    reads = Reads(grouped(requirement()), RuntimeError(f"hotend {leaked} unreadable"))
    with caplog.at_level(logging.WARNING, logger=print_run.__name__):
        assert await chooser(reads, [])(77) is None
    [record] = [r for r in caplog.records if r.name == print_run.__name__]
    assert record.getMessage() == "rack pick left to Bambuddy"
    assert getattr(record, "error", None) == "RuntimeError"
    assert record.exc_info is None
    assert leaked not in caplog.text


class UnreadableUsage(Usages):
    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]:
        raise RuntimeError(f"pool timeout near {serial(21)}")


@pytest.mark.parametrize(("manual", "sent"), [(2, {"0": 2}), (None, {"0": 4})])
async def test_an_unreadable_usage_ranks_without_it_and_keeps_a_hand_pick(
    caplog: pytest.LogCaptureFixture, manual: int | None, sent: dict[str, int]
) -> None:
    """claude-review on #1043, finding 2: usage only ranks, so a failed read ranks as if
    no hotend had printed (here, by color) and never drops a position picked by hand."""
    warnings: list[FilamentWarning] = []
    reads = Reads(grouped(requirement(color="#FF6A13")), status(slot(2), slot(4, color="FF6A13FF")))
    with caplog.at_level(logging.WARNING, logger=print_run.__name__):
        choice = await chooser(reads, warnings, usages=UnreadableUsage(), manual=manual)(77)
    assert choice is not None and choice.nozzle_rack_choice == sent
    assert "rack-left-to-bambuddy" not in [w.kind for w in warnings]
    [record] = [r for r in caplog.records if r.name == print_run.__name__]
    assert (record.getMessage(), getattr(record, "error", None)) == (
        "rack usage unreadable; ranked without it",
        "RuntimeError",
    )
    assert serial(21) not in caplog.text


async def test_a_rack_with_no_eligible_position_says_so() -> None:
    warnings: list[FilamentWarning] = []
    assert await chooser(Reads(grouped(requirement()), status()), warnings)(77) is None
    assert [w.message for w in warnings] == [
        "Rack pick left to Bambuddy: no eligible position for group 0."
    ]


async def test_let_bambuddy_pick_reads_no_usage_and_sends_nothing() -> None:
    usages = Usages()
    warnings: list[FilamentWarning] = []
    choice = await chooser(
        Reads(grouped(requirement()), status(slot(2))),
        warnings,
        usages=usages,
        algorithm="bambuddy",
    )(77)
    assert choice is None and usages.calls == ["seen"]
    assert warnings == []
