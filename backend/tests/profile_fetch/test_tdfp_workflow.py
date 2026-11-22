"""#7025: ``TdfpSearch`` and ``TdfpCreate`` on a real Temporal, started by the API's
`filament.tdfp.TemporalTdfp`, their activities faked by name: the answer comes back to the
caller, a failure comes back as its kind and why, the create is tried once, and a create
sent again under its key answers the first one's outcome without running again."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.filament.tdfp import TemporalTdfp
from scadbuddy.profile_fetch import workflow
from scadbuddy.profile_fetch.wire import (
    CREATE_ACTIVITY,
    SEARCH_ACTIVITY,
    CreateAnswer,
    CreateAsk,
    SearchAsk,
)
from scadbuddy.profile_fetch.workflow import TdfpCreate, TdfpSearch
from tests.profile_fetch.test_activities import SEARCH_ANSWER
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = pytest.mark.requires_temporal

ASK = CreateAsk(
    brand="SUNLU", material="PLA+/Pro", material_type="Silk", colour="Copper", hexes=["#B87333"]
)


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@pytest.fixture
def queue() -> str:
    return f"profile-fetch-{uuid.uuid4().hex[:8]}"


class Fakes:
    def __init__(self, error: Callable[[], BaseException] | None = None) -> None:
        self.error = error
        self.searches: list[SearchAsk] = []
        self.creates: list[CreateAsk] = []
        self.attempts: list[int] = []

    def activities(self) -> list[Callable[..., Any]]:
        @activity.defn(name=SEARCH_ACTIVITY)
        async def search(ask: SearchAsk) -> Any:
            self.searches.append(ask)
            self.attempts.append(activity.info().attempt)
            if self.error is not None:
                raise self.error()
            return SEARCH_ANSWER

        @activity.defn(name=CREATE_ACTIVITY)
        async def create(ask: CreateAsk) -> CreateAnswer:
            self.creates.append(ask)
            self.attempts.append(activity.info().attempt)
            if self.error is not None:
                raise self.error()
            return CreateAnswer(status="created", tdfp_id=40000)

        return [search, create]


def _worker(client: Client, queue: str, fakes: Fakes) -> Worker:
    return Worker(
        client, task_queue=queue, workflows=[TdfpSearch, TdfpCreate], activities=fakes.activities()
    )


async def test_a_search_answers_the_candidates(client: Client, queue: str) -> None:
    fakes = Fakes()
    try:
        async with _worker(client, queue, fakes):
            outcome = await TemporalTdfp(client, queue).search(SearchAsk(brand="SUNLU"), wait=60)
    finally:
        await terminate_open_workflows(client, queue)
    assert outcome.search == SEARCH_ANSWER
    assert outcome.kind is None


async def test_a_create_is_tried_once_and_answers_why_it_failed(client: Client, queue: str) -> None:
    fakes = Fakes(lambda: ApplicationError("the browser failed", type="browser"))
    try:
        async with _worker(client, queue, fakes):
            outcome = await TemporalTdfp(client, queue).create(ASK, key="k-1", wait=60)
    finally:
        await terminate_open_workflows(client, queue)
    assert fakes.attempts == [1]
    assert outcome.create is None
    assert outcome.kind == "browser"
    assert outcome.reason == f"the browser failed; {workflow.MAY_EXIST}"


async def test_a_create_sent_again_under_its_key_answers_the_first(
    client: Client, queue: str
) -> None:
    fakes = Fakes()
    tdfp = TemporalTdfp(client, queue)
    try:
        async with _worker(client, queue, fakes):
            first = await tdfp.create(ASK, key="k-2", wait=60)
            again = await tdfp.create(ASK, key="k-2", wait=60)
            other = await tdfp.create(ASK.model_copy(update={"colour": "Gold"}), key="k-2", wait=60)
    finally:
        await terminate_open_workflows(client, queue)
    assert first == again
    assert first.create is not None and first.create.tdfp_id == 40000
    # The same key with another ask is another create.
    assert other.create is not None
    assert [ask.colour for ask in fakes.creates] == ["Copper", "Gold"]


async def test_a_search_no_worker_takes_fails_as_not_started(
    client: Client, queue: str, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setattr(workflow, "SEARCH_SCHEDULE_TO_START", timedelta(seconds=1))
    fakes = Fakes()
    try:
        # A worker for the workflow only: no worker takes the activity. Unsandboxed, so the
        # workflow reads the patched timeout.
        async with Worker(
            client,
            task_queue=queue,
            workflows=[TdfpSearch],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            outcome = await TemporalTdfp(client, queue).search(SearchAsk(brand="SUNLU"), wait=60)
    finally:
        await terminate_open_workflows(client, queue)
    assert outcome.kind == "not_started"
    assert outcome.reason is not None and "busy" in outcome.reason
    assert fakes.searches == []
