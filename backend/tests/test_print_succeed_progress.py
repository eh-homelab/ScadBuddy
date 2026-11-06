"""``print_succeed`` marks the subject's progress new before it records the run's end
(#5521). A reader that sees the run ended reads the new print, and the observer keeps
that read; marking the progress new after the end dropped it, and the next read, which
found nothing new, published ``print.progress`` again."""

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
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from scadbuddy.workflows.print_models import PrintRunInput, SourceSpec, SucceedInput

OUTPUT = "a" * 32
AT = datetime(2026, 11, 6, 12, tzinfo=UTC)
#: What a read finds once the run is recorded: its queued print.
QUEUED = PrintProgress(route="slice_queue", stage="queued", queue_item_id=51, bambuddy_url="")


class Outputs:
    async def get(self, output_id: str) -> Any:
        return SimpleNamespace(id=output_id, slug="demo")


class Runs:
    """Records the run's end; a progress read then sees it ended, at once."""

    def __init__(self, observer: ProgressObserver, key: str) -> None:
        self.observer = observer
        self.key = key

    async def succeed(self, run_id: str, slug: str, result: PrintRunResult) -> PrintRun:
        self.observer.observe_subject(self.key, slug, QUEUED)
        return PrintRun(
            id=run_id, subject=f"output:{OUTPUT}", status="succeeded", created_at=AT, result=result
        )


def _input(kind: str) -> SucceedInput:
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
        result=PrintRunResult(library_file_id=41, copies=1, queue_item_ids=[51]),
    )


@pytest.mark.parametrize(("kind", "key"), [("output", OUTPUT), ("library", "library:7")])
async def test_a_read_after_the_run_ended_is_not_published_again(kind: str, key: str) -> None:
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
            store=cast(PrintRunStore, Runs(observer, key)),
            observer=observer,
        )
    )

    await ActivityEnvironment().run(activities.succeed, _input(kind))

    # The run's start, then the read that saw it ended.
    assert [event.kind for event in seen] == ["print.progress"] * 2
    # The next read finds nothing new, so it publishes nothing.
    assert not observer.observe_subject(key, "demo", QUEUED)
    assert len(seen) == 2
