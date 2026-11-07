"""Each print run followed and settled on its own (#5613).

Two prints of one library file at once (two order items of the file, or the file on two
orders) share the run subject ``library:<file id>``. The subject's follow reads its
newest run, as before, and also each older run still open by that run's own sends or LAN
start (`Follower.read_run`, `progress.run_reader`), and records each settled one against
its run (`PrintOutcomeStore.record_run`). These tests drive the follow over the real
stores: Bambuddy's queue through respx, the LAN route through the printers' reports."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.follow import Follower
from scadbuddy.bambuddy.outcomes import OPEN_RUN_WINDOW, PrintOutcomeStore
from scadbuddy.bambuddy.output_reader import LocalOutputs
from scadbuddy.bambuddy.print_links import PrintLinkStore, PrintSend
from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.progress import (
    PrintProgress,
    ProgressObserver,
    library_progress,
    run_reader,
)
from scadbuddy.bambuddy.runs import PrintRun, PrintRunStore
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.events import InProcessEventBus
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.printers.lan_models import LanStarted
from scadbuddy.printers.progress import LanProgress
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool

SUBJECT = PrintSubject.library(7)
SLUG = "library-7"
FAST = {"min_interval": 0.01, "max_interval": 0.02, "error_interval": 0.02}
CONFIG = BambuddyConfig(base_url="http://bambuddy.test", api_key="bb_test")

pytestmark = pytest.mark.requires_postgres


async def succeeded(
    pool: PgPool,
    run_id: str,
    result: PrintRunResult,
    *,
    subject: PrintSubject = SUBJECT,
    printer: str | None = None,
) -> None:
    runs = PrintRunStore(pool)
    await runs.insert_accepted(
        run_id,
        subject=subject,
        key=f"k-{run_id}",
        slug=SLUG,
        workflow_id=f"print-{run_id}",
        workflow_run_id="w1",
        retention=None,
        printer=printer,
    )
    await runs.succeed(run_id, SLUG, result)


def recorded(pool: PgPool) -> dict[str, str]:
    with pool.connection() as conn:
        rows = conn.execute("SELECT run_id, outcome FROM print_outcomes").fetchall()
    return {row["run_id"]: row["outcome"] for row in rows}


class NoLan:
    async def read(self, run_subject: str) -> None:
        return None

    async def read_run(self, run: PrintRun) -> None:
        return None


async def unused(meta: OutputMeta) -> None:
    raise AssertionError("an output read")


def follower(
    pool: PgPool,
    read_library: Callable[[PrintSubject], Awaitable[PrintProgress | None]],
    read_run: Callable[[PrintSubject, PrintRun], Awaitable[PrintProgress | None]],
) -> Follower:
    outcomes = PrintOutcomeStore(pool, None)
    return Follower(
        outputs=LocalOutputs(None, None),  # type: ignore[arg-type]
        observer=ProgressObserver(InProcessEventBus()),
        read=unused,
        read_library=read_library,
        events=None,
        record_outcome=outcomes.record,
        older_runs=outcomes,
        read_run=read_run,
        **FAST,  # type: ignore[arg-type]
    )


async def until(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(10):
        while not check():
            await asyncio.sleep(0.01)


@respx.mock
async def test_two_runs_of_one_library_file_on_bambuddy_both_settle(pg_pool: PgPool) -> None:
    """The newer print settles first: the follow records it and stays open for the older
    one, which it reads by its own sends and records against its own run."""
    links = PrintLinkStore(pg_pool)
    for run_id, item in (("r-older", 51), ("r-newer", 52)):
        await succeeded(
            pg_pool, run_id, PrintRunResult(library_file_id=7, copies=1, queue_item_ids=[item])
        )
        await links.record_sends(SUBJECT, [PrintSend(queue_item_id=item, run_id=run_id)])
    status = {51: "printing", 52: "printing"}

    def queue_item(request: httpx.Request) -> httpx.Response:
        item = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json={"id": item, "status": status[item]})

    respx.get(url__regex=r"http://bambuddy\.test/api/v1/queue/\d+").mock(side_effect=queue_item)

    async def read_library(subject: PrintSubject) -> PrintProgress | None:
        async with BambuddyClient(CONFIG) as client:
            return await library_progress(client, subject, links)

    follow = follower(
        pg_pool, read_library, run_reader(NoLan(), lambda: BambuddyClient(CONFIG), links)
    )
    following = asyncio.create_task(follow.follow(SUBJECT.run_subject, datetime.now(UTC)))
    status[52] = "completed"
    await until(lambda: "r-newer" in recorded(pg_pool))
    await asyncio.sleep(0.1)
    assert not following.done()
    assert recorded(pg_pool) == {"r-newer": "finished"}
    status[51] = "failed"
    assert await asyncio.wait_for(following, 10) == "settled"
    assert recorded(pg_pool) == {"r-newer": "finished", "r-older": "failed"}


class Reports:
    """The printers' latest reports, by printer id, set by the test."""

    def __init__(self) -> None:
        self.by_printer: dict[str, tuple[dict[str, Any], datetime]] = {}

    def get(self, printer_id: str) -> tuple[dict[str, Any], datetime] | None:
        return self.by_printer.get(printer_id)


async def test_two_runs_of_one_library_file_over_the_lan_both_settle(pg_pool: PgPool) -> None:
    """The same over the LAN, two printers: the older print, still running when the newer
    one finished, is read from its own printer's report and recorded against its run."""
    printers = [str(uuid.uuid4()), str(uuid.uuid4())]
    with pg_pool.connection() as conn:
        for index, printer in enumerate(printers):
            conn.execute(
                "INSERT INTO printers (id, serial, name) VALUES (%s, %s, %s)",
                (printer, f"S{index}", f"P{index}"),
            )
    started_at = datetime.now(UTC) - timedelta(minutes=5)
    names = {"r-older": "scadbuddy-older-p1", "r-newer": "scadbuddy-newer-p1"}
    for (run_id, name), printer in zip(names.items(), printers, strict=True):
        started = LanStarted(subtask_name=name, started_at=started_at)
        result = PrintRunResult(library_file_id=7, copies=1, path="native", lan=[started])
        await succeeded(pg_pool, run_id, result, printer=printer)
    reports = Reports()

    def report(printer: str, run_id: str, state: str) -> None:
        reports.by_printer[printer] = (
            {"subtask_name": names[run_id], "gcode_state": state},
            datetime.now(UTC),
        )

    report(printers[0], "r-older", "RUNNING")
    report(printers[1], "r-newer", "RUNNING")
    lan = LanProgress(
        PrintRunStore(pg_pool).latest_for_output,
        PrinterStore(pg_pool, None),
        reports,  # type: ignore[arg-type]
    )

    async def read_library(subject: PrintSubject) -> PrintProgress | None:
        return await lan.read(subject.run_subject)

    def no_client() -> BambuddyClient:
        raise AssertionError("a LAN read builds no Bambuddy client")

    links = PrintLinkStore(pg_pool)
    follow = follower(pg_pool, read_library, run_reader(lan, no_client, links))
    following = asyncio.create_task(follow.follow(SUBJECT.run_subject, datetime.now(UTC)))
    report(printers[1], "r-newer", "FINISH")
    await until(lambda: "r-newer" in recorded(pg_pool))
    await asyncio.sleep(0.1)
    assert not following.done()
    report(printers[0], "r-older", "FINISH")
    assert await asyncio.wait_for(following, 10) == "settled"
    assert recorded(pg_pool) == {"r-newer": "finished", "r-older": "finished"}


async def test_a_single_run_is_followed_as_before(pg_pool: PgPool) -> None:
    """One run of the subject: no older run is read, and the follow ends at its settle."""
    await succeeded(pg_pool, "r1", PrintRunResult(library_file_id=7, copies=1, queue_item_ids=[51]))
    done = PrintProgress(
        route="slice_queue",
        stage="done",
        settled=True,
        queue_item_id=51,
        bambuddy_url="http://bambuddy.test/queue",
    )

    async def read_library(subject: PrintSubject) -> PrintProgress:
        return done

    async def read_run(subject: PrintSubject, run: PrintRun) -> None:
        raise AssertionError("an older run read")

    follow = follower(pg_pool, read_library, read_run)
    assert await asyncio.wait_for(follow.follow(SUBJECT.run_subject, datetime.now(UTC)), 10) == (
        "settled"
    )
    assert recorded(pg_pool) == {"r1": "finished"}


async def test_an_output_print_is_followed_as_before(pg_pool: PgPool) -> None:
    """An output's print, one run: read by its record, recorded once, no older run."""
    output = PrintSubject.output("c" * 32)
    await succeeded(
        pg_pool,
        "r1",
        PrintRunResult(library_file_id=7, copies=1, queue_item_ids=[51]),
        subject=output,
    )
    outcomes = PrintOutcomeStore(pg_pool, None)
    assert await outcomes.older_open(output.run_subject) == []
    done = PrintProgress(
        route="slice_queue",
        stage="done",
        settled=True,
        queue_item_id=51,
        bambuddy_url="http://bambuddy.test/queue",
    )
    assert (await outcomes.record(output.run_subject, "demo", done)) is not None
    assert recorded(pg_pool) == {"r1": "finished"}


async def test_older_open_is_each_older_run_without_an_outcome_in_the_window(
    pg_pool: PgPool,
) -> None:
    result = PrintRunResult(library_file_id=7, copies=1, queue_item_ids=[51])
    for run_id in ("r-stale", "r-settled", "r-open", "r-newest"):
        await succeeded(pg_pool, run_id, result)
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE print_runs SET created_at = now() - %s WHERE id = 'r-stale'",
            (OPEN_RUN_WINDOW + timedelta(hours=1),),
        )
        conn.execute(
            "INSERT INTO print_outcomes (run_id, subject, slug, outcome)"
            " VALUES ('r-settled', %s, %s, 'finished')",
            (SUBJECT.run_subject, SLUG),
        )
    outcomes = PrintOutcomeStore(pg_pool, None)
    assert [run.id for run in await outcomes.older_open(SUBJECT.run_subject)] == ["r-open"]


async def test_record_run_records_only_that_runs_print(pg_pool: PgPool) -> None:
    for run_id, item in (("r-older", 51), ("r-newer", 52)):
        await succeeded(
            pg_pool, run_id, PrintRunResult(library_file_id=7, copies=1, queue_item_ids=[item])
        )
    outcomes = PrintOutcomeStore(pg_pool, None)
    (older,) = await outcomes.older_open(SUBJECT.run_subject)

    def done(item: int) -> PrintProgress:
        return PrintProgress(
            route="slice_queue",
            stage="done",
            settled=True,
            queue_item_id=item,
            bambuddy_url="http://bambuddy.test/queue",
        )

    assert await outcomes.record_run(older, SLUG, done(52)) is None
    event = await outcomes.record_run(older, SLUG, done(51))
    assert event is not None and event.run_id == "r-older"
    assert await outcomes.record_run(older, SLUG, done(51)) is None
    assert await outcomes.older_open(SUBJECT.run_subject) == []
