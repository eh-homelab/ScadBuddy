"""#4939: a native run of several prints keeps ``lan_require_plate_clear`` as its plan
read it (`print_activities.plate_clear_of`); any other run never waits for a clear."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.bambuddy.print_run import PlannedRun, PlatePlan
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.workflows.print_activities import plate_clear_of
from scadbuddy.workflows.printing import _ClearTimeoutError, _lan_failed


def planned(*, lan_start: bool = True, copies: int = 2, plates: int = 1) -> PlannedRun:
    return PlannedRun(
        library_file_id=1,
        printer_id=1,
        nozzle_size="0.4",
        copies=copies,
        options=PrintOptions(),
        plates=[PlatePlan(plate_id=plate) for plate in range(1, plates + 1)],
        path="native" if lan_start else "bambuddy",
        lan_start=lan_start,
    )


class Store:
    def __init__(self, required: bool) -> None:
        self.required = required
        self.reads = 0

    def snapshot(self) -> Any:
        self.reads += 1
        return SimpleNamespace(runtime=SimpleNamespace(lan_require_plate_clear=self.required))


@pytest.mark.parametrize(("copies", "plates"), [(2, 1), (1, 2)])
def test_a_native_run_of_several_prints_keeps_the_setting(copies: int, plates: int) -> None:
    assert plate_clear_of(
        planned(copies=copies, plates=plates), cast(SettingsStore, Store(True))
    ).plate_clear
    assert not plate_clear_of(
        planned(copies=copies, plates=plates), cast(SettingsStore, Store(False))
    ).plate_clear


def test_only_the_gates_own_timeout_reads_as_nobody_saying_the_plate_is_clear() -> None:
    """#4990 review: a `TimeoutError` from another step keeps that step's words."""
    gate = _lan_failed(_ClearTimeoutError(), "clear", 1, 2)
    assert gate.stage == "failed" and gate.message.startswith("Nobody said the plate was clear")
    other = _lan_failed(TimeoutError("slow"), "start", 1, 2)
    assert other.message.startswith("Plate 2 was not started")


def test_one_print_or_a_bambuddy_run_never_reads_it() -> None:
    store = Store(True)
    assert not plate_clear_of(planned(copies=1), cast(SettingsStore, store)).plate_clear
    assert not plate_clear_of(planned(lan_start=False), cast(SettingsStore, store)).plate_clear
    assert store.reads == 0
