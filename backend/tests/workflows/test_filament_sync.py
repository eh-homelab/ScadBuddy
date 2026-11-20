"""``FilamentBbSync`` (#6265 Task 8, plan `docs/superpowers/plans/2026-11-18-filament-bb-sync.md`
Decisions 11, 12 and 17, R-T7-1) on a dev server, over a stub of its pass activity."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowHandle
from temporalio.common import RetryPolicy
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from scadbuddy.filament.bb_sync_models import SYNC_PASS_ACTIVITY, SyncPassInput, SyncPassResult
from scadbuddy.filament.bb_sync_nudge import CHANGED_SIGNAL, SyncStart, workflow_id
from scadbuddy.workflows import filament_sync
from scadbuddy.workflows.filament_sync import DEBOUNCE, MAX_PASSES_IN_A_ROW, FilamentBbSync
from tests.support.temporal import temporal_client, terminate_open_workflows

pytestmark = pytest.mark.requires_temporal

INSTANCE = "0123456789ab"


@dataclass
class Call:
    full: bool
    interval_s: float
    run_id: str


class Passes:
    """The pass activity's stub: it records each call and answers what ``answer`` gives
    for the call's number (0 first)."""

    def __init__(self, answer: Callable[[int, SyncPassInput], SyncPassResult] | None = None):
        self.calls: list[Call] = []
        self.answer = answer or (lambda _n, start: result(start))
        self.during: Callable[[int], object] | None = None

    @activity.defn(name=SYNC_PASS_ACTIVITY)
    async def sync_pass(self, start: SyncPassInput) -> SyncPassResult:
        assert start.instance == INSTANCE
        n = len(self.calls)
        self.calls.append(Call(start.full, start.interval_s, activity.info().workflow_run_id or ""))
        if self.during is not None:
            pending = self.during(n)
            if asyncio.iscoroutine(pending):
                await pending
        return self.answer(n, start)


def result(start: SyncPassInput, **over: object) -> SyncPassResult:
    body: dict[str, object] = {
        "written": 0,
        "failed": 0,
        "deferred": 0,
        "more": False,
        "instance_changed": False,
        "interval_s": start.interval_s,
    } | over
    return SyncPassResult.model_validate(body)


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as connected:
        yield connected


@asynccontextmanager
async def serving(
    client: Client, passes: Passes, *, unsandboxed: bool = False
) -> AsyncIterator[str]:
    queue = f"filament-sync-{uuid.uuid4().hex[:8]}"
    runner = {"workflow_runner": UnsandboxedWorkflowRunner()} if unsandboxed else {}
    try:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[FilamentBbSync],
            activities=[passes.sync_pass],
            **runner,  # type: ignore[arg-type]
        ):
            yield queue
    finally:
        await terminate_open_workflows(client, queue)


async def start(
    client: Client, queue: str, interval_s: float, *, signal: bool
) -> WorkflowHandle[FilamentBbSync, None]:
    """Start the workflow as the nudger does (``signal``), or with no signal (the timer
    alone)."""
    return await client.start_workflow(
        FilamentBbSync.run,
        SyncStart(instance=INSTANCE, interval_s=interval_s),
        id=f"{workflow_id(INSTANCE)}-{queue}",
        task_queue=queue,
        start_signal=CHANGED_SIGNAL if signal else None,
        start_signal_args=[False] if signal else [],
    )


async def until(check: Callable[[], bool], timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)


async def test_a_signal_runs_one_debounced_pass(client: Client) -> None:
    passes = Passes()
    async with serving(client, passes) as queue:
        handle = await start(client, queue, 0, signal=True)
        await handle.signal(FilamentBbSync.changed, False)
        await handle.signal(FilamentBbSync.changed, False)
        await until(lambda: len(passes.calls) == 1)
        # The debounce folded all three in: nothing more comes after it.
        await asyncio.sleep(DEBOUNCE.total_seconds() + 1)
    assert [call.full for call in passes.calls] == [False]


async def test_the_timer_runs_a_full_pass(client: Client) -> None:
    passes = Passes()
    async with serving(client, passes) as queue:
        await start(client, queue, 1, signal=False)
        await until(lambda: len(passes.calls) >= 1)
    assert passes.calls[0].full is True
    assert passes.calls[0].interval_s == 1


async def test_interval_zero_runs_no_timer(client: Client) -> None:
    passes = Passes()
    async with serving(client, passes) as queue:
        handle = await start(client, queue, 0, signal=False)
        await asyncio.sleep(3)
        assert passes.calls == []
        await handle.signal(FilamentBbSync.changed, False)
        await until(lambda: len(passes.calls) == 1)
        # "Sync now" still runs a full pass.
        await handle.signal(FilamentBbSync.changed, True)
        await until(lambda: len(passes.calls) == 2)
        await asyncio.sleep(DEBOUNCE.total_seconds() + 1)
    assert [call.full for call in passes.calls] == [False, True]


async def test_more_runs_the_next_pass_at_once(client: Client) -> None:
    """R-T4-21, R-T7-1: on ``more`` the next pass runs at once, at most three back to back;
    then the workflow waits (here for a signal: no timer)."""
    passes = Passes(lambda _n, start: result(start, more=True, deferred=1))
    async with serving(client, passes) as queue:
        await start(client, queue, 0, signal=True)
        await until(lambda: len(passes.calls) == MAX_PASSES_IN_A_ROW)
        await asyncio.sleep(DEBOUNCE.total_seconds() + 1)
    assert len(passes.calls) == MAX_PASSES_IN_A_ROW == 3


async def test_the_interval_comes_back_from_the_pass(client: Client) -> None:
    """R-T7-3: a pass reports the stored interval, and the next one is sent it."""
    passes = Passes(lambda n, start: result(start, more=n == 0, interval_s=0 if n == 0 else 9))
    async with serving(client, passes) as queue:
        await start(client, queue, 300, signal=True)
        await until(lambda: len(passes.calls) == 2)
    assert [call.interval_s for call in passes.calls] == [300, 0]


@pytest.mark.parametrize(
    "ending",
    [{"instance_changed": True}, {"skipped": "no_bambuddy"}, {"skipped": "nothing_pushes"}],
    ids=["instance", "none", "nothing-pushes"],
)
async def test_a_workflow_for_an_old_instance_ends(
    client: Client, ending: dict[str, object]
) -> None:
    passes = Passes(lambda _n, start: result(start, **ending))
    async with serving(client, passes) as queue:
        handle = await start(client, queue, 0, signal=True)
        await asyncio.wait_for(handle.result(), 30)
    assert len(passes.calls) == 1


async def test_a_failed_pass_does_not_end_the_workflow(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass whose every attempt failed is logged; the next signal runs one again."""
    monkeypatch.setattr(filament_sync, "PASS_RETRY", RetryPolicy(maximum_attempts=1))

    def answer(n: int, start: SyncPassInput) -> SyncPassResult:
        if n == 0:
            raise RuntimeError("Bambuddy is down")
        return result(start)

    passes = Passes(answer)
    async with serving(client, passes, unsandboxed=True) as queue:
        handle = await start(client, queue, 0, signal=True)
        await until(lambda: len(passes.calls) == 1)
        await asyncio.sleep(1)
        assert (await handle.describe()).close_time is None
        await handle.signal(FilamentBbSync.changed, False)
        await until(lambda: len(passes.calls) == 2)


async def test_continue_as_new_keeps_a_pending_full(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "Sync now" that comes during the run's last pass is served by the next run, as a
    full pass."""
    monkeypatch.setattr(filament_sync, "CONTINUE_AFTER_PASSES", 1)
    passes = Passes()
    handle: WorkflowHandle[FilamentBbSync, None] | None = None

    async def sync_now(n: int) -> None:
        if n == 0:
            assert handle is not None
            await handle.signal(FilamentBbSync.changed, True)

    passes.during = sync_now
    async with serving(client, passes, unsandboxed=True) as queue:
        handle = await start(client, queue, 0, signal=True)
        await until(lambda: len(passes.calls) == 2)
    first, second = passes.calls
    assert (first.full, second.full) == (False, True)
    assert first.run_id != second.run_id
