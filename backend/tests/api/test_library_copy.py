"""`/print/library-copies` (#5355): a copy started once per request, its progress read
off the rows, and Bambuddy's library browsed as a source while the native library
serves `/print/library`."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from temporalio.client import WorkflowExecutionStatus

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.library_copy import ENDED_UNFINISHED, _settled
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.operations.component import OPERATIONS, OperationCommands
from scadbuddy.print_library import copy as copy_module
from scadbuddy.print_library.copy_models import (
    LibraryCopyBatch,
    LibraryCopyBatchInput,
    LibraryCopyRequest,
    LibraryCopySummary,
)
from scadbuddy.print_library.copy_store import LibraryCopyStore, PlannedFile
from tests.api.test_library_native import _native_settings
from tests.api.test_send import BASE, configure

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

NATIVE_FILE = 1000000001
NATIVE_FOLDER = 1000000002


@pytest.fixture
def native_client(settings: Settings, s3: S3Settings) -> Iterator[TestClient]:
    with TestClient(create_app(_native_settings(settings, s3))) as client:
        configure(client)
        yield client


def _until(check: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.1)


def test_a_copy_runs_once_per_request_and_its_rows_are_its_status(
    native_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    plans: list[Any] = []
    batches: list[LibraryCopyBatchInput] = []

    async def plan(request: Any, source: Any, target: Any) -> list[PlannedFile]:
        plans.append(request)
        return [
            PlannedFile(source_id=7, path="Dinos", name="rex.3mf", size=3),
            PlannedFile(source_id=8, path="", name="gone.3mf", reason="it is no longer there"),
        ]

    async def batch(store: LibraryCopyStore, input: LibraryCopyBatchInput, **_: Any) -> Any:
        batches.append(input)
        store.record(input.copy_id, 7, "copied", target_id=NATIVE_FILE, target_folder_id=None)
        counts = store.counts(input.copy_id)
        return LibraryCopyBatch(copied=1, skipped=0, failed=0, remaining=counts.pending)

    monkeypatch.setattr(copy_module, "plan_copy", plan)
    monkeypatch.setattr(copy_module, "copy_batch", batch)
    body = {"direction": "bb_to_sb", "file_ids": [8, 7], "folder_ids": [3]}
    first = native_client.post("/api/v1/print/library-copies", json=body)
    assert first.status_code == 202, first.text
    copy_id = first.json()["copy_id"]
    assert copy_id.startswith("library-copy-")
    # The row is there from the 202 on, and the same request answers the same id.
    assert native_client.get(f"/api/v1/print/library-copies/{copy_id}").status_code == 200
    again = native_client.post("/api/v1/print/library-copies", json={**body, "file_ids": [7, 8]})
    assert again.json() == {"copy_id": copy_id}

    def status() -> dict[str, Any]:
        answer = native_client.get(f"/api/v1/print/library-copies/{copy_id}")
        assert answer.status_code == 200, answer.text
        got: dict[str, Any] = answer.json()
        return got

    _until(lambda: status()["status"] != "running" and not status()["running"])
    done = status()
    assert (done["status"], done["total"], done["copied"], done["failed"]) == ("done", 2, 1, 1)
    # The start recorded the run it began: the status matches the execution by it.
    state: AppState = getattr(native_client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    ops: OperationCommands = state.components.get(OPERATIONS)
    portal = native_client.portal
    assert portal is not None
    described = portal.call(ops.client.get_workflow_handle(copy_id).describe)
    assert done["run_id"] == described.raw_info.first_run_id
    assert done["request"] == {
        "direction": "bb_to_sb",
        "file_ids": [7, 8],
        "folder_ids": [3],
        "target_folder_id": None,
    }
    files = {f["source_id"]: f for f in done["files"]}
    assert files[7]["status"] == "copied" and files[7]["target_id"] == NATIVE_FILE
    assert files[8] == {
        "source_id": 8,
        "path": "",
        "name": "gone.3mf",
        "size": None,
        "status": "failed",
        "reason": "it is no longer there",
        "attempts": 0,
        "target_id": None,
        "target_folder_id": None,
    }
    # Failed rows come first, so a long list shows what needs looking at.
    assert [f["source_id"] for f in done["files"]] == [8, 7]
    assert len(plans) == 1 and len(batches) == 1
    recent = native_client.get("/api/v1/print/library-copies").json()
    assert [(r["copy_id"], r["status"], r["running"]) for r in recent] == [(copy_id, "done", False)]

    # The same request after the end runs again, under the same id, with fresh rows.
    rerun = native_client.post("/api/v1/print/library-copies", json=body)
    assert rerun.status_code == 202 and rerun.json() == {"copy_id": copy_id}
    _until(lambda: len(plans) == 2 and status()["status"] == "done")
    assert native_client.get("/api/v1/print/library-copies/nope").status_code == 404


def test_a_copy_whose_execution_ended_unfinished_reads_failed_but_one_starting_does_not(
    native_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminated execution leaves its row `running`; the status says it ended. A
    row written before its execution starts (a new id, a rerun) is not that."""
    gates: list[threading.Event] = []

    async def plan(request: Any, source: Any, target: Any) -> list[PlannedFile]:
        return [PlannedFile(source_id=7, path="", name="rex.3mf", size=3)]

    async def batch(store: LibraryCopyStore, input: LibraryCopyBatchInput, **_: Any) -> Any:
        gate = threading.Event()
        gates.append(gate)
        await asyncio.to_thread(gate.wait)
        store.record(input.copy_id, 7, "copied", target_id=NATIVE_FILE, target_folder_id=None)
        return LibraryCopyBatch(copied=1, skipped=0, failed=0, remaining=0)

    monkeypatch.setattr(copy_module, "plan_copy", plan)
    monkeypatch.setattr(copy_module, "copy_batch", batch)
    state: AppState = getattr(native_client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    ops: OperationCommands = state.components.get(OPERATIONS)
    rows = LibraryCopyStore(state.settings_store.pool)

    def status(copy_id: str) -> dict[str, Any]:
        answer = native_client.get(f"/api/v1/print/library-copies/{copy_id}")
        assert answer.status_code == 200, answer.text
        got: dict[str, Any] = answer.json()
        return got

    # A row with no execution yet is a copy about to run.
    fresh = LibraryCopyRequest(direction="bb_to_sb", file_ids=[9])
    rows.start("library-copy-fresh", fresh)
    shown = status("library-copy-fresh")
    assert (shown["status"], shown["running"], shown["error"]) == ("running", False, None)

    body = {"direction": "bb_to_sb", "file_ids": [7]}
    started = native_client.post("/api/v1/print/library-copies", json=body)
    assert started.status_code == 202, started.text
    copy_id = started.json()["copy_id"]
    _until(lambda: len(gates) == 1)  # the batch is in flight: the execution runs
    assert status(copy_id)["running"] is True
    portal = native_client.portal
    assert portal is not None  # a client in its `with` block has one
    portal.call(ops.client.get_workflow_handle(copy_id).terminate)
    gates[0].set()
    _until(lambda: status(copy_id)["running"] is False)
    ended = status(copy_id)
    assert (ended["status"], ended["error"]) == ("failed", ENDED_UNFINISHED)
    listed = {r["copy_id"]: r for r in native_client.get("/api/v1/print/library-copies").json()}
    assert (listed[copy_id]["status"], listed[copy_id]["error"]) == ("failed", ENDED_UNFINISHED)
    assert listed["library-copy-fresh"]["status"] == "running"

    # Rerun: the row is reset before the new execution starts, and the old one closed
    # before that, so the window between the two reads as running, not ended.
    rerun = native_client.post("/api/v1/print/library-copies", json=body)
    assert rerun.status_code == 202 and rerun.json() == {"copy_id": copy_id}
    assert status(copy_id)["status"] == "running"
    _until(lambda: len(gates) == 2)
    assert status(copy_id)["status"] == "running"
    gates[1].set()
    _until(lambda: status(copy_id)["status"] == "done")
    assert status(copy_id)["error"] is None


def test_a_copy_needs_the_native_library_and_a_bambuddy(
    client: TestClient, native_client: TestClient
) -> None:
    body = {"direction": "bb_to_sb", "file_ids": [7]}
    refused = client.post("/api/v1/print/library-copies", json=body)
    assert refused.status_code == 409, refused.text
    assert "library_backend" in refused.json()["detail"]
    assert client.get("/api/v1/print/library-copies").status_code == 200
    # On native, with Bambuddy unset: the 409 that names the missing URL.
    assert native_client.put("/api/v1/settings", json={"bambuddy_url": ""}).status_code == 200
    unset = native_client.post("/api/v1/print/library-copies", json=body)
    assert unset.status_code == 409, unset.text
    assert "Bambuddy URL" in unset.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"direction": "bb_to_sb", "file_ids": [NATIVE_FILE]},
        {"direction": "sb_to_bb", "file_ids": [7]},
        {"direction": "sb_to_bb", "folder_ids": [NATIVE_FOLDER], "target_folder_id": NATIVE_FOLDER},
        {"direction": "bb_to_sb", "folder_ids": [3], "target_folder_id": 4},
        {"direction": "bb_to_sb"},
    ],
)
def test_an_id_on_the_wrong_side_is_refused_before_anything_starts(
    native_client: TestClient, body: dict[str, Any]
) -> None:
    assert native_client.post("/api/v1/print/library-copies", json=body).status_code == 422


def test_a_target_folder_the_target_does_not_have_is_refused_before_anything_starts(
    native_client: TestClient,
) -> None:
    body = {"direction": "bb_to_sb", "file_ids": [7], "target_folder_id": 1000000999}
    refused = native_client.post("/api/v1/print/library-copies", json=body)
    assert refused.status_code == 404, refused.text
    assert "1000000999" in refused.json()["detail"]
    assert native_client.get("/api/v1/print/library-copies").json() == []


@respx.mock
def test_bambuddy_is_browsed_as_a_source_while_native_serves(native_client: TestClient) -> None:
    respx.get(f"{BASE}/api/v1/library/folders").mock(
        return_value=Response(
            200,
            json=[{"id": 3, "name": "Dinos", "parent_id": None, "file_count": 1, "children": []}],
        )
    )
    respx.get(f"{BASE}/api/v1/library/files/", params={"folder_id": "3"}).mock(
        return_value=Response(
            200,
            json=[
                {"id": 7, "filename": "rex.3mf", "file_type": "3mf", "folder_id": 3},
                {"id": 9, "filename": "rex.gcode.3mf", "file_type": "gcode.3mf", "folder_id": 3},
            ],
        )
    )
    listing = native_client.get("/api/v1/print/bambuddy-library", params={"folder_id": 3})
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert [f["name"] for f in body["folders"]] == ["Dinos"]
    # Every type, sliced files included: a copy takes any file.
    assert [(f["id"], f["filename"]) for f in body["files"]] == [
        (7, "rex.3mf"),
        (9, "rex.gcode.3mf"),
    ]
    # The native library still serves the page itself: Bambuddy's folder is not its.
    assert native_client.get("/api/v1/print/library", params={"folder_id": 3}).status_code == 404


def _summary(run_id: str | None, started_at: datetime) -> LibraryCopySummary:
    return LibraryCopySummary(
        copy_id="library-copy-skew",
        direction="bb_to_sb",
        request=LibraryCopyRequest(direction="bb_to_sb", file_ids=[7]),
        status="running",
        started_at=started_at,
        run_id=run_id,
    )


def _closed(run_id: str, first_run_id: str, close_time: datetime) -> Any:
    return SimpleNamespace(
        status=WorkflowExecutionStatus.TERMINATED,
        close_time=close_time,
        run_id=run_id,
        raw_info=SimpleNamespace(first_run_id=first_run_id),
    )


def test_settled_matches_the_run_by_its_id_not_by_two_clocks() -> None:
    """#5424: Temporal's ``close_time`` and Postgres' ``started_at`` come from two
    clocks. The row's own run, closed, reads as ended whatever the clocks say. The run
    before a rerun never does."""
    now = datetime.now(UTC)
    early = now - timedelta(seconds=5)  # Temporal's clock behind Postgres'
    late = now + timedelta(seconds=5)  # Temporal's clock ahead of Postgres'
    ended = _settled(_summary("run-b", now), _closed("run-b", "run-b", early))
    assert (ended.status, ended.error) == ("failed", ENDED_UNFINISHED)
    # A chain that continued as new: matched by its first run.
    chained = _settled(_summary("run-b", now), _closed("run-c", "run-b", early))
    assert chained.status == "failed"
    rerun = _settled(_summary("run-b", now), _closed("run-a", "run-a", late))
    assert (rerun.status, rerun.error) == ("running", None)
    unstarted = _settled(_summary(None, now), _closed("run-a", "run-a", late))
    assert unstarted.status == "running"
