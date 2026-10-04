"""The print follow loop (#268, #1053): what `FollowPrint`'s activity does each pass,
with fast intervals and a scripted reader."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.bambuddy import follow as follow_module
from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.follow import FollowActivities, Follower, FollowInput
from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver, progress_for
from scadbuddy.core.events import Event, InProcessEventBus, PrintEvent
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import META_NAME, OutputMeta, OutputStore
from scadbuddy.render.glb import BoundingBox

OUTPUT = "c" * 32
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
FAST = {"min_interval": 0.01, "max_interval": 0.08, "error_interval": 0.02}


def write_output(paths: DataPaths, output_id: str = OUTPUT, **extra: Any) -> OutputMeta:
    meta = OutputMeta(
        id=output_id,
        slug="demo",
        job_id="d" * 32,
        created_at=NOW - timedelta(hours=1),
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        print_route="slice_queue",
        queue_item_id=51,
        **extra,
    )
    directory = paths.outputs / "demo" / output_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / META_NAME).write_text(json.dumps(meta.model_dump(mode="json")), encoding="utf-8")
    return meta


def progress(stage: str = "running", *, settled: bool = False, done: int = 0) -> PrintProgress:
    return PrintProgress(
        route="slice_queue",
        stage=stage,  # type: ignore[arg-type]
        settled=settled,
        queue_item_id=51,
        copies_completed=done,
        bambuddy_url="http://bambuddy.test/queue",
    )


class Script:
    """A reader that answers from a list, then repeats its last answer, and counts."""

    def __init__(self, *answers: PrintProgress | Exception | None) -> None:
        self.answers = list(answers)
        self.reads = 0

    async def __call__(self, meta: OutputMeta) -> PrintProgress | None:
        self.reads += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


class FlakyOutputs(OutputStore):
    """An output store whose disk drops out for the first ``failures`` reads."""

    def __init__(self, paths: DataPaths, failures: int) -> None:
        super().__init__(paths)
        self.failures = failures

    def get(self, output_id: str) -> OutputMeta:
        if self.failures > 0:
            self.failures -= 1
            raise OSError("read error")
        return super().get(output_id)


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path)
    data.ensure()
    return data


def follower_for(
    paths: DataPaths,
    read: Callable[[OutputMeta], Awaitable[PrintProgress | None]],
    **options: Any,
) -> tuple[Follower, list[Event]]:
    bus = InProcessEventBus()
    seen: list[Event] = []
    bus.add_listener(seen.append)
    follower = Follower(
        outputs=options.pop("outputs", OutputStore(paths)),
        observer=ProgressObserver(bus),
        read=read,
        events=bus,
        now=options.pop("now", lambda: NOW),
        **{**FAST, **options},
    )
    return follower, seen


def kinds(seen: list[Event]) -> list[str]:
    return [event.kind for event in seen if isinstance(event, PrintEvent)]


def follow(follower: Follower, active: datetime = NOW) -> str:
    return asyncio.run(asyncio.wait_for(follower.follow(OUTPUT, active), 5))


def test_follows_a_print_until_it_settles_publishing_each_change_once(paths: DataPaths) -> None:
    write_output(paths)
    read = Script(
        progress("queued"),
        progress("queued"),
        progress("running"),
        progress("done", settled=True, done=1),
    )
    follower, seen = follower_for(paths, read)
    assert follow(follower) == "settled"
    assert read.reads == 4
    # The repeated "queued" read published nothing.
    assert kinds(seen) == ["print.progress", "print.progress", "print.progress", "print.settled"]


def test_backs_off_while_nothing_changes(paths: DataPaths) -> None:
    write_output(paths)
    read = Script(progress("running"))
    follower, _ = follower_for(paths, read, min_interval=0.02, max_interval=0.16)

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(follower.follow(OUTPUT, NOW), 0.5)

    asyncio.run(scenario())
    # Waits of 0.02, 0.04, 0.08, 0.16, 0.16 …: about 5 reads in 0.5 s, not 25.
    assert 3 <= read.reads <= 7


def test_a_failure_is_announced_once_and_the_follow_carries_on(paths: DataPaths) -> None:
    write_output(paths)
    down = ApiError(502, "Bambuddy did not answer")
    read = Script(down, down, down, progress("done", settled=True))
    follower, seen = follower_for(paths, read)
    assert follow(follower) == "settled"
    assert read.reads == 4
    # One for the failure (the UI re-reads and sees the problem), then the settle.
    assert kinds(seen) == ["print.progress", "print.progress", "print.settled"]


def test_a_print_bambuddy_no_longer_has_ends_it(paths: DataPaths) -> None:
    write_output(paths)
    read = Script(ApiError(404, "no queue item 51"))
    follower, seen = follower_for(paths, read)
    assert follow(follower) == "gone"
    assert read.reads == 1
    assert kinds(seen) == ["print.progress"]


def test_an_unexpected_error_does_not_end_it(paths: DataPaths) -> None:
    write_output(paths)
    read = Script(RuntimeError("bug"), progress("done", settled=True))
    follower, _ = follower_for(paths, read)
    assert follow(follower) == "settled"
    assert read.reads == 2


def test_a_long_print_that_keeps_moving_is_followed_past_the_age_limit(
    paths: DataPaths,
) -> None:
    write_output(paths)
    clock = [NOW]
    read = Script(*[progress("running", done=n) for n in range(4)], progress("done", settled=True))

    async def reading(meta: OutputMeta) -> PrintProgress | None:
        clock[0] += timedelta(hours=10)  # each read is ten hours later
        return await read(meta)

    follower, _ = follower_for(paths, reading, max_age=timedelta(hours=24), now=lambda: clock[0])
    assert follow(follower) == "settled"
    # 50 hours after it started, still followed: it moved at every read.
    assert read.reads == 5


def test_a_quiet_print_is_given_up_on(paths: DataPaths) -> None:
    write_output(paths)
    clock = [NOW]
    read = Script(progress("running"))

    async def reading(meta: OutputMeta) -> PrintProgress | None:
        clock[0] += timedelta(hours=10)
        return await read(meta)

    follower, _ = follower_for(paths, reading, max_age=timedelta(hours=24), now=lambda: clock[0])
    assert follow(follower) == "quiet"
    # Moved once, then quiet for more than a day.
    assert read.reads == 4


def test_a_deleted_output_ends_it(paths: DataPaths) -> None:
    read = Script(progress("running"))
    follower, _ = follower_for(paths, read)
    assert follow(follower) == "deleted"
    assert read.reads == 0


def test_an_output_read_blip_does_not_end_it(paths: DataPaths) -> None:
    write_output(paths)
    read = Script(progress("done", settled=True))
    follower, _ = follower_for(paths, read, outputs=FlakyOutputs(paths, 2))
    assert follow(follower) == "settled"
    assert read.reads == 1


@respx.mock
def test_the_real_read_follows_a_failed_print_to_settled(paths: DataPaths) -> None:
    """Through ``progress_for`` and the client, as production reads: a queue item that
    failed is settled on the first read, so the follow ends there."""
    config = BambuddyConfig(base_url="http://bambuddy.test", api_key="bb_test")
    respx.get("http://bambuddy.test/api/v1/queue/51").mock(
        return_value=httpx.Response(
            200, json={"id": 51, "status": "failed", "error_message": "AMS slot empty"}
        )
    )

    async def read(meta: OutputMeta) -> PrintProgress | None:
        async with BambuddyClient(config) as client:
            return await progress_for(client, meta)

    write_output(paths)
    follower, seen = follower_for(paths, read)
    assert follow(follower) == "settled"
    assert kinds(seen) == ["print.progress", "print.settled"]


def test_the_activity_resumes_the_age_from_its_heartbeat(paths: DataPaths) -> None:
    """A retried attempt (a worker restart) keeps the age its last heartbeat carried,
    so a quiet print is not followed for another full day."""
    meta = write_output(paths)
    clock = [NOW]
    read = Script(progress("running"))

    async def reading(meta: OutputMeta) -> PrintProgress | None:
        clock[0] += timedelta(hours=10)
        return await read(meta)

    follower, _ = follower_for(paths, reading, max_age=timedelta(hours=24), now=lambda: clock[0])
    # Already seen before the restart: nothing it reads now is a change.
    follower.observer.observe(meta, progress("running"))
    env = ActivityEnvironment()
    env.info = replace(env.info, heartbeat_details=[(NOW - timedelta(hours=20)).isoformat()])
    beats: list[Any] = []
    env.on_heartbeat = lambda *details: beats.append(details)
    reason = asyncio.run(
        asyncio.wait_for(
            env.run(FollowActivities(follower).follow_print, FollowInput(output_id=OUTPUT)), 5
        )
    )
    assert reason == "quiet"
    # 20 hours quiet when it resumed: one more read, and it is given up on (a fresh
    # start would read three times).
    assert read.reads == 1
    assert beats, "the activity heartbeats while it waits"


def test_a_fresh_attempt_counts_its_age_from_now(paths: DataPaths) -> None:
    meta = write_output(paths)
    clock = [NOW]
    read = Script(progress("running"))

    async def reading(meta: OutputMeta) -> PrintProgress | None:
        clock[0] += timedelta(hours=10)
        return await read(meta)

    follower, _ = follower_for(paths, reading, max_age=timedelta(hours=24), now=lambda: clock[0])
    follower.observer.observe(meta, progress("running"))
    # A poke schedules a new activity: its first attempt has no heartbeat to resume.
    reason = asyncio.run(
        asyncio.wait_for(
            ActivityEnvironment().run(
                FollowActivities(follower).follow_print,
                FollowInput(output_id=OUTPUT, fresh=True),
            ),
            5,
        )
    )
    assert reason == "quiet"
    assert read.reads == 3


def test_a_fresh_read_that_finds_no_change_still_waits_between_reads(paths: DataPaths) -> None:
    """Review C1: a poked attempt reads at once, then backs off as any other."""
    meta = write_output(paths)
    read = Script(progress("running"))
    follower, _ = follower_for(paths, read, min_interval=0.05, max_interval=0.2)
    follower.observer.observe(meta, progress("running"))

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(follower.follow(OUTPUT, NOW, read_now=True), 0.5)

    asyncio.run(scenario())
    # Reads at 0, 0.05, 0.15, 0.35: four or so, never hundreds.
    assert 2 <= read.reads <= 6


def test_a_retried_fresh_attempt_resumes_the_age_and_waits(paths: DataPaths) -> None:
    """Review I3: `fresh` is the first attempt's; a retry keeps its heartbeat."""
    meta = write_output(paths)
    clock = [NOW]
    read = Script(progress("running"))

    async def reading(meta: OutputMeta) -> PrintProgress | None:
        clock[0] += timedelta(hours=10)
        return await read(meta)

    follower, _ = follower_for(paths, reading, max_age=timedelta(hours=24), now=lambda: clock[0])
    follower.observer.observe(meta, progress("running"))
    env = ActivityEnvironment()
    env.info = replace(
        env.info, attempt=2, heartbeat_details=[(NOW - timedelta(hours=20)).isoformat()]
    )
    reason = asyncio.run(
        asyncio.wait_for(
            env.run(
                FollowActivities(follower).follow_print,
                FollowInput(output_id=OUTPUT, fresh=True),
            ),
            5,
        )
    )
    assert reason == "quiet"
    assert read.reads == 1


def test_a_worker_shutdown_ends_the_attempt_at_once(paths: DataPaths) -> None:
    """Review I4: the attempt never runs out the worker's graceful shutdown; it is
    retried on another worker with its heartbeated age."""
    write_output(paths)
    follower, _ = follower_for(paths, Script(progress("running")), min_interval=60, max_interval=60)
    env = ActivityEnvironment()

    async def scenario() -> BaseException | None:
        attempt = asyncio.ensure_future(
            env.run(FollowActivities(follower).follow_print, FollowInput(output_id=OUTPUT))
        )
        await asyncio.sleep(0.05)
        env.worker_shutdown()
        try:
            await asyncio.wait_for(attempt, 2)
        except BaseException as error:
            return error
        return None

    error = asyncio.run(scenario())
    assert isinstance(error, ApplicationError) and not error.non_retryable


def test_a_cancelled_attempt_stops_reading(paths: DataPaths) -> None:
    """Review #1091 8: a poke cancels the running attempt; its inner follow ends with
    it, so it never reads beside the fresh attempt."""
    meta = write_output(paths)
    read = Script(progress("running"))
    follower, _ = follower_for(paths, read, min_interval=0.02, max_interval=0.02)
    follower.observer.observe(meta, progress("running"))
    inner = follower.follow
    ended: list[BaseException] = []

    async def following(*args: Any, **kwargs: Any) -> Any:
        try:
            return await inner(*args, **kwargs)
        except BaseException as error:
            ended.append(error)
            raise

    follower.follow = following  # type: ignore[method-assign]
    env = ActivityEnvironment()

    async def scenario() -> int:
        attempt = asyncio.ensure_future(
            env.run(FollowActivities(follower).follow_print, FollowInput(output_id=OUTPUT))
        )
        async with asyncio.timeout(2):
            while read.reads < 2:
                await asyncio.sleep(0.01)
        env.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(attempt, 2)
        await asyncio.sleep(0)
        reads = read.reads
        await asyncio.sleep(0.2)
        return reads

    reads = asyncio.run(scenario())
    assert len(ended) == 1 and isinstance(ended[0], asyncio.CancelledError)
    assert read.reads == reads


def test_a_settled_print_runs_each_settled_hook_once(paths: DataPaths) -> None:
    """#836: a feature hears that a print settled (the rack credits its hotends), after
    its ``print.settled`` is published."""
    write_output(paths)
    follower, seen = follower_for(
        paths, Script(progress("running"), progress("done", settled=True))
    )
    heard: list[tuple[str, str]] = []

    async def hook(meta: OutputMeta) -> None:
        heard.append((meta.id, kinds(seen)[-1]))

    follower.on_settled.append(hook)
    assert follow(follower) == "settled"
    assert heard == [(OUTPUT, "print.settled")]


def test_a_failing_settled_hook_does_not_fail_the_follow(paths: DataPaths) -> None:
    write_output(paths)
    follower, _ = follower_for(paths, Script(progress("done", settled=True)))
    ran: list[str] = []

    async def broken(meta: OutputMeta) -> None:
        raise RuntimeError("serial ABC123 leaked")

    async def after(meta: OutputMeta) -> None:
        ran.append(meta.id)

    follower.on_settled.extend([broken, after])
    assert follow(follower) == "settled"
    assert ran == [OUTPUT]


def test_a_settled_hook_that_hangs_is_cut_off_and_the_print_still_settles(
    paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    """#1083: a hook is awaited inside the follow, so one that never returns (a slow
    Bambuddy, a stuck pool) is bounded and logged like any other failure."""
    write_output(paths)
    follower, seen = follower_for(
        paths, Script(progress("done", settled=True, done=1)), settle_timeout=0.05
    )

    async def hook(meta: OutputMeta) -> None:
        await asyncio.Event().wait()

    follower.on_settled.append(hook)
    with caplog.at_level(logging.DEBUG):
        assert follow(follower) == "settled"
    assert kinds(seen) == ["print.progress", "print.settled"]
    [record] = [r for r in caplog.records if r.getMessage() == "a settled-print hook failed"]
    assert getattr(record, "error", None) == "TimeoutError"


def test_the_activity_heartbeats_while_a_settled_hook_runs(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1083: a hook longer than ``FOLLOW_HEARTBEAT`` must not time the attempt out
    before ``settle_timeout`` cuts it off, or the retry would run the hook again, and
    again: the follow never ends."""
    monkeypatch.setattr(follow_module, "HEARTBEAT_SLICE", 0.01)
    write_output(paths)
    follower, _ = follower_for(paths, Script(progress("done", settled=True)))
    finished: list[str] = []

    async def hook(meta: OutputMeta) -> None:
        await asyncio.sleep(0.2)
        finished.append(meta.id)

    follower.on_settled.append(hook)
    env = ActivityEnvironment()
    beats: list[Any] = []
    env.on_heartbeat = lambda *details: beats.append(details)
    reason = asyncio.run(
        asyncio.wait_for(
            env.run(
                FollowActivities(follower).follow_print,
                FollowInput(output_id=OUTPUT, fresh=True),
            ),
            5,
        )
    )
    assert reason == "settled" and finished == [OUTPUT]
    # A fresh first attempt reads at once: every beat is the hook's, carrying the age.
    assert len(beats) >= 2
    assert set(beats) == {(NOW.isoformat(),)}


def test_running_follows_are_counted_and_a_full_worker_is_said(
    paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    """Review #1091 2: a follow holds its slot for as long as the print moves; the
    gauge shows how many are held, and the last free slot taken is a warning, since
    the prints after it wait unfollowed."""
    write_output(paths)
    follower, _ = follower_for(paths, Script(progress("running")), min_interval=60, max_interval=60)
    metrics = Metrics()
    activities = FollowActivities(follower, slots=1, running=metrics.print_follows_running)
    env = ActivityEnvironment()

    def held() -> float | None:
        return metrics.registry.get_sample_value("scadbuddy_print_follows_running")

    async def scenario() -> float | None:
        attempt = asyncio.ensure_future(
            env.run(activities.follow_print, FollowInput(output_id=OUTPUT))
        )
        await asyncio.sleep(0.05)
        during = held()
        env.worker_shutdown()
        with pytest.raises(ApplicationError):
            await asyncio.wait_for(attempt, 2)
        return during

    with caplog.at_level(logging.WARNING, logger=follow_module.__name__):
        during = asyncio.run(scenario())
    assert during == 1
    assert held() == 0
    assert any("every follow slot" in r.message for r in caplog.records)
