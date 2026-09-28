"""The per-print watcher (#268): lifecycle, back-off, failures, resume, the lock."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import psycopg
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.progress import PrintProgress, ProgressObserver, progress_for
from scadbuddy.bambuddy.watcher import LocalWatchLock, PgPrintLog, PgWatchLock, PrintWatcher
from scadbuddy.core.events import Event, InProcessEventBus, PrintEvent
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import META_NAME, OutputMeta, OutputStore
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.pg_store import migrate
from tests.bambuddy.conftest import recording

OUTPUT = "c" * 32
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
FAST = {"min_interval": 0.01, "max_interval": 0.08, "error_interval": 0.02, "rescan_interval": 0}


def write_output(paths: DataPaths, output_id: str = OUTPUT, **extra: Any) -> OutputMeta:
    meta = OutputMeta(
        id=output_id,
        slug="demo",
        job_id="d" * 32,
        created_at=NOW - timedelta(hours=1),
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        print_route="pipeline",
        pipeline_run_id=12,
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


class MemoryPrintLog:
    """The ``PrintLog`` in a dict, standing in for ``print_watches``."""

    def __init__(self, at: dict[str, datetime] | None = None) -> None:
        self._at: dict[str, datetime] = dict(at or {})

    async def record(self, output_id: str, at: datetime) -> None:
        self._at[output_id] = at

    async def printed_at(self, output_id: str) -> datetime | None:
        return self._at.get(output_id)

    async def since(self, cutoff: datetime) -> list[str]:
        return [output_id for output_id, at in self._at.items() if at >= cutoff]

    async def forget(self, output_id: str) -> None:
        self._at.pop(output_id, None)

    async def aclose(self) -> None:
        return None


class FlakyLog(MemoryPrintLog):
    """A log whose database drops out for the first ``failures`` reads."""

    def __init__(self, failures: int, at: dict[str, datetime]) -> None:
        super().__init__(at)
        self.failures = failures

    async def printed_at(self, output_id: str) -> datetime | None:
        if self.failures > 0:
            self.failures -= 1
            raise OSError("connection lost")
        return await super().printed_at(output_id)


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path)
    data.ensure()
    return data


def watcher_for(
    paths: DataPaths,
    read: Callable[[OutputMeta], Awaitable[PrintProgress | None]],
    *,
    prints: MemoryPrintLog | Literal["none"] | None = None,
    **options: Any,
) -> tuple[PrintWatcher, list[Event]]:
    """A watcher whose log says ``OUTPUT`` was printed just now, unless given one."""
    bus = InProcessEventBus()
    seen: list[Event] = []
    bus.add_listener(seen.append)
    watcher = PrintWatcher(
        outputs=OutputStore(paths),
        observer=ProgressObserver(bus),
        read=read,
        events=bus,
        prints=None if prints == "none" else prints or MemoryPrintLog({OUTPUT: NOW}),
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


def test_a_watch_on_an_unrecorded_print_reads_it(paths: DataPaths) -> None:
    """Sent before the watcher existed, or recorded without a database: a watch (from
    a send or from someone reading its progress) still follows it."""

    async def scenario() -> Script:
        write_output(paths)
        read = Script(progress("done", settled=True))
        watcher, _ = watcher_for(paths, read, prints=MemoryPrintLog())
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return read

    assert asyncio.run(scenario()).reads == 1


def test_a_print_started_too_long_ago_is_not_resumed(paths: DataPaths) -> None:
    async def scenario() -> frozenset[str]:
        write_output(paths)
        log = MemoryPrintLog({OUTPUT: NOW - timedelta(hours=25)})
        watcher, _ = watcher_for(paths, Script(progress("running")), prints=log)
        await watcher.start()
        watching = watcher.watching
        await watcher.aclose()
        return watching

    assert asyncio.run(scenario()) == frozenset()


def test_a_long_print_that_keeps_moving_is_followed_past_the_age_limit(
    paths: DataPaths,
) -> None:
    async def scenario() -> Script:
        write_output(paths)
        clock = [NOW]
        stages = [progress("running", done=n) for n in range(4)]
        read = Script(*stages, progress("done", settled=True, done=4))

        async def reading(meta: OutputMeta) -> PrintProgress | None:
            clock[0] += timedelta(hours=10)  # each read is ten hours later
            return await read(meta)

        log = MemoryPrintLog({OUTPUT: NOW})
        watcher, _ = watcher_for(paths, reading, prints=log, max_age=timedelta(hours=24))
        watcher.now = lambda: clock[0]
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return read

    # 50 hours after it started, still followed: it moved at every read.
    assert asyncio.run(scenario()).reads == 5


def test_a_database_blip_does_not_end_the_watch(paths: DataPaths) -> None:
    async def scenario() -> Script:
        write_output(paths)
        read = Script(progress("done", settled=True))
        watcher, _ = watcher_for(paths, read, prints=FlakyLog(2, {OUTPUT: NOW}))
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return read

    assert asyncio.run(scenario()).reads == 1


def test_a_settled_print_is_forgotten_so_no_rescan_reads_it_again(paths: DataPaths) -> None:
    async def scenario() -> list[str]:
        write_output(paths)
        log = MemoryPrintLog({OUTPUT: NOW})
        watcher, _ = watcher_for(paths, Script(progress("done", settled=True)), prints=log)
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return await log.since(NOW - timedelta(hours=24))

    assert asyncio.run(scenario()) == []


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
        log = MemoryPrintLog({"1" * 32: NOW, "2" * 32: NOW - timedelta(hours=30)})
        watcher, _ = watcher_for(paths, Script(progress("running")), prints=log, min_interval=10)
        await watcher.start()
        watching = set(watcher.watching)
        await watcher.aclose()
        return watching

    assert asyncio.run(scenario()) == {"1" * 32}


def test_the_rescan_picks_up_a_print_nobody_follows(paths: DataPaths) -> None:
    async def scenario() -> set[str]:
        log = MemoryPrintLog()
        watcher, _ = watcher_for(
            paths, Script(progress("running")), prints=log, min_interval=10, rescan_interval=0.02
        )
        await watcher.start()
        assert watcher.watching == set()
        # Another replica started it, and died before its watcher read it.
        await log.record(OUTPUT, NOW)
        await asyncio.sleep(0.1)
        watching = set(watcher.watching)
        await watcher.aclose()
        return watching

    assert asyncio.run(scenario()) == {OUTPUT}


class SlowRelease(LocalWatchLock):
    """A release that takes long enough for a new print to arrive during it."""

    def __init__(self) -> None:
        self.releasing = asyncio.Event()
        self.acquired = 0

    async def acquire(self, output_id: str) -> bool:
        self.acquired += 1
        return True

    async def release(self, output_id: str) -> None:
        self.releasing.set()
        await asyncio.sleep(0.05)


def test_a_print_started_while_its_last_watch_ends_is_followed(paths: DataPaths) -> None:
    async def scenario() -> tuple[SlowRelease, Script]:
        write_output(paths)
        read = Script(progress("done", settled=True), progress("running"))
        lock = SlowRelease()
        watcher, _ = watcher_for(paths, read, lock=lock, min_interval=0.01, max_interval=10)
        watcher.watch(OUTPUT)
        await lock.releasing.wait()
        # The follower has settled and is releasing its lock: it reads no more.
        watcher.watch(OUTPUT)
        async with asyncio.timeout(5):
            while read.reads < 2:
                await asyncio.sleep(0.005)
        await watcher.aclose()
        return lock, read

    lock, read = asyncio.run(scenario())
    assert lock.acquired == 2
    assert read.reads >= 2


def test_without_a_database_a_started_print_is_followed_until_it_settles(
    paths: DataPaths,
) -> None:
    async def scenario() -> tuple[Script, set[str]]:
        write_output(paths)
        read = Script(progress("running"), progress("done", settled=True))
        watcher, _ = watcher_for(paths, read, prints="none")
        await watcher.start()
        resumed = set(watcher.watching)
        await watcher.started(OUTPUT)
        await until_idle(watcher)
        await watcher.aclose()
        return read, resumed

    read, resumed = asyncio.run(scenario())
    # Nothing to resume from, and the started print is read until it settles.
    assert resumed == set()
    assert read.reads == 2


def test_started_records_the_print_and_follows_it(paths: DataPaths) -> None:
    async def scenario() -> tuple[datetime | None, frozenset[str]]:
        write_output(paths)
        log = MemoryPrintLog()
        watcher, _ = watcher_for(paths, Script(progress("running")), prints=log, min_interval=10)
        await watcher.started(OUTPUT)
        watching = watcher.watching
        await watcher.aclose()
        return await log.printed_at(OUTPUT), watching

    assert asyncio.run(scenario()) == (NOW, frozenset({OUTPUT}))


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


@pytest.mark.requires_postgres
def test_the_postgres_print_log_keeps_the_latest_start(pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate(conn)

    async def scenario() -> tuple[datetime | None, datetime | None, list[str], list[str]]:
        log = PgPrintLog(pg_conninfo)
        try:
            await log.record(OUTPUT, NOW - timedelta(hours=30))
            await log.record(OUTPUT, NOW)
            await log.record("e" * 32, NOW - timedelta(hours=30))
            recent = await log.since(NOW - timedelta(hours=24))
            await log.forget(OUTPUT)
            return (
                await log.printed_at(OUTPUT),
                await log.printed_at("f" * 32),
                recent,
                await log.since(NOW - timedelta(hours=24)),
            )
        finally:
            await log.aclose()

    assert asyncio.run(scenario()) == (None, None, [OUTPUT], [])
