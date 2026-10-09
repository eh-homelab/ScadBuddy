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
from typing import Any, cast

import httpx
import psycopg
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.bambuddy import follow as follow_module
from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.follow import FollowActivities, Follower, FollowInput
from scadbuddy.bambuddy.output_reader import LocalOutputs
from scadbuddy.bambuddy.progress import (
    PrintProgress,
    ProgressObserver,
    from_failed_run,
    progress_for,
)
from scadbuddy.bambuddy.runs import PrintRun, PrintRunError, PrintRunStore, newest_failure
from scadbuddy.bambuddy.subject import PrintSubject
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
        outputs=LocalOutputs(options.pop("outputs", OutputStore(paths)), cast(Any, None)),
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

    async def hook(subject: PrintSubject) -> None:
        heard.append((subject.id, kinds(seen)[-1]))

    follower.on_settled.append(hook)
    assert follow(follower) == "settled"
    assert heard == [(OUTPUT, "print.settled")]


def test_a_failing_settled_hook_does_not_fail_the_follow(paths: DataPaths) -> None:
    write_output(paths)
    follower, _ = follower_for(paths, Script(progress("done", settled=True)))
    ran: list[str] = []

    async def broken(subject: PrintSubject) -> None:
        raise RuntimeError("serial ABC123 leaked")

    async def after(subject: PrintSubject) -> None:
        ran.append(subject.id)

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

    async def hook(subject: PrintSubject) -> None:
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

    async def hook(subject: PrintSubject) -> None:
        await asyncio.sleep(0.2)
        finished.append(subject.id)

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


def test_the_activity_heartbeats_while_a_slow_read_runs(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1091 1: a read is two Bambuddy calls of up to 30 s each, longer than
    ``FOLLOW_HEARTBEAT``; without beats meanwhile each attempt would time out mid-read,
    and the retry read again, never reaching the error interval."""
    monkeypatch.setattr(follow_module, "HEARTBEAT_SLICE", 0.01)
    write_output(paths)

    async def slow(meta: OutputMeta) -> PrintProgress | None:
        await asyncio.sleep(0.2)
        return progress("done", settled=True)

    follower, _ = follower_for(paths, slow)
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
    assert reason == "settled"
    # A fresh first attempt reads at once: every beat is the read's, carrying the age.
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


def test_a_library_print_is_followed_until_it_settles_and_runs_the_hooks(
    paths: DataPaths,
) -> None:
    """#1073: a library file's print is read by its subject until it settles, then the
    settle hooks run with that subject, as an output's do."""
    read = Script(progress("queued"), progress("running"), progress("done", settled=True))
    follower, _ = follower_for(paths, Script(None), read_library=read)
    heard: list[str] = []

    async def hook(subject: PrintSubject) -> None:
        heard.append(subject.key)

    follower.on_settled.append(hook)
    ended = asyncio.run(asyncio.wait_for(follower.follow("library:89", NOW), 5))
    assert ended == "settled"
    assert read.reads == 3
    assert heard == ["library:89"]


def test_a_library_print_publishes_each_change_on_its_own_topic(paths: DataPaths) -> None:
    """#1751: a library file's print is announced as an output's is, under its run
    subject, so the dialog and the history follow it live."""
    read = Script(progress("queued"), progress("queued"), progress("done", settled=True))
    follower, seen = follower_for(paths, Script(None), read_library=read)
    asyncio.run(asyncio.wait_for(follower.follow("library:89", NOW), 5))
    events = [event for event in seen if isinstance(event, PrintEvent)]
    assert [event.kind for event in events] == ["print.progress", "print.progress", "print.settled"]
    assert {(event.output_id, event.slug) for event in events} == {("library:89", "library-89")}


def test_a_library_print_bambuddy_no_longer_has_ends_the_follow(paths: DataPaths) -> None:
    follower, seen = follower_for(
        paths, Script(None), read_library=Script(ApiError(404, "queue item gone"))
    )
    assert asyncio.run(asyncio.wait_for(follower.follow("library:89", NOW), 5)) == "gone"
    # The failure is announced, so an open dialog re-reads the route and shows it.
    assert kinds(seen) == ["print.progress"]


def test_without_a_library_reader_a_library_follow_ends_at_once(paths: DataPaths) -> None:
    follower, _ = follower_for(paths, Script(None))
    assert asyncio.run(asyncio.wait_for(follower.follow("library:89", NOW), 5)) == "gone"


FAILED_RUN = from_failed_run("the slice failed", bambuddy_url="http://bambuddy.test/queue")


def test_a_newer_runs_failure_is_published_not_the_older_print_it_follows(
    paths: DataPaths,
) -> None:
    """#1837: a newer run of the output failed before queueing while this follow's older
    print still moves. The route publishes that failure (``route: "run"``); the follow
    publishes the same, so the two never alternate, yet still reads the older print to
    its end, for the settled hooks."""
    write_output(paths)
    read = Script(progress("queued"), progress("running"), progress("done", settled=True))
    asked: list[str] = []

    async def newest_failed(run_subject: str) -> PrintProgress | None:
        asked.append(run_subject)
        return FAILED_RUN

    follower, seen = follower_for(paths, read, newest_failed=newest_failed)
    # The progress route already published the failure, through the same observer.
    follower.observer.observe_subject(OUTPUT, "demo", FAILED_RUN)
    seen.clear()
    heard: list[str] = []

    async def hook(subject: PrintSubject) -> None:
        heard.append(subject.id)

    follower.on_settled.append(hook)
    assert follow(follower) == "settled"
    assert read.reads == 3
    assert asked == [OUTPUT] * 3
    assert kinds(seen) == []  # nothing new to publish: no alternation
    assert heard == [OUTPUT]  # the older print's settle still ran its hooks


def test_without_a_newer_failure_the_follow_publishes_its_print(paths: DataPaths) -> None:
    write_output(paths)
    read = Script(progress("running"), progress("done", settled=True))

    async def newest_failed(run_subject: str) -> PrintProgress | None:
        return None

    follower, seen = follower_for(paths, read, newest_failed=newest_failed)
    assert follow(follower) == "settled"
    assert kinds(seen) == ["print.progress", "print.progress", "print.settled"]


def test_a_newest_failure_read_that_breaks_publishes_the_print_as_before(
    paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    write_output(paths)
    read = Script(progress("running"), progress("done", settled=True))

    async def newest_failed(run_subject: str) -> PrintProgress | None:
        raise RuntimeError("a bug")

    follower, seen = follower_for(paths, read, newest_failed=newest_failed)
    with caplog.at_level(logging.ERROR):
        assert follow(follower) == "settled"
    assert kinds(seen) == ["print.progress", "print.progress", "print.settled"]
    assert "the newest run's failure could not be read" in caplog.text


class _Runs:
    """`PrintRunStore` as `newest_failure` reads it."""

    def __init__(self, latest: PrintRun | Exception | None, *, available: bool = True) -> None:
        self.latest = latest
        self.available = available

    async def latest_for_output(self, output_id: str) -> PrintRun | None:
        if isinstance(self.latest, Exception):
            raise self.latest
        return self.latest


def _run(status: str, *, may_have_queued: bool = False) -> PrintRun:
    return PrintRun(
        id="r" * 32,
        subject=f"output:{OUTPUT}",
        status=status,  # type: ignore[arg-type]
        created_at=NOW,
        error=PrintRunError(status=502, title="Bad Gateway", detail="the slice failed")
        if status == "failed"
        else None,
        may_have_queued=may_have_queued,
    )


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        (_Runs(_run("failed")), "the slice failed"),
        (_Runs(_run("failed", may_have_queued=True)), None),  # its print says more
        (_Runs(_run("succeeded")), None),
        (_Runs(None), None),  # never run
        (_Runs(psycopg.OperationalError("down")), None),  # unreachable database
        (_Runs(_run("failed"), available=False), None),  # no database
    ],
    ids=["failed", "may-have-queued", "succeeded", "none", "unreachable", "no-database"],
)
def test_newest_failure(runs: _Runs, expected: str | None) -> None:
    assert asyncio.run(newest_failure(cast(PrintRunStore, runs), OUTPUT)) == expected
