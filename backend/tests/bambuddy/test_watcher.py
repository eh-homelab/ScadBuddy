"""The per-print watcher (#268): lifecycle, back-off, failures, resume, the lock."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver, progress_for
from scadbuddy.bambuddy.watcher import LocalWatchLock, PgWatchLock, PrintWatcher
from scadbuddy.core.events import Event, InProcessEventBus, PrintEvent
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import META_NAME, OutputMeta, OutputStore
from scadbuddy.render.glb import BoundingBox
from tests.bambuddy.conftest import recording

OUTPUT = "c" * 32
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
FAST = {"min_interval": 0.01, "max_interval": 0.08, "error_interval": 0.02, "rescan_interval": 0}


def write_output(
    paths: DataPaths,
    output_id: str = OUTPUT,
    *,
    printed_at: datetime | None = NOW,
    **extra: Any,
) -> OutputMeta:
    meta = OutputMeta(
        id=output_id,
        slug="demo",
        job_id="d" * 32,
        created_at=NOW - timedelta(hours=1),
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        print_route="pipeline",
        pipeline_run_id=12,
        printed_at=printed_at,
        **extra,
    )
    directory = paths.outputs / "demo" / output_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / META_NAME).write_text(json.dumps(meta.model_dump(mode="json")), encoding="utf-8")
    return meta


def progress(stage: str = "running", *, settled: bool = False, done: int = 0) -> PrintProgress:
    return PrintProgress(
        route="pipeline",
        stage=stage,  # type: ignore[arg-type]
        settled=settled,
        pipeline_run_id=12,
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


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path)
    data.ensure()
    return data


def watcher_for(
    paths: DataPaths, read: Callable[[OutputMeta], Awaitable[PrintProgress | None]], **options: Any
) -> tuple[PrintWatcher, list[Event]]:
    bus = InProcessEventBus()
    seen: list[Event] = []
    bus.add_listener(seen.append)
    watcher = PrintWatcher(
        outputs=OutputStore(paths),
        observer=ProgressObserver(bus),
        read=read,
        events=bus,
        now=lambda: NOW,
        **{**FAST, **options},
    )
    return watcher, seen


async def until_idle(watcher: PrintWatcher, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while watcher.watching:
            await asyncio.sleep(0.005)


def kinds(seen: list[Event]) -> list[str]:
    return [event.kind for event in seen if isinstance(event, PrintEvent)]


def test_follows_a_print_until_it_settles_publishing_each_change_once(paths: DataPaths) -> None:
    async def scenario() -> tuple[Script, list[Event]]:
        write_output(paths)
        read = Script(
            progress("queued"),
            progress("queued"),
            progress("running"),
            progress("done", settled=True, done=1),
        )
        watcher, seen = watcher_for(paths, read)
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return read, seen

    read, seen = asyncio.run(scenario())
    assert read.reads == 4
    # The repeated "queued" read published nothing.
    assert kinds(seen) == ["print.progress", "print.progress", "print.progress", "print.settled"]


def test_backs_off_while_nothing_changes(paths: DataPaths) -> None:
    async def scenario() -> Script:
        write_output(paths)
        read = Script(progress("running"))
        watcher, _ = watcher_for(paths, read, min_interval=0.02, max_interval=0.16)
        watcher.watch(OUTPUT)
        await asyncio.sleep(0.5)
        await watcher.aclose()
        return read

    # Waits of 0.02, 0.04, 0.08, 0.16, 0.16 …: about 5 reads in 0.5 s, not 25.
    assert 3 <= asyncio.run(scenario()).reads <= 7


def test_watching_twice_follows_once_and_reads_again_now(paths: DataPaths) -> None:
    async def scenario() -> Script:
        write_output(paths)
        read = Script(progress("running"))
        watcher, _ = watcher_for(paths, read, min_interval=10, max_interval=10)
        watcher.watch(OUTPUT)
        watcher.watch(OUTPUT)
        await asyncio.sleep(0.05)
        assert watcher.watching == {OUTPUT}
        await watcher.aclose()
        return read

    assert asyncio.run(scenario()).reads == 1


def test_a_failure_is_announced_once_and_the_watch_carries_on(paths: DataPaths) -> None:
    async def scenario() -> tuple[Script, list[Event]]:
        write_output(paths)
        down = ApiError(502, "Bambuddy did not answer")
        read = Script(down, down, down, progress("done", settled=True))
        watcher, seen = watcher_for(paths, read)
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return read, seen

    read, seen = asyncio.run(scenario())
    assert read.reads == 4
    # One for the failure (the UI re-reads and sees the problem), then the settle.
    assert kinds(seen) == ["print.progress", "print.progress", "print.settled"]


def test_a_print_bambuddy_no_longer_has_ends_the_watch(paths: DataPaths) -> None:
    async def scenario() -> tuple[Script, list[Event]]:
        write_output(paths)
        read = Script(ApiError(404, "no pipeline run 12"))
        watcher, seen = watcher_for(paths, read)
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return read, seen

    read, seen = asyncio.run(scenario())
    assert read.reads == 1
    assert kinds(seen) == ["print.progress"]


def test_an_unexpected_error_does_not_end_the_watch(paths: DataPaths) -> None:
    async def scenario() -> Script:
        write_output(paths)
        read = Script(RuntimeError("bug"), progress("done", settled=True))
        watcher, _ = watcher_for(paths, read)
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return read

    assert asyncio.run(scenario()).reads == 2


@pytest.mark.parametrize(
    "printed_at", [None, NOW - timedelta(hours=25)], ids=["never-printed", "too-old"]
)
def test_a_print_that_is_not_live_is_not_read(
    paths: DataPaths, printed_at: datetime | None
) -> None:
    async def scenario() -> Script:
        write_output(paths, printed_at=printed_at)
        read = Script(progress("running"))
        watcher, _ = watcher_for(paths, read)
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return read

    assert asyncio.run(scenario()).reads == 0


def test_a_deleted_output_ends_the_watch(paths: DataPaths) -> None:
    async def scenario() -> Script:
        read = Script(progress("running"))
        watcher, _ = watcher_for(paths, read)
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return read

    assert asyncio.run(scenario()).reads == 0


def test_start_resumes_recent_prints_after_a_restart(paths: DataPaths) -> None:
    async def scenario() -> set[str]:
        write_output(paths, "1" * 32)
        write_output(paths, "2" * 32, printed_at=NOW - timedelta(hours=30))
        write_output(paths, "3" * 32, printed_at=None)
        watcher, _ = watcher_for(paths, Script(progress("running")), min_interval=10)
        await watcher.start()
        watching = set(watcher.watching)
        await watcher.aclose()
        return watching

    assert asyncio.run(scenario()) == {"1" * 32}


def test_the_rescan_picks_up_a_print_nobody_follows(paths: DataPaths) -> None:
    async def scenario() -> set[str]:
        watcher, _ = watcher_for(
            paths, Script(progress("running")), min_interval=10, rescan_interval=0.02
        )
        await watcher.start()
        assert watcher.watching == set()
        write_output(paths)
        await asyncio.sleep(0.1)
        watching = set(watcher.watching)
        await watcher.aclose()
        return watching

    assert asyncio.run(scenario()) == {OUTPUT}


class Refusing(LocalWatchLock):
    """Another replica holds every print."""

    async def acquire(self, output_id: str) -> bool:
        return False


def test_a_print_another_process_holds_is_left_to_it(paths: DataPaths) -> None:
    async def scenario() -> Script:
        write_output(paths)
        read = Script(progress("running"))
        watcher, _ = watcher_for(paths, read, lock=Refusing())
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return read

    assert asyncio.run(scenario()).reads == 0


@respx.mock
def test_the_real_read_follows_a_recorded_failed_slice_to_settled(paths: DataPaths) -> None:
    """Through ``progress_for`` and the client, as production reads: the recorded run
    whose slice failed is settled on the first read, so the watch ends there."""
    config = BambuddyConfig(base_url="http://bambuddy.test", api_key="bb_test")
    respx.get("http://bambuddy.test/api/v1/pipeline-runs/12").mock(
        return_value=httpx.Response(200, json=recording("pipeline-run.json"))
    )

    async def read(meta: OutputMeta) -> PrintProgress | None:
        async with BambuddyClient(config) as client:
            return await progress_for(client, meta)

    async def scenario() -> list[Event]:
        write_output(paths)
        watcher, seen = watcher_for(paths, read)
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return seen

    assert kinds(asyncio.run(scenario())) == ["print.progress", "print.settled"]


@pytest.mark.requires_postgres
def test_the_postgres_lock_lets_one_process_follow_each_print(pg_conninfo: str) -> None:
    async def scenario() -> tuple[bool, bool, bool, bool]:
        first, second = PgWatchLock(pg_conninfo), PgWatchLock(pg_conninfo)
        try:
            a = await first.acquire(OUTPUT)
            b = await second.acquire(OUTPUT)
            other = await second.acquire("e" * 32)
            await first.release(OUTPUT)
            c = await second.acquire(OUTPUT)
            return a, b, other, c
        finally:
            await first.aclose()
            await second.aclose()

    assert asyncio.run(scenario()) == (True, False, True, True)


@pytest.mark.requires_postgres
def test_a_dead_process_releases_its_prints(pg_conninfo: str) -> None:
    async def scenario() -> bool:
        dead, alive = PgWatchLock(pg_conninfo), PgWatchLock(pg_conninfo)
        try:
            assert await dead.acquire(OUTPUT)
            await dead.aclose()
            return await alive.acquire(OUTPUT)
        finally:
            await alive.aclose()

    assert asyncio.run(scenario()) is True
