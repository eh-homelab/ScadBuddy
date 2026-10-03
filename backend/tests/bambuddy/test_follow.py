"""The print follow loop (#268, #1053): what `FollowPrint`'s activity does each pass,
with fast intervals and a scripted reader."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from temporalio.testing import ActivityEnvironment

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.follow import FollowActivities, Follower, FollowInput
from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver, progress_for
from scadbuddy.core.events import Event, InProcessEventBus, PrintEvent
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
    env = ActivityEnvironment()
    env.info = replace(env.info, heartbeat_details=[(NOW - timedelta(hours=20)).isoformat()])
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
    assert read.reads == 3
