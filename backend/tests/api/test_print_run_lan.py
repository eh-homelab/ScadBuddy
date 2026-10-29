"""The controls of a native run's prints still to start: stop the rest (#4942), and say
the plate is clear (#4939). Each route reads the run's record, then sends its
``PrintLanSequence`` a cancel or the ``plate_cleared`` signal; the Prints page card
names the run whose controls they are.

The sequence here is a stand-in execution of the same id on a queue no worker polls:
the routes only send to it, and what they sent is in its history."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle
from temporalio.service import RPCError

from scadbuddy.api import printing as printing_api
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.print_run import PrintRunResult
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.printers.component import PRINTERS, Printers
from scadbuddy.printers.lan_models import LanStarted, LanStopped
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


def test_a_sequence_that_closed_is_a_409_that_says_so(
    client: TestClient, workflow_reaper: WorkflowReaper, sequences: list[Any]
) -> None:
    """A sequence that ended without recording it (terminated, say), or none at all."""
    awaiting = WAITING.model_copy(update={"lan_awaiting_clear": True})
    run_id = _run(client, workflow_reaper, awaiting)
    handle = _sequence(workflow_reaper, run_id, sequences)
    workflow_reaper.run(handle.terminate())
    unstarted = _run(client, workflow_reaper, awaiting)
    for run, action in [(run_id, "stop"), (run_id, "plate-cleared"), (unstarted, "stop")]:
        response = client.post(f"/api/v1/print/runs/{run}/{action}")
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == printing_api.SEQUENCE_GONE


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
    client: TestClient, workflow_reaper: WorkflowReaper
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
    (card,) = client.get("/api/v1/print/printers/now").json()["printers"]
    assert card["lan_run"] == {"run_id": run_id, "waiting": 2, "awaiting_clear": True}
    assert card["awaiting_plate_clear"] is True
