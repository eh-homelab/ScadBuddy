"""The controls of a native run's prints still to start: stop the rest (#4942), and say
the plate is clear (#4939). Each route reads the run's record, then sends its
``PrintLanSequence`` a cancel or the ``plate_cleared`` signal; the Prints page card
names the run whose controls they are.

The sequence here is a stand-in execution of the same id on a queue no worker polls:
the routes only send to it, and what they sent is in its history."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle
from temporalio.service import RPCError

from scadbuddy import main as app_main
from scadbuddy.api import printing as printing_api
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.printers.component import PRINTERS, Printers
from scadbuddy.printers.lan_models import LanQueued, LanStarted, LanStopped
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.workflows.print_models import (
    LAN_SEQUENCE_WORKFLOW,
    PLATE_CLEARED_SIGNAL,
    lan_sequence_id,
)
from tests.printers.conftest import KEK
from tests.support.temporal import WorkflowReaper

pytestmark = pytest.mark.requires_postgres

STARTED = LanStarted(subtask_name="scadbuddy-run-p1", started_at=datetime.now(UTC), plate_id=1)
WAITING = PrintRunResult(
    library_file_id=41, copies=3, path="native", lan=[STARTED], lan_waiting=[1, 1]
)


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def _printer(client: TestClient) -> str:
    printers: Printers = _state(client).components.get(PRINTERS)
    printers.store.kek = KEK
    row = printers.store.create(PrinterCreate(serial="01P00A000000001", name="Garage"))
    printers.store.link_lan(
        row.id, LanLink(host="10.0.0.9", access_code="12345678"), cert_sha256="ab" * 32
    )
    return row.id


def _run(
    client: TestClient,
    reaper: WorkflowReaper,
    result: PrintRunResult = WAITING,
    *,
    printer: str | None = None,
) -> str:
    """A succeeded run with ``result``, as its ``PrintRun`` records one."""
    store = _state(client).print_runs.store
    run_id = uuid.uuid4().hex
    reaper.run(
        store.insert_accepted(
            run_id,
            subject=PrintSubject.output("o" * 32),
            key=run_id,
            slug="demo",
            workflow_id=f"print-{run_id}",
            workflow_run_id="w",
            retention=None,
            printer=printer,
        )
    )
    reaper.run(store.succeed(run_id, "demo", result))
    return run_id


@pytest.fixture
def sequences(workflow_reaper: WorkflowReaper) -> Iterator[list[WorkflowHandle[Any, Any]]]:
    """Each stand-in sequence a test starts, terminated after it."""
    started: list[WorkflowHandle[Any, Any]] = []
    yield started
    for handle in started:
        # A test may have closed it already.
        with contextlib.suppress(RPCError):
            workflow_reaper.run(handle.terminate())


def _sequence(
    reaper: WorkflowReaper, run_id: str, started: list[WorkflowHandle[Any, Any]]
) -> WorkflowHandle[Any, Any]:
    assert reaper.client is not None
    handle = reaper.run(
        reaper.client.start_workflow(
            LAN_SEQUENCE_WORKFLOW,
            "stand-in",
            id=lan_sequence_id(run_id),
            task_queue=f"nobody-{uuid.uuid4().hex[:8]}",
        )
    )
    started.append(handle)
    return handle


def _events(reaper: WorkflowReaper, handle: WorkflowHandle[Any, Any]) -> list[Any]:
    return list(reaper.run(handle.fetch_history()).events)


def test_stop_cancels_the_runs_sequence(
    client: TestClient, workflow_reaper: WorkflowReaper, sequences: list[Any]
) -> None:
    run_id = _run(client, workflow_reaper)
    handle = _sequence(workflow_reaper, run_id, sequences)

    response = client.post(f"/api/v1/print/runs/{run_id}/stop")

    assert response.status_code == 202, response.text
    assert response.json()["id"] == run_id
    kinds = [event.event_type for event in _events(workflow_reaper, handle)]
    assert EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CANCEL_REQUESTED in kinds


@pytest.mark.parametrize(
    "result",
    [
        PrintRunResult(library_file_id=41, copies=1, path="native", lan=[STARTED]),
        WAITING.model_copy(
            update={"lan_stopped": LanStopped(stage="cancelled", message="Stopped.")}
        ),
        WAITING.model_copy(update={"path": "bambuddy"}),
    ],
    ids=["all-started", "stopped", "bambuddy"],
)
def test_stop_with_nothing_waiting_is_a_409(
    client: TestClient, workflow_reaper: WorkflowReaper, result: PrintRunResult
) -> None:
    run_id = _run(client, workflow_reaper, result)
    response = client.post(f"/api/v1/print/runs/{run_id}/stop")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == printing_api.NOTHING_WAITING


def test_stop_of_an_unknown_run_is_a_404(client: TestClient) -> None:
    assert client.post(f"/api/v1/print/runs/{uuid.uuid4().hex}/stop").status_code == 404


def test_a_sequence_that_closed_records_the_rest_stopped(
    client: TestClient, workflow_reaper: WorkflowReaper, sequences: list[Any]
) -> None:
    """A sequence that ended without recording it (terminated, say), or none at all:
    nothing will start the rest, so the run records them stopped (#4990 review), and its
    controls go. A stop is then done; a plate-cleared is the 409 that says why."""
    awaiting = WAITING.model_copy(update={"lan_awaiting_clear": True})
    run_id = _run(client, workflow_reaper, awaiting)
    handle = _sequence(workflow_reaper, run_id, sequences)
    workflow_reaper.run(handle.terminate())
    response = client.post(f"/api/v1/print/runs/{run_id}/plate-cleared")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == printing_api.SEQUENCE_GONE
    recorded = client.get(f"/api/v1/print/runs/{run_id}").json()["result"]
    assert recorded["lan_stopped"] == printing_api.SEQUENCE_GONE_STOP.model_dump()
    assert recorded["lan_awaiting_clear"] is False
    assert client.post(f"/api/v1/print/runs/{run_id}/stop").status_code == 409

    unstarted = _run(client, workflow_reaper, WAITING)
    response = client.post(f"/api/v1/print/runs/{unstarted}/stop")
    assert response.status_code == 202, response.text
    assert response.json()["result"]["lan_stopped"] == printing_api.SEQUENCE_GONE_STOP.model_dump()


def test_a_gone_sequence_stops_using_its_runs_files(
    client: TestClient, workflow_reaper: WorkflowReaper, sequences: list[Any]
) -> None:
    """#4337: before the per-print copies' sweep reads which subjects are in use, a run
    whose sequence is gone (terminated, or never started) records the rest stopped, so
    its subject's files are not kept for good. A run whose sequence is running keeps
    them, and so does one whose own ``PrintRun`` execution is still open: it starts its
    sequence only after it records the run succeeded."""
    state = _state(client)
    gone = _run(client, workflow_reaper)
    never = _run(client, workflow_reaper)
    alive = _run(client, workflow_reaper)
    starting = _run(client, workflow_reaper)
    workflow_reaper.run(_sequence(workflow_reaper, gone, sequences).terminate())
    _sequence(workflow_reaper, alive, sequences)
    assert workflow_reaper.client is not None
    parent = workflow_reaper.run(
        workflow_reaper.client.start_workflow(
            "PrintRun", "stand-in", id=f"print-{starting}", task_queue=f"nobody-{starting[:8]}"
        )
    )
    sequences.append(parent)

    workflow_reaper.run(printing_api.end_gone_sequences(state.print_runs))

    stopped = printing_api.SEQUENCE_GONE_STOP.model_dump()
    for run_id in (gone, never):
        assert client.get(f"/api/v1/print/runs/{run_id}").json()["result"]["lan_stopped"] == stopped
    for run_id in (alive, starting):
        assert client.get(f"/api/v1/print/runs/{run_id}").json()["result"]["lan_stopped"] is None
    waiting = workflow_reaper.run(state.print_runs.store.lan_waiting_runs())
    assert sorted(run_id for run_id, _ in waiting) == sorted([alive, starting])
    assert "o" * 32 in workflow_reaper.run(state.print_runs.store.subjects_in_use())


def test_plate_cleared_signals_how_many_prints_had_started(
    client: TestClient, workflow_reaper: WorkflowReaper, sequences: list[Any]
) -> None:
    run_id = _run(client, workflow_reaper, WAITING.model_copy(update={"lan_awaiting_clear": True}))
    handle = _sequence(workflow_reaper, run_id, sequences)

    response = client.post(f"/api/v1/print/runs/{run_id}/plate-cleared")

    assert response.status_code == 202, response.text
    [signal] = [
        event.workflow_execution_signaled_event_attributes
        for event in _events(workflow_reaper, handle)
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
    ]
    assert signal.signal_name == PLATE_CLEARED_SIGNAL
    assert signal.input.payloads[0].data == b"1"


def test_plate_cleared_while_not_waiting_for_one_is_a_409(
    client: TestClient, workflow_reaper: WorkflowReaper, sequences: list[Any]
) -> None:
    run_id = _run(client, workflow_reaper)
    handle = _sequence(workflow_reaper, run_id, sequences)
    response = client.post(f"/api/v1/print/runs/{run_id}/plate-cleared")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == printing_api.NOT_AWAITING_CLEAR
    kinds = [event.event_type for event in _events(workflow_reaper, handle)]
    assert EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED not in kinds


def test_the_printers_card_names_the_runs_controls(
    client: TestClient, workflow_reaper: WorkflowReaper, sequences: list[Any]
) -> None:
    """The Prints page card of the printer a run waits on: its controls, and, while the
    run waits for a clear, the plate warning the Bambuddy path shows too."""
    printer = _printer(client)
    printers: Printers = _state(client).components.get(PRINTERS)
    printers.reports.put(printer, {"gcode_state": "FINISH"}, datetime.now(UTC))
    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]
    assert card["lan_run"] is None and card["awaiting_plate_clear"] is False

    run_id = _run(
        client,
        workflow_reaper,
        WAITING.model_copy(update={"lan_awaiting_clear": True}),
        printer=printer,
    )
    _sequence(workflow_reaper, run_id, sequences)
    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]
    assert card["lan_run"] == {
        "run_id": run_id,
        "waiting": 2,
        "awaiting_clear": True,
        "queued": False,
        "queued_for_print": False,
        "printer_idle": False,
    }
    assert card["awaiting_plate_clear"] is True


def test_a_gone_oldest_run_gives_way_once_the_sweep_records_it(
    client: TestClient, workflow_reaper: WorkflowReaper, sequences: list[Any]
) -> None:
    """#6871 (reviews of #6882): the card shows a printer's oldest waiting run, and its
    read writes nothing. A run whose sequence closed without recording its end (and whose
    ``PrintRun`` has closed) is recorded stopped by the sweep, and the next run's controls
    show. A run whose ``PrintRun`` is still open may yet start its sequence, so it stays
    first."""
    printer = _printer(client)
    gone = _run(client, workflow_reaper, printer=printer)
    starting = _run(client, workflow_reaper, printer=printer)
    alive = _run(client, workflow_reaper, printer=printer)
    _sequence(workflow_reaper, alive, sequences)
    assert workflow_reaper.client is not None
    parent = workflow_reaper.run(
        workflow_reaper.client.start_workflow(
            "PrintRun", "stand-in", id=f"print-{starting}", task_queue=f"nobody-{starting[:8]}"
        )
    )
    sequences.append(parent)
    state = _state(client)

    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]
    assert card["lan_run"]["run_id"] == gone
    assert client.get(f"/api/v1/print/runs/{gone}").json()["result"]["lan_stopped"] is None

    workflow_reaper.run(printing_api.end_gone_sequences(state.print_runs))
    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]
    assert card["lan_run"]["run_id"] == starting

    workflow_reaper.run(parent.terminate())
    workflow_reaper.run(printing_api.end_gone_sequences(state.print_runs))
    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]
    assert card["lan_run"]["run_id"] == alive


def test_the_prune_passes_run_holds_sweep_ends_a_gone_run(
    client: TestClient, workflow_reaper: WorkflowReaper, sequences: list[Any]
) -> None:
    """#6871 (review of #6939): the run-holds sweep, on the prune Schedule every
    `PRUNE_INTERVAL` whatever the asset sweep's interval, records a run whose sequence is
    gone as stopped, so the next run heads the printer's card within minutes."""
    printer = _printer(client)
    gone = _run(client, workflow_reaper, printer=printer)
    alive = _run(client, workflow_reaper, printer=printer)
    _sequence(workflow_reaper, alive, sequences)
    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]
    assert card["lan_run"]["run_id"] == gone

    workflow_reaper.run(app_main._reap_run_holds_logged(_state(client)))

    stopped = printing_api.SEQUENCE_GONE_STOP.model_dump()
    assert client.get(f"/api/v1/print/runs/{gone}").json()["result"]["lan_stopped"] == stopped
    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]
    assert card["lan_run"]["run_id"] == alive


def test_a_queued_runs_card_reads_whether_the_print_it_waits_for_still_runs(
    client: TestClient, workflow_reaper: WorkflowReaper, sequences: list[Any]
) -> None:
    """#6871 (review of #6939): `queued_for_print` follows the card's live state, so the
    text turns to the plate once the print the run was queued behind has ended."""
    printer = _printer(client)
    printers: Printers = _state(client).components.get(PRINTERS)
    queued = PrintRunResult(
        library_file_id=41,
        copies=1,
        path="native",
        lan_waiting=[1],
        lan_queued=LanQueued(state="RUNNING", job=STARTED, place=1),
    )
    run_id = _run(client, workflow_reaper, queued, printer=printer)
    _sequence(workflow_reaper, run_id, sequences)

    printers.reports.put(printer, {"gcode_state": "RUNNING"}, datetime.now(UTC))
    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]
    assert card["lan_run"]["queued"] and card["lan_run"]["queued_for_print"]

    printers.reports.put(printer, {"gcode_state": "FINISH"}, datetime.now(UTC))
    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]
    assert card["lan_run"]["queued"] and not card["lan_run"]["queued_for_print"]


def test_a_hanging_describe_never_stops_the_hold_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    """#6871 (review of #6948): each run's sequence check is bounded, and one that does
    not answer is skipped, so the run-holds reap after it still runs."""

    class Hanging:
        async def describe(self) -> None:
            await asyncio.sleep(3600)

    class Client:
        def get_workflow_handle(self, workflow_id: str) -> Hanging:
            return Hanging()

    class Store:
        async def lan_waiting_runs(self) -> list[tuple[str, str | None]]:
            return [("r1", "print-r1"), ("r2", "print-r2")]

    reaped: list[object] = []

    def reap(refs: object) -> int:
        reaped.append(refs)
        return 0

    monkeypatch.setattr(printing_api, "SEQUENCE_CHECK_TIMEOUT", timedelta(milliseconds=50))
    monkeypatch.setattr(app_main, "reap_run_holds", reap)
    state = SimpleNamespace(print_runs=SimpleNamespace(store=Store(), client=Client()), refs="refs")
    asyncio.run(asyncio.wait_for(app_main._reap_run_holds_logged(state), timeout=5))  # type: ignore[arg-type]
    assert reaped == ["refs"]


def test_the_gone_checks_stop_within_their_budget_however_many_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#6871 (review of #6948): many hanging describes, each within its own bound, still
    end within the pass's budget, so the hold reap after them runs inside its timeout."""

    class Hanging:
        async def describe(self) -> None:
            await asyncio.sleep(3600)

    class Client:
        def get_workflow_handle(self, workflow_id: str) -> Hanging:
            return Hanging()

    class Store:
        async def lan_waiting_runs(self) -> list[tuple[str, str | None]]:
            return [(f"r{n}", f"print-r{n}") for n in range(50)]

    monkeypatch.setattr(printing_api, "SEQUENCE_CHECK_TIMEOUT", timedelta(seconds=1))
    runs = SimpleNamespace(store=Store(), client=Client())

    async def check() -> float:
        loop = asyncio.get_running_loop()
        began = loop.time()
        await printing_api.end_gone_sequences(runs, timedelta(milliseconds=300))  # type: ignore[arg-type]
        return loop.time() - began

    took = asyncio.run(asyncio.wait_for(check(), timeout=10))
    assert took < 2  # 50 runs at 1 s each would take 50 s


def test_a_plate_only_queued_run_waits_for_a_print_started_since(
    client: TestClient, workflow_reaper: WorkflowReaper, sequences: list[Any]
) -> None:
    """#6871 (review of #6948): a run queued for an ended print's plate alone, on a
    printer someone then started printing on, waits for that print on the card."""
    printer = _printer(client)
    printers: Printers = _state(client).components.get(PRINTERS)
    queued = PrintRunResult(
        library_file_id=41,
        copies=1,
        path="native",
        lan_waiting=[1],
        lan_queued=LanQueued(state="FINISH", job=None, place=1),
    )
    run_id = _run(client, workflow_reaper, queued, printer=printer)
    _sequence(workflow_reaper, run_id, sequences)

    printers.reports.put(printer, {"gcode_state": "FINISH"}, datetime.now(UTC))
    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]
    assert card["lan_run"]["queued"] and not card["lan_run"]["queued_for_print"]

    printers.reports.put(printer, {"gcode_state": "RUNNING"}, datetime.now(UTC))
    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]
    assert card["lan_run"]["queued_for_print"]


def test_two_passes_at_once_record_a_gone_run_stopped_once(
    client: TestClient, workflow_reaper: WorkflowReaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6871 (review of #6948): the run-holds reap and the native-copies sweep may check
    the same gone run at once; the row lock in `record_lan_stopped` lets only one record
    and announce its stop. A barrier just after the run's read makes both passes read
    before either writes, unless the lock holds the second one back: without it, both
    would record (checked by removing ``FOR UPDATE`` once)."""
    store = _state(client).print_runs.store
    gone = _run(client, workflow_reaper, printer=_printer(client))
    announced: list[str] = []
    announce = store._announce

    def counted(conn: Any, run: Any, slug: str, row: Any) -> None:
        announced.append(run.id)
        announce(conn, run, slug, row)

    monkeypatch.setattr(store, "_announce", counted)
    # With the lock, the second read waits for the first write, so the first pass's
    # wait times out and breaks the barrier; both then go on.
    barrier = threading.Barrier(2, timeout=1)
    pool = store._pool

    class Paused:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        def __getattr__(self, name: str) -> Any:
            return getattr(self._conn, name)

        def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
            cursor = self._conn.execute(query, *args, **kwargs)
            if "slug FROM print_runs WHERE id = %s" in str(query):
                with contextlib.suppress(threading.BrokenBarrierError):
                    barrier.wait()
            return cursor

    class PausedPool:
        def __getattr__(self, name: str) -> Any:
            return getattr(pool, name)

        @contextlib.contextmanager
        def connection(self, *args: Any, **kwargs: Any) -> Iterator[Paused]:
            with pool.connection(*args, **kwargs) as conn:
                yield Paused(conn)

    monkeypatch.setattr(store, "_pool", PausedPool())

    async def both() -> None:
        runs = _state(client).print_runs
        await asyncio.gather(
            printing_api.end_gone_sequences(runs), printing_api.end_gone_sequences(runs)
        )

    workflow_reaper.run(both())
    stopped = printing_api.SEQUENCE_GONE_STOP.model_dump()
    assert client.get(f"/api/v1/print/runs/{gone}").json()["result"]["lan_stopped"] == stopped
    assert announced == [gone]
