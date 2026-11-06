"""``print_succeed`` and the progress observer around a run's end (#5521).

``print_succeed`` records the run's end, then marks the subject's progress new
(`ProgressObserver.started`) with the print's ids: its run and its queue items. A progress
read can land while it does. A read of the new print is kept, so the next read, which
finds nothing new, publishes nothing. A read of the previous print, as a LAN run's
subject finds until its end is recorded, is forgotten, and the new print's reads are
published as new."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.print_run import PrintRunRequest, PrintRunResult
from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices
from scadbuddy.bambuddy.runs import PrintRun, PrintRunStore
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.events import Event, InProcessEventBus
from scadbuddy.library.output_prints import OutputPrintStore
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.printers.lan_models import LanStarted
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from scadbuddy.workflows.print_models import PrintRunInput, SourceSpec, SucceedInput

OUTPUT = "a" * 32
AT = datetime(2026, 11, 6, 12, tzinfo=UTC)
#: What a read finds once a queued run's end is recorded: its queued print.
QUEUED = PrintProgress(route="slice_queue", stage="queued", queue_item_id=51, bambuddy_url="")
#: The subject's previous print, settled: what a LAN run's subject reads until the run's
#: end is recorded (`LanProgress.read` is None, and the route reads the last queue item).
PREVIOUS = PrintProgress(
    route="slice_queue", stage="done", settled=True, queue_item_id=50, bambuddy_url=""
)
#: The new LAN print, read once its run's end is recorded.
LAN = PrintProgress(route="lan", stage="running", run_id="run-1", bambuddy_url="")
LAN_DONE = LAN.model_copy(update={"stage": "done", "settled": True})


class Outputs:
    async def get(self, output_id: str) -> Any:
        return SimpleNamespace(id=output_id, slug="demo")


class Runs:
    """Records the run's end; a progress read of ``reads`` lands while it does."""

    def __init__(self, observer: ProgressObserver, key: str, reads: PrintProgress) -> None:
        self.observer = observer
        self.key = key
        self.reads = reads
        self.calls = 0

    async def succeed(self, run_id: str, slug: str, result: PrintRunResult) -> PrintRun:
        self.calls += 1
        self.observer.observe_subject(self.key, slug, self.reads)
        return PrintRun(
            id=run_id, subject=f"output:{OUTPUT}", status="succeeded", created_at=AT, result=result
        )


def _input(kind: str, result: PrintRunResult) -> SucceedInput:
    source = (
        SourceSpec(kind="output", output_id=OUTPUT)
        if kind == "output"
        else SourceSpec(kind="library", file_id=7)
    )
    return SucceedInput(
        input=PrintRunInput(
            subject=OUTPUT if kind == "output" else "library:7",
            slug="demo",
            key="k",
            source=source,
            request=PrintRunRequest(
                filament_plan=FilamentPlan(slots=[]),
                choices=PrintChoices(nozzles=[NozzleChoice(size="0.4")]),
            ),
        ),
        run_id="run-1",
        result=result,
    )


def _setup(key: str, reads: PrintProgress) -> tuple[PrintActivities, ProgressObserver, list[Event]]:
    bus = InProcessEventBus()
    seen: list[Event] = []
    bus.add_listener(seen.append)
    observer = ProgressObserver(bus)
    activities = PrintActivities(
        PrintDeps(
            settings_store=cast(SettingsStore, None),
            outputs=cast(Any, Outputs()),
            prints=cast(OutputPrintStore, None),
            uploads=cast(BambuddyUploadStore, None),
            store=cast(PrintRunStore, Runs(observer, key, reads)),
            observer=observer,
        )
    )
    return activities, observer, seen


@pytest.mark.parametrize(("kind", "key"), [("output", OUTPUT), ("library", "library:7")])
async def test_a_read_of_the_new_print_is_not_published_again(kind: str, key: str) -> None:
    activities, observer, seen = _setup(key, QUEUED)
    result = PrintRunResult(library_file_id=41, copies=1, queue_item_ids=[51])

    await ActivityEnvironment().run(activities.succeed, _input(kind, result))

    # The read that saw the new print, then the run's start.
    assert [event.kind for event in seen] == ["print.progress"] * 2
    # The next read finds nothing new, so it publishes nothing.
    assert not observer.observe_subject(key, "demo", QUEUED)
    assert len(seen) == 2


async def test_a_lan_run_is_not_reported_settled_by_its_previous_print() -> None:
    activities, observer, seen = _setup(OUTPUT, PREVIOUS)
    observer.observe_subject(OUTPUT, "demo", PREVIOUS)  # the panel read it before the run
    seen.clear()
    result = PrintRunResult(
        library_file_id=41,
        copies=1,
        path="native",
        lan=[LanStarted(subtask_name="scadbuddy-run-1-p1", started_at=AT, plate_id=1)],
    )

    await ActivityEnvironment().run(activities.succeed, _input("output", result))
    # The run's start makes the panel read again: the new print, then it ending.
    observer.observe_subject(OUTPUT, "demo", LAN)
    observer.observe_subject(OUTPUT, "demo", LAN_DONE)

    assert [event.kind for event in seen] == [
        "print.progress",  # the run's start
        "print.progress",  # the new print
        "print.progress",  # it ended
        "print.settled",
    ]


async def test_a_fresh_process_still_reports_the_new_print_settled() -> None:
    """Nothing was read of the subject before the run: the read of the previous print
    is its first, and is forgotten once the new print starts, so the new print's
    ``print.settled`` is published even when its first read finds it settled."""
    activities, observer, seen = _setup(OUTPUT, PREVIOUS)
    result = PrintRunResult(
        library_file_id=41,
        copies=1,
        path="native",
        lan=[LanStarted(subtask_name="scadbuddy-run-1-p1", started_at=AT, plate_id=1)],
    )

    await ActivityEnvironment().run(activities.succeed, _input("output", result))
    observer.observe_subject(OUTPUT, "demo", LAN_DONE)

    assert [event.kind for event in seen] == [
        "print.progress",  # the previous print, read first
        "print.settled",
        "print.progress",  # the run's start
        "print.progress",  # the new print, ended
        "print.settled",
    ]


async def test_a_retried_end_marks_the_print_new_once() -> None:
    """``print_succeed`` is retried until the end is recorded: the attempts that fail
    publish nothing and forget nothing."""
    activities, observer, seen = _setup(OUTPUT, QUEUED)
    runs = cast(Runs, activities.d.store)
    real = runs.succeed

    async def flaky(run_id: str, slug: str, result: PrintRunResult) -> PrintRun:
        if runs.calls < 2:
            runs.calls += 1
            raise ConnectionError("Postgres blipped")
        return await real(run_id, slug, result)

    runs.succeed = flaky  # type: ignore[method-assign]
    result = PrintRunResult(library_file_id=41, copies=1, queue_item_ids=[51])
    for _ in range(2):
        with pytest.raises(ConnectionError):
            await ActivityEnvironment().run(activities.succeed, _input("output", result))
    assert seen == []

    await ActivityEnvironment().run(activities.succeed, _input("output", result))

    assert [event.kind for event in seen] == ["print.progress"] * 2
    assert not observer.observe_subject(OUTPUT, "demo", QUEUED)
