"""``LibraryCopy`` replays the histories it wrote (#5424).

On the versioned ``library`` worker (#4090) a copy runs as ``VersionedLibraryCopy``,
AUTO_UPGRADE: an open copy moves to the new build at its next workflow task and replays
there. So a change to the commands ``LibraryCopy.run`` issues goes behind
``workflow.patched`` and adds a history recorded on the changed code beside these.

``completed`` is a copy that plans, copies two batches and finishes; ``plan_failed``
one whose plan activity fails until its retries run out, so the finish activity records
the cause and the execution fails. The ``*_v1`` histories were recorded on the code
before #5424 (no ``RETRIES_PATCH`` marker); the others on the code after it. Each was
recorded with stub activities (``SCADBUDDY_RECORD_HISTORIES=1`` runs the recording
below, which writes the names without the ``_v1``). Never re-record them.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from scadbuddy.print_library.copy import LIBRARY_COPY_BATCH, LIBRARY_COPY_FINISH, LIBRARY_COPY_PLAN
from scadbuddy.print_library.copy_models import (
    LibraryCopyBatch,
    LibraryCopyBatchInput,
    LibraryCopyFinishInput,
    LibraryCopyInput,
    LibraryCopyPlan,
    LibraryCopyRequest,
    LibraryCopyResult,
)
from scadbuddy.workflows.library_copy import LibraryCopy
from tests.support.temporal import temporal_server

HISTORIES = Path(__file__).parent / "fixtures" / "library_copy_histories"
RECORDED = ["completed", "plan_failed"]


@pytest.mark.parametrize("name", sorted(p.stem for p in HISTORIES.glob("*.json")))
async def test_library_copy_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(
        f"library-copy-{name}", (HISTORIES / f"{name}.json").read_text()
    )
    await Replayer(workflows=[LibraryCopy], data_converter=pydantic_data_converter).replay_workflow(
        history
    )


def test_every_recording_has_its_history() -> None:
    names = {p.stem for p in HISTORIES.glob("*.json")}
    assert {f"{n}_v1" for n in RECORDED} <= names


@pytest.fixture
def recording_temporal() -> Iterator[str]:
    with temporal_server() as address:
        yield address


@pytest.mark.requires_temporal
@pytest.mark.skipif(os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history")
@pytest.mark.parametrize("name", RECORDED)
async def test_record(recording_temporal: str, name: str) -> None:
    batches = 0

    @activity.defn(name=LIBRARY_COPY_PLAN)
    async def plan(input: LibraryCopyInput) -> LibraryCopyPlan:
        if name == "plan_failed":
            raise ApplicationError("Bambuddy answered 502 when asked to list the library")
        return LibraryCopyPlan(total=3)

    @activity.defn(name=LIBRARY_COPY_BATCH)
    async def batch(input: LibraryCopyBatchInput) -> LibraryCopyBatch:
        nonlocal batches
        batches += 1
        return LibraryCopyBatch(copied=2, skipped=0, failed=0, remaining=1 if batches == 1 else 0)

    @activity.defn(name=LIBRARY_COPY_FINISH)
    async def finish(input: LibraryCopyFinishInput) -> None:
        return None

    client = await Client.connect(recording_temporal, data_converter=pydantic_data_converter)
    queue = f"library-copy-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client, task_queue=queue, workflows=[LibraryCopy], activities=[plan, batch, finish]
    ):
        handle = await client.start_workflow(
            LibraryCopy.run,
            LibraryCopyInput(
                copy_id=f"library-copy-{name}",
                request=LibraryCopyRequest(direction="bb_to_sb", file_ids=[7, 8, 9]),
            ),
            id=f"library-copy-{name}-{queue}",
            task_queue=queue,
            execution_timeout=timedelta(minutes=10),
        )
        if name == "plan_failed":
            with pytest.raises(WorkflowFailureError):
                await handle.result()
        else:
            result: LibraryCopyResult = await handle.result()
            assert result.batches_done == 2
        history = await handle.fetch_history()
    HISTORIES.mkdir(exist_ok=True)
    (HISTORIES / f"{name}.json").write_text(history.to_json())
