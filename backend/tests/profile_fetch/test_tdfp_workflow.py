"""#7025: ``TdfpSearch`` and ``TdfpCreate`` on a real Temporal, started by the API's
`filament.tdfp.TemporalTdfp`, their activities faked by name: the answer comes back to the
caller, a failure comes back as its kind and why, the create is tried once, and a create
sent again under its key answers the first one's outcome without running again."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.filament import tdfp as tdfp_module
from scadbuddy.filament.profile_fetch import FetchStarter
from scadbuddy.filament.tdfp import TemporalTdfp
from scadbuddy.profile_fetch import workflow
from scadbuddy.profile_fetch.wire import (
    CREATE_ACTIVITY,
    EXPORT_ACTIVITY,
    SEARCH_ACTIVITY,
    CreateAnswer,
    CreateAsk,
    ExportReport,
    FetchAsk,
    SearchAsk,
)
from scadbuddy.profile_fetch.worker import temporal_workers
from scadbuddy.profile_fetch.workflow import TdfpSearch
from tests.profile_fetch.test_activities import SEARCH_ANSWER
from tests.profile_fetch.test_workflow import REPORT
from tests.profile_fetch.test_workflow import Fakes as FetchFakes
from tests.profile_fetch.test_workflow import _ask as _fetch_ask
from tests.profile_fetch.test_workflow import _result as _fetch_result
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
    def __init__(self, error: Callable[[], BaseException | None] | None = None) -> None:
        self.error = error
        self.searches: list[SearchAsk] = []
        self.creates: list[CreateAsk] = []
        self.attempts: list[int] = []

    def activities(self) -> list[Callable[..., Any]]:
        @activity.defn(name=SEARCH_ACTIVITY)
        async def search(ask: SearchAsk) -> Any:
            self.searches.append(ask)
            self.attempts.append(activity.info().attempt)
            if self.error is not None and (failure := self.error()) is not None:
                raise failure
            return SEARCH_ANSWER

        @activity.defn(name=CREATE_ACTIVITY)
        async def create(ask: CreateAsk) -> CreateAnswer:
            self.creates.append(ask)
            self.attempts.append(activity.info().attempt)
            if self.error is not None and (failure := self.error()) is not None:
                raise failure
            return CreateAnswer(status="created", tdfp_id=40000)

        return [search, create]


@asynccontextmanager
async def _workers(
    client: Client,
    queue: str,
    fakes: Fakes,
    *,
    fetch: list[Callable[..., Any]] | None = None,
    **options: Any,
) -> AsyncIterator[None]:
    """The process's two workers (`worker.temporal_workers`), with ``fakes``' activities."""
    search, create = fakes.activities()
    async with AsyncExitStack() as running:
        for each in temporal_workers(
            client,
            queue,
            fetch=fetch if fetch is not None else FetchFakes().activities(),
            create=create,
            search=search,
            **options,
        ):
            await running.enter_async_context(each)
        yield


async def test_a_search_answers_the_candidates(client: Client, queue: str) -> None:
    fakes = Fakes()
    try:
        async with _workers(client, queue, fakes):
            outcome = await TemporalTdfp(client, queue).search(SearchAsk(brand="SUNLU"), wait=60)
    finally:
        await terminate_open_workflows(client, queue)
    assert outcome.search == SEARCH_ANSWER
    assert outcome.kind is None


async def test_a_create_failed_before_its_save_runs_again_under_its_key(
    client: Client, queue: str
) -> None:
    """R-7025-11: a failure the driver raises before "Save Filament" made nothing, so the
    same key tries again instead of answering the failure for ever."""
    errors = [ApplicationError("the browser failed", type="browser", non_retryable=True)]
    fakes = Fakes(lambda: errors.pop(0) if errors else None)
    tdfp = TemporalTdfp(client, queue)
    try:
        async with _workers(client, queue, fakes):
            first = await tdfp.create(ASK, key="k-1", wait=60)
            assert not await tdfp.started(ASK, key="k-1")
            again = await tdfp.create(ASK, key="k-1", wait=60)
            assert await tdfp.started(ASK, key="k-1")
    finally:
        await terminate_open_workflows(client, queue)
    # Each run is tried once.
    assert fakes.attempts == [1, 1]
    assert (first.create, first.kind, first.reason) == (None, "browser", "the browser failed")
    assert again.create is not None and again.create.tdfp_id == 40000


async def test_a_create_that_may_have_saved_is_kept_under_its_key(
    client: Client, queue: str
) -> None:
    """``not_created``, or a failure the driver does not raise before its save, is kept: the
    same key answers it again and never runs a second create (R-7025-7)."""
    fakes = Fakes(lambda: ApplicationError("lost", type="step_failed", non_retryable=True))
    tdfp = TemporalTdfp(client, queue)
    try:
        async with _workers(client, queue, fakes):
            first = await tdfp.create(ASK, key="k-3", wait=60)
            again = await tdfp.create(ASK, key="k-3", wait=60)
    finally:
        await terminate_open_workflows(client, queue)
    assert fakes.attempts == [1]
    assert first == again
    assert first.kind == "step_failed"
    assert first.reason == f"lost; {workflow.MAY_EXIST}"


def test_the_routes_wait_out_every_attempt() -> None:
    """R-7025-10: the search's wait is every attempt's queue wait and run, the back-off and
    the margin, from the same constants as the workflow's, so a retry never outlasts it."""
    for schedule, timeout, retry, wait in (
        (
            workflow.SEARCH_SCHEDULE_TO_START,
            workflow.SEARCH_TIMEOUT,
            workflow.SEARCH_RETRY,
            tdfp_module.SEARCH_WAIT,
        ),
        (
            workflow.CREATE_SCHEDULE_TO_START,
            workflow.CREATE_TIMEOUT,
            workflow.CREATE_RETRY,
            tdfp_module.CREATE_WAIT,
        ),
    ):
        attempts = retry.maximum_attempts
        assert attempts >= 1
        floor = attempts * (schedule + timeout) + workflow.WAIT_MARGIN
        assert wait >= floor.total_seconds()
    assert workflow.SEARCH_RETRY.maximum_attempts == 1
    assert (timedelta(minutes=4, seconds=30)).total_seconds() == tdfp_module.SEARCH_WAIT
    two = workflow.step_wait(
        timedelta(minutes=1),
        timedelta(minutes=3),
        RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=2),
    )
    assert two == (timedelta(minutes=8, seconds=35)).total_seconds()


async def test_a_create_sent_again_under_its_key_answers_the_first(
    client: Client, queue: str
) -> None:
    fakes = Fakes()
    tdfp = TemporalTdfp(client, queue)
    try:
        async with _workers(client, queue, fakes):
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
    assert outcome.reason is not None and "busy with another search" in outcome.reason
    assert fakes.searches == []


async def test_a_search_answers_while_a_preset_fetch_exports(
    client: Client, queue: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7062: a search sent while a preset fetch's export holds the worker's one fetch slot
    answers; it is not ``not_started`` ("busy with a preset fetch", the API's 503). Its
    activity runs on its own queue and slot, in the same process (R-7062-1)."""
    monkeypatch.setattr(workflow, "SEARCH_SCHEDULE_TO_START", timedelta(seconds=5))
    exporting, release = asyncio.Event(), asyncio.Event()

    @activity.defn(name=EXPORT_ACTIVITY)
    async def export(ask: FetchAsk) -> ExportReport:
        exporting.set()
        await release.wait()
        return REPORT

    _, import_, failed = FetchFakes().activities()
    fakes = Fakes()
    ask = _fetch_ask()
    try:
        # Unsandboxed, so the workflow reads the patched wait.
        async with _workers(
            client,
            queue,
            fakes,
            fetch=[export, import_, failed],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            await FetchStarter(client, queue).start(ask)
            await asyncio.wait_for(exporting.wait(), 30)
            try:
                outcome = await TemporalTdfp(client, queue).search(
                    SearchAsk(brand="SUNLU"), wait=60
                )
            finally:
                release.set()
            await _fetch_result(client, ask)
    finally:
        await terminate_open_workflows(client, queue)
    assert (outcome.kind, outcome.reason) == (None, None)
    assert outcome.search == SEARCH_ANSWER
    assert fakes.searches == [SearchAsk(brand="SUNLU")]
