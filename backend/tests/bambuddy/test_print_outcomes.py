"""A print's outcome, recorded once (#2665): the follow's settled read writes
``print_outcomes`` for the run that queued or started the print and publishes
``print.outcome`` in the same transaction, however many replicas read it settled."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from psycopg import Connection

from scadbuddy.bambuddy.follow import Follower
from scadbuddy.bambuddy.outcomes import PrintOutcomeStore, is_runs_print, outcome_of
from scadbuddy.bambuddy.output_reader import LocalOutputs
from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.progress import CopyProgress, PrintProgress, ProgressObserver
from scadbuddy.bambuddy.runs import PrintRun, PrintRunStore
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.events import Event, InProcessEventBus, PrintOutcome
from scadbuddy.flow_triggers.events import trigger_event
from scadbuddy.printers.lan_models import LanStarted
from tests.conftest import PgPool

SUBJECT = PrintSubject.library(7)
RESULT = PrintRunResult(library_file_id=7, copies=1, queue_item_ids=[51])
FAST = {"min_interval": 0.01, "max_interval": 0.02, "error_interval": 0.02}


class Events:
    """Records what is published inside a transaction."""

    def __init__(self) -> None:
        self.published: list[Event] = []

    def publish_in(self, conn: Connection[Any], event: Event) -> None:
        self.published.append(event)


def settled(stage: str, *, queue_item_id: int | None = 51) -> PrintProgress:
    return PrintProgress(
        route="slice_queue",
        stage=stage,  # type: ignore[arg-type]
        settled=True,
        queue_item_id=queue_item_id,
        bambuddy_url="http://bambuddy.test/queue",
    )


async def queued_run(pool: PgPool, run_id: str = "r1", result: PrintRunResult = RESULT) -> None:
    runs = PrintRunStore(pool)
    await runs.insert_accepted(
        run_id,
        subject=SUBJECT,
        key=f"k-{run_id}",
        slug="library-7",
        workflow_id=f"print-{run_id}",
        workflow_run_id="w1",
        retention=None,
    )
    await runs.succeed(run_id, "library-7", result)


def replica(store: PrintOutcomeStore, answer: PrintProgress) -> Follower:
    """One replica's follow of the library file's print, its read scripted."""

    async def read_library(subject: PrintSubject) -> PrintProgress:
        return answer

    async def unused(meta: Any) -> None:
        raise AssertionError("an output read")

    return Follower(
        outputs=LocalOutputs(None, None),  # type: ignore[arg-type]
        observer=ProgressObserver(InProcessEventBus()),
        read=unused,
        read_library=read_library,
        events=None,
        record_outcome=store.record,
        **FAST,  # type: ignore[arg-type]
    )


@pytest.mark.requires_postgres
async def test_one_print_settling_on_two_replicas_is_recorded_and_announced_once(
    pg_pool: PgPool,
) -> None:
    await queued_run(pg_pool)
    events = Events()
    replicas = [replica(PrintOutcomeStore(pg_pool, events), settled("done")) for _ in range(2)]
    ended = await asyncio.gather(*(r.follow(SUBJECT.run_subject, r.now()) for r in replicas))
    assert ended == ["settled", "settled"]
    assert events.published == [
        PrintOutcome(
            id=events.published[0].id,
            at=events.published[0].at,
            output_id="library:7",
            slug="library-7",
            run_id="r1",
            outcome="finished",
        )
    ]
    # A later read of the same settled print (a follow started again) adds nothing.
    await PrintOutcomeStore(pg_pool, events).record("library:7", "library-7", settled("done"))
    assert len(events.published) == 1
    with pg_pool.connection() as conn:
        rows = conn.execute("SELECT run_id, outcome FROM print_outcomes").fetchall()
    assert rows == [{"run_id": "r1", "outcome": "finished"}]


@pytest.mark.requires_postgres
async def test_a_failed_print_fires_print_failed_not_print_finished(pg_pool: PgPool) -> None:
    await queued_run(pg_pool)
    events = Events()
    recorded = await PrintOutcomeStore(pg_pool, events).record(
        "library:7", "library-7", settled("failed")
    )
    assert recorded is not None and recorded.outcome == "failed"
    found = trigger_event(recorded)
    assert found is not None
    kind, slug, key, facts = found
    assert (kind, slug, key) == ("print.failed", "library-7", "print:r1")
    assert facts == {
        "kind": "print.failed",
        "slug": "library-7",
        "run_id": "r1",
        "output_id": "library:7",
        "printer": None,
    }


@pytest.mark.requires_postgres
async def test_a_read_of_another_print_than_the_newest_runs_records_nothing(
    pg_pool: PgPool,
) -> None:
    """A newer print queued between the read and the record: the read names the older
    print's queue item, which is not the newest run's, so nothing is pinned on it."""
    await queued_run(pg_pool, "r1")
    await queued_run(pg_pool, "r2", RESULT.model_copy(update={"queue_item_ids": [52]}))
    events = Events()
    store = PrintOutcomeStore(pg_pool, events)
    assert await store.record("library:7", "library-7", settled("done", queue_item_id=51)) is None
    assert events.published == []
    recorded = await store.record("library:7", "library-7", settled("done", queue_item_id=52))
    assert recorded is not None and recorded.run_id == "r2"


@pytest.mark.requires_postgres
async def test_a_print_with_no_run_on_record_records_nothing(pg_pool: PgPool) -> None:
    events = Events()
    store = PrintOutcomeStore(pg_pool, events)
    assert await store.record("library:7", "library-7", settled("done")) is None
    assert events.published == []


@pytest.mark.parametrize(
    ("stage", "outcome", "kind"),
    [
        ("done", "finished", "print.finished"),
        ("failed", "failed", "print.failed"),
        ("cancelled", "cancelled", None),
        ("unknown", "unknown", None),
    ],
)
def test_each_outcome_and_the_trigger_event_it_fires(
    stage: str, outcome: str, kind: str | None
) -> None:
    """A cancelled print fires neither (Ruling 24), nor one whose end is unknown."""
    assert outcome_of(settled(stage)) == outcome
    event = PrintOutcome(output_id="o", slug="demo", run_id="r", outcome=outcome)
    found = trigger_event(event)
    assert (found[0] if found else None) == kind


def _run(result: PrintRunResult) -> PrintRun:
    return PrintRun.model_validate(
        {
            "id": "r1",
            "subject": "output:o",
            "status": "succeeded",
            "created_at": "2026-10-24T00:00:00Z",
            "result": result,
        }
    )


def test_a_lan_print_is_its_runs_when_the_run_started_one_over_the_lan() -> None:
    lan = PrintProgress(route="lan", stage="done", settled=True, bambuddy_url="")
    started = LanStarted(subtask_name="s", started_at=datetime(2026, 10, 24, tzinfo=UTC))
    assert is_runs_print(_run(RESULT.model_copy(update={"lan": [started]})), lan)
    assert not is_runs_print(_run(RESULT), lan)


def test_a_multi_plate_read_is_its_runs_by_any_plates_queue_item() -> None:
    plates = settled("failed", queue_item_id=None).model_copy(
        update={"copies_detail": [CopyProgress(queue_entry_id=60), CopyProgress(queue_entry_id=61)]}
    )
    assert is_runs_print(_run(RESULT.model_copy(update={"queue_item_ids": [61]})), plates)
    assert not is_runs_print(_run(RESULT), plates)
