"""A listing's prepare run (#6237, #6299 review) on a real Temporal, its activities faked:
it keeps at most `PREPARE_IN_FLIGHT` prepared renders open, counts a failed submit, and a
second start joins the open run. The activities' own rules, and a visitor's render during
a run, are ``tests/api/test_storefront_prepare.py``'s."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from scadbuddy.orders.storefront.prepare import TemporalPreparer
from scadbuddy.workflows.prepare_listing import (
    PREPARE_IN_FLIGHT,
    SETTLED_ACTIVITY,
    SUBMIT_ACTIVITY,
    OpenJob,
    PrepareListing,
    PrepareListingResult,
    SubmitAsk,
    Submitted,
    workflow_id,
)
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = pytest.mark.requires_temporal


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.fixture
def queue() -> str:
    return f"library-{uuid.uuid4().hex[:8]}"


class Renders:
    """A fake render queue of ``count`` combinations: each submit opens a job (or answers
    ``outcomes[index]``), and each read of the open jobs settles the oldest one, unless
    ``hold`` is clear. It counts the most jobs open at once."""

    def __init__(self, count: int, outcomes: dict[int, str] | None = None) -> None:
        self.count = count
        self.outcomes = outcomes or {}
        self.open: list[str] = []
        self.most_open = 0
        self.asked: list[int] = []
        self.hold = asyncio.Event()
        self.hold.set()

    def activities(self) -> list[Any]:
        @activity.defn(name=SUBMIT_ACTIVITY)
        async def prepare_listing_submit(ask: SubmitAsk) -> Submitted:
            self.asked.append(ask.index)
            if ask.index >= self.count:
                return Submitted(outcome="end")
            outcome = self.outcomes.get(ask.index, "open")
            if outcome != "open":
                return Submitted.model_validate({"outcome": outcome})
            key = f"key-{ask.index}"
            self.open.append(key)
            self.most_open = max(self.most_open, len(self.open))
            return Submitted(outcome="open", job=OpenJob(slug="demo", render_key=key))

        @activity.defn(name=SETTLED_ACTIVITY)
        async def prepare_listing_settled(jobs: list[OpenJob]) -> list[OpenJob]:
            if self.hold.is_set() and self.open:
                self.open.pop(0)
            return [j for j in jobs if j.render_key in self.open]

        return [prepare_listing_submit, prepare_listing_settled]


def worker(client: Client, queue: str, renders: Renders) -> Worker:
    return Worker(
        client, task_queue=queue, workflows=[PrepareListing], activities=renders.activities()
    )


async def test_the_run_never_has_more_than_its_limit_open(client: Client, queue: str) -> None:
    listing_id = uuid.uuid4().hex
    renders = Renders(7)
    try:
        async with worker(client, queue, renders):
            assert await TemporalPreparer(client, queue).start(listing_id)
            handle = client.get_workflow_handle(
                workflow_id(listing_id), result_type=PrepareListingResult
            )
            result = await asyncio.wait_for(handle.result(), 60)
        assert result == PrepareListingResult(submitted=7, failed_to_start=0)
        assert renders.most_open == PREPARE_IN_FLIGHT
        assert renders.open == []
        # Each index once, then the one past the last.
        assert renders.asked == [*range(7), 7]
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_failed_submit_shows_in_failed_to_start(client: Client, queue: str) -> None:
    listing_id = uuid.uuid4().hex
    # Index 1 fails, and index 2 is done already.
    renders = Renders(4, {1: "failed", 2: "settled"})
    preparer = TemporalPreparer(client, queue)
    try:
        async with worker(client, queue, renders):
            await preparer.start(listing_id)
            handle = client.get_workflow_handle(
                workflow_id(listing_id), result_type=PrepareListingResult
            )
            result = await asyncio.wait_for(handle.result(), 60)
        assert result.failed_to_start == 1
        state = await preparer.run_state(listing_id)
        # The memo holds the failed index, not a count (#6299 review).
        assert (state.running, state.failed_indices) == (False, (1,))
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_full_queue_is_waited_out_on_the_same_combination(
    client: Client, queue: str
) -> None:
    listing_id = uuid.uuid4().hex
    renders = Renders(2)
    busy = 1
    plain = renders.activities()

    @activity.defn(name=SUBMIT_ACTIVITY)
    async def prepare_listing_submit(ask: SubmitAsk) -> Submitted:
        nonlocal busy
        if ask.index == 1 and busy:
            busy -= 1
            renders.asked.append(ask.index)
            return Submitted(outcome="busy")
        submitted: Submitted = await plain[0](ask)
        return submitted

    try:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[PrepareListing],
            activities=[prepare_listing_submit, plain[1]],
        ):
            await TemporalPreparer(client, queue).start(listing_id)
            handle = client.get_workflow_handle(
                workflow_id(listing_id), result_type=PrepareListingResult
            )
            result = await asyncio.wait_for(handle.result(), 60)
        assert result == PrepareListingResult(submitted=2, failed_to_start=0)
        assert renders.asked == [0, 1, 1, 2]
    finally:
        await terminate_open_workflows(client, queue)


async def test_a_second_start_while_it_runs_starts_no_second_run(
    client: Client, queue: str
) -> None:
    listing_id = uuid.uuid4().hex
    renders = Renders(3)
    renders.hold.clear()
    preparer = TemporalPreparer(client, queue)
    try:
        async with worker(client, queue, renders):
            assert await preparer.start(listing_id)
            first = await client.get_workflow_handle(workflow_id(listing_id)).describe()
            while len(renders.asked) < PREPARE_IN_FLIGHT:
                await asyncio.sleep(0.1)
            assert (await preparer.run_state(listing_id)).running
            assert not await preparer.start(listing_id)
            again = await client.get_workflow_handle(workflow_id(listing_id)).describe()
            assert again.run_id == first.run_id
            renders.hold.set()
            handle = client.get_workflow_handle(
                workflow_id(listing_id), result_type=PrepareListingResult
            )
            assert (await asyncio.wait_for(handle.result(), 90)).submitted == 3
        # One run asked for each combination once.
        assert renders.asked == [0, 1, 2, 3]
    finally:
        await terminate_open_workflows(client, queue)
