"""``FollowPrint`` replays the histories it wrote (#5613).

A follow moves to each new print-worker build (``VersionedFollowPrint``, AUTO_UPGRADE),
so a follow open across a deploy replays on the new code. #5613 settles each run of a
subject in the follow's activity (`bambuddy/follow.py`), not in the workflow: the
workflow id (``follow-print-<subject>``), its input and its commands stay as they were,
and these histories say so. A change to ``FollowPrint.run``'s commands goes behind
``workflow.patched``.

Recorded by the workflow as it stood before #5613 (``SCADBUDDY_RECORD_HISTORIES=1`` runs
the recordings below): ``settled`` (one attempt that ends ``settled``) and ``poked`` (a
new print's ``poke`` cancels the first attempt, and a fresh one ends ``settled``). Never
re-record them: a later change adds a history recorded on the changed code beside them.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer, Worker

from scadbuddy.bambuddy.follow import FOLLOW_ACTIVITY, FollowInput
from scadbuddy.workflows.follow import POKE_SIGNAL, FollowPrint, follow_id, follow_queue
from tests.support.temporal import temporal_server

HISTORIES = Path(__file__).parent / "fixtures" / "follow_print_histories"
SUBJECT = "library:89"


@pytest.mark.parametrize("name", ["settled", "poked"])
async def test_follow_print_replays_its_recorded_history(name: str) -> None:
    history = WorkflowHistory.from_json(
        f"follow-print-{name}", (HISTORIES / f"{name}.json").read_text()
    )
    await Replayer(workflows=[FollowPrint], data_converter=pydantic_data_converter).replay_workflow(
        history
    )


# ── recording ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def recording_temporal() -> Iterator[str]:
    with temporal_server() as address:
        yield address


#: Attempts the fake activity has started, and how many of them hold until cancelled.
_started: list[FollowInput] = []
_holding = [0]


@activity.defn(name=FOLLOW_ACTIVITY)
async def _follows(input: FollowInput) -> str:
    """The real activity's shape: heartbeats while it follows, then ends ``settled``."""
    _started.append(input)
    if len(_started) <= _holding[0]:
        while True:
            activity.heartbeat()
            await asyncio.sleep(0.1)
    return "settled"


@asynccontextmanager
async def _serving(address: str) -> AsyncIterator[tuple[Client, str]]:
    client = await Client.connect(address, data_converter=pydantic_data_converter)
    queue = f"bambuddy-{uuid.uuid4().hex[:8]}"
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(Worker(client, task_queue=queue, workflows=[FollowPrint]))
        await stack.enter_async_context(
            Worker(
                client,
                task_queue=follow_queue(queue),
                activities=[_follows],
                max_heartbeat_throttle_interval=timedelta(milliseconds=200),
                default_heartbeat_throttle_interval=timedelta(milliseconds=200),
            )
        )
        yield client, queue


recording = pytest.mark.skipif(
    os.environ.get("SCADBUDDY_RECORD_HISTORIES") != "1", reason="records a history"
)


@pytest.mark.requires_temporal
@recording
async def test_record_settled(recording_temporal: str) -> None:
    _started.clear()
    _holding[0] = 0
    async with _serving(recording_temporal) as (client, queue):
        handle = await client.start_workflow(
            FollowPrint.run, args=[SUBJECT, False], id=follow_id(SUBJECT), task_queue=queue
        )
        assert await handle.result() == "settled"
        (HISTORIES / "settled.json").write_text((await handle.fetch_history()).to_json())


@pytest.mark.requires_temporal
@recording
async def test_record_poked(recording_temporal: str) -> None:
    _started.clear()
    _holding[0] = 1
    async with _serving(recording_temporal) as (client, queue):
        handle = await client.start_workflow(
            FollowPrint.run, args=[SUBJECT, False], id=follow_id(SUBJECT), task_queue=queue
        )
        async with asyncio.timeout(15):
            while not _started:
                await asyncio.sleep(0.1)
        await handle.signal(POKE_SIGNAL)
        assert await handle.result() == "settled"
        assert [input.fresh for input in _started] == [False, True]
        (HISTORIES / "poked.json").write_text((await handle.fetch_history()).to_json())
