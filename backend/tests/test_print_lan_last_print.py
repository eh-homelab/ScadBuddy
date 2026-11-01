"""``print_succeed`` records a LAN print's last print (#4644): the run, the printer, and
each plate it started or still waits to start with its copies, since a LAN start has no
queue item for ``print_record`` to record."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.print_run import PrintRunRequest, PrintRunResult
from scadbuddy.bambuddy.progress import ProgressObserver
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices
from scadbuddy.bambuddy.runs import PrintRun, PrintRunStore
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.library.output_prints import OutputPrintStore
from scadbuddy.library.outputs import LanPlateSend
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.printers.lan_models import LanStarted, LanStopped
from scadbuddy.workflows.print_activities import PrintActivities, PrintDeps
from scadbuddy.workflows.print_models import (
    LanUpdateInput,
    PrintRunInput,
    SourceSpec,
    SucceedInput,
)

OUTPUT = "a" * 32
PRINTER = "0b2c6f0e-8d1a-4c35-9b8e-2f6d4a1c7e90"
AT = datetime(2026, 11, 1, 12, tzinfo=UTC)


class Runs:
    async def succeed(self, run_id: str, slug: str, result: PrintRunResult) -> PrintRun:
        return PrintRun(
            id=run_id,
            subject=f"output:{OUTPUT}",
            status="succeeded",
            created_at=AT,
            result=result,
            printer=PRINTER,
        )

    async def update_result(self, run_id: str, slug: str, result: PrintRunResult) -> PrintRun:
        return await self.succeed(run_id, slug, result)


class Prints:
    def __init__(self, *, fail: bool = False) -> None:
        self.lan: list[tuple[str, str, str | None, list[LanPlateSend]]] = []
        self.fail = fail

    def record_lan(
        self, output_id: str, *, run_id: str, printer: str | None, plates: list[LanPlateSend]
    ) -> None:
        if self.fail:
            raise psycopg.OperationalError("the database is gone")
        self.lan.append((output_id, run_id, printer, plates))


class Observer:
    def started(self, meta: object) -> None:
        pass

    def started_subject(self, key: str, slug: str) -> None:
        pass


def _activities(prints: Prints) -> PrintActivities:
    return PrintActivities(
        PrintDeps(
            settings_store=cast(SettingsStore, None),
            outputs=cast(Any, None),
            prints=cast(OutputPrintStore, prints),
            uploads=cast(BambuddyUploadStore, None),
            store=cast(PrintRunStore, Runs()),
            observer=cast(ProgressObserver, Observer()),
        )
    )


def _input(result: PrintRunResult, *, kind: str = "output") -> SucceedInput:
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


def _started(plate_id: int | None) -> LanStarted:
    return LanStarted(subtask_name="scadbuddy-run-1-p1", started_at=AT, plate_id=plate_id)


async def test_a_lan_run_records_its_last_print() -> None:
    """Two plates of two copies each: the first started, the other three to come."""
    prints = Prints()
    result = PrintRunResult(
        library_file_id=41,
        copies=2,
        path="native",
        lan=[_started(1)],
        lan_waiting=[1, 3, 3],
    )
    await ActivityEnvironment().run(_activities(prints).succeed, _input(result))
    assert prints.lan == [
        (
            OUTPUT,
            "run-1",
            PRINTER,
            [LanPlateSend(plate_id=1, copies=2), LanPlateSend(plate_id=3, copies=2)],
        )
    ]


async def test_a_start_from_before_it_named_its_plate_is_plate_one() -> None:
    """A run of one plate started by an older build: its start names no plate."""
    prints = Prints()
    result = PrintRunResult(library_file_id=41, copies=1, path="native", lan=[_started(None)])
    await ActivityEnvironment().run(_activities(prints).succeed, _input(result))
    [(_, _, _, plates)] = prints.lan
    assert plates == [LanPlateSend(plate_id=1, copies=1)]


@pytest.mark.parametrize(
    ("result", "kind"),
    [
        (PrintRunResult(library_file_id=41, copies=1, queue_item_ids=[5]), "output"),
        (
            PrintRunResult(library_file_id=41, copies=1, path="native", lan=[_started(1)]),
            "library",
        ),
    ],
    ids=["a Bambuddy run", "a library file"],
)
async def test_anything_else_records_no_lan_last_print(result: PrintRunResult, kind: str) -> None:
    prints = Prints()
    await ActivityEnvironment().run(_activities(prints).succeed, _input(result, kind=kind))
    assert prints.lan == []


async def test_a_later_start_and_a_stop_rewrite_the_last_print() -> None:
    """The run's later starts update its result (`print_lan_update`), and the last print
    with it: a stop drops the copies it will never start."""
    prints = Prints()
    started = PrintRunResult(
        library_file_id=41,
        copies=2,
        path="native",
        lan=[_started(1), _started(1)],
        lan_waiting=[3, 3],
    )
    stopped = started.model_copy(
        update={"lan_stopped": LanStopped(stage="cancelled", message="Stopped.")}
    )
    activities = _activities(prints)
    for result in (started, stopped):
        await ActivityEnvironment().run(
            activities.lan_update, LanUpdateInput(run_id="run-1", slug="demo", result=result)
        )
    assert [plates for _, _, _, plates in prints.lan] == [
        [LanPlateSend(plate_id=1, copies=2), LanPlateSend(plate_id=3, copies=2)],
        [LanPlateSend(plate_id=1, copies=2)],
    ]


async def test_a_last_print_that_cannot_be_recorded_never_fails_the_run() -> None:
    result = PrintRunResult(library_file_id=41, copies=1, path="native", lan=[_started(1)])
    run = await ActivityEnvironment().run(_activities(Prints(fail=True)).succeed, _input(result))
    assert run.status == "succeeded"
