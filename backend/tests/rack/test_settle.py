"""``record_settled`` and the rack's settle hook (#836, spec 2026-10-01 §4), on Postgres."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
import pytest
import respx

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import ArchiveDetail
from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore
from scadbuddy.bambuddy.progress import PrintProgress, progress_for
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.bambuddy.watcher import SETTLE_TIMEOUT
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.rack import usage
from scadbuddy.rack.usage import (
    SETTINGS_READ_TIMEOUT,
    PickedHotend,
    RackUsageStore,
    record_settled,
    settle_hook,
)
from tests.bambuddy.conftest import BASE_URL, recording
from tests.bambuddy.test_watcher import OUTPUT as WATCHED
from tests.bambuddy.test_watcher import (
    Script,
    kinds,
    progress,
    until_idle,
    watcher_for,
    write_output,
)
from tests.conftest import UNUSED_TEMPORAL_ADDRESS, PgPool, open_pg_pool
from tests.rack.helpers import serial

pytestmark = pytest.mark.requires_postgres

OUTPUT = "c" * 32
AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
A = serial(19)
API = f"{BASE_URL}/api/v1"


class Links:
    def __init__(self, *links: PrintLink) -> None:
        self.links = list(links)

    async def for_output(self, output_id: str) -> list[PrintLink]:
        return self.links


class Archives:
    def __init__(
        self,
        *archives: ArchiveDetail,
        failing: set[int] | None = None,
        hanging: set[int] | None = None,
    ) -> None:
        self.by_id = {archive.id: archive for archive in archives}
        self.failing = failing or set()
        self.hanging = hanging or set()
        self.reads: list[int] = []

    async def archive(self, archive_id: int) -> ArchiveDetail:
        self.reads.append(archive_id)
        if archive_id in self.hanging:
            await asyncio.Event().wait()
        if archive_id in self.failing:
            raise ApiError(503, f"archive {archive_id} unreadable near {A}")
        return self.by_id[archive_id]


def link(archive_id: int, queue_item_id: int | None) -> PrintLink:
    if queue_item_id is None:
        return PrintLink(archive_id=archive_id, matched_by="content_hash")
    return PrintLink(archive_id=archive_id, matched_by="queue_item", queue_item_id=queue_item_id)


@pytest.fixture
async def store(pg_conninfo: str) -> AsyncIterator[RackUsageStore]:
    opened = RackUsageStore(pg_conninfo)
    await opened.record_picks(51, 1, [PickedHotend(group_id=0, position=4, serial=A)])
    try:
        yield opened
    finally:
        opened.close()


async def settle(store: RackUsageStore, links: Links, archives: Archives) -> int:
    return await record_settled(OUTPUT, client=archives, links=links, store=store, now=lambda: AT)


async def test_each_linked_archive_of_a_picked_item_is_one_print(store: RackUsageStore) -> None:
    """A ``quantity`` 2 item: two archives, two prints."""
    archives = Archives(
        ArchiveDetail(
            status="completed",
            id=101,
            actual_time_seconds=600,
            print_time_seconds=900,
            filament_used_grams=5.0,
        ),
        ArchiveDetail(
            status="completed",
            id=102,
            actual_time_seconds=None,
            print_time_seconds=900,
            filament_used_grams=None,
        ),
    )
    assert await settle(store, Links(link(101, 51), link(102, 51)), archives) == 2
    usage = (await store.usage([A]))[A]
    assert (usage.prints, usage.print_seconds, usage.grams) == (2, 1500, 5.0)


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
async def test_a_failed_or_cancelled_print_with_an_archive_still_counts(
    store: RackUsageStore, outcome: str
) -> None:
    """Spec §10: the hotend wore whatever the outcome."""
    archives = Archives(ArchiveDetail(id=101, status=outcome, actual_time_seconds=120))
    assert await settle(store, Links(link(101, 51)), archives) == 1
    assert (await store.usage([A]))[A].print_seconds == 120


async def test_a_second_settle_of_the_same_print_changes_nothing(store: RackUsageStore) -> None:
    archives = Archives(ArchiveDetail(id=101, status="completed", actual_time_seconds=600))
    await settle(store, Links(link(101, 51)), archives)
    assert await settle(store, Links(link(101, 51)), archives) == 0
    assert (await store.usage([A]))[A].prints == 1


async def test_a_settle_reads_only_archives_not_yet_recorded(store: RackUsageStore) -> None:
    """claude-review on #1043, finding 3.3: an output printed N times must not cost N
    archive reads on every settle; an archive already counted is not read again."""
    first = ArchiveDetail(id=101, status="completed", actual_time_seconds=60)
    await settle(store, Links(link(101, 51)), Archives(first))
    await store.record_picks(52, 1, [PickedHotend(group_id=0, position=4, serial=A)])
    archives = Archives(first, ArchiveDetail(id=102, status="completed", actual_time_seconds=40))
    assert await settle(store, Links(link(101, 51), link(102, 52)), archives) == 1
    assert archives.reads == [102]


async def test_an_archive_with_no_queue_item_or_no_picks_is_not_counted(
    store: RackUsageStore,
) -> None:
    archives = Archives(ArchiveDetail(id=101), ArchiveDetail(id=103))
    assert await settle(store, Links(link(101, None), link(103, 99)), archives) == 0


async def test_a_second_print_of_one_output_counts_only_its_own_archives(
    store: RackUsageStore,
) -> None:
    """Review Focus 5: the second settle walks the first print's archive too."""
    await settle(
        store,
        Links(link(101, 51)),
        Archives(ArchiveDetail(id=101, status="completed", actual_time_seconds=60)),
    )
    await store.record_picks(52, 1, [PickedHotend(group_id=0, position=4, serial=A)])
    archives = Archives(
        ArchiveDetail(id=101, status="completed", actual_time_seconds=9999),
        ArchiveDetail(id=102, status="completed", actual_time_seconds=40),
    )
    assert await settle(store, Links(link(101, 51), link(102, 52)), archives) == 1
    usage = (await store.usage([A]))[A]
    assert (usage.prints, usage.print_seconds) == (2, 100)


@pytest.mark.parametrize("status", ["printing", "paused", None])
async def test_another_print_still_running_is_left_for_its_own_settle(
    store: RackUsageStore, status: str | None
) -> None:
    """claude-review on #1043, finding 1: the output's other print, still running, is not
    credited with its estimate (``DO NOTHING`` would freeze it); its own settle counts it."""
    await store.record_picks(52, 1, [PickedHotend(group_id=0, position=4, serial=A)])
    running = ArchiveDetail(id=101, status=status, print_time_seconds=9999, filament_used_grams=9.0)
    done = ArchiveDetail(id=102, status="completed", actual_time_seconds=40)
    links = Links(link(101, 51), link(102, 52))
    assert await settle(store, links, Archives(running, done)) == 1
    assert (await store.usage([A]))[A].print_seconds == 40

    finished = ArchiveDetail(id=101, status="completed", actual_time_seconds=600)
    assert await settle(store, links, Archives(finished, done)) == 1
    assert (await store.usage([A]))[A].print_seconds == 640


async def test_an_unreadable_archive_is_logged_by_type_and_the_rest_are_written(
    store: RackUsageStore, caplog: pytest.LogCaptureFixture
) -> None:
    archives = Archives(
        ArchiveDetail(id=102, status="completed", actual_time_seconds=40), failing={101}
    )
    with caplog.at_level(logging.DEBUG):
        assert await settle(store, Links(link(101, 51), link(102, 51)), archives) == 1
    [record] = [r for r in caplog.records if r.name == "scadbuddy.rack.usage"]
    assert record.getMessage() == "could not record a rack nozzle's print"
    assert (
        getattr(record, "output_id", None),
        getattr(record, "archive_id", None),
        getattr(record, "error", None),
    ) == (OUTPUT, 101, "ApiError")
    assert A not in repr(record.__dict__) and record.exc_info is None


async def test_an_archive_that_stalls_costs_only_itself(
    store: RackUsageStore, caplog: pytest.LogCaptureFixture
) -> None:
    """#1086 review: a stall on one archive must not use up the watcher's whole-hook
    timeout and lose the archives after it."""
    archives = Archives(
        ArchiveDetail(id=102, status="completed", actual_time_seconds=40), hanging={101}
    )
    with caplog.at_level(logging.DEBUG):
        written = await record_settled(
            OUTPUT,
            client=archives,
            links=Links(link(101, 51), link(102, 51)),
            store=store,
            now=lambda: AT,
            archive_timeout=0.1,
        )
    assert written == 1
    assert archives.reads == [101, 102]
    [record] = [r for r in caplog.records if r.name == "scadbuddy.rack.usage"]
    assert (getattr(record, "archive_id", None), getattr(record, "error", None)) == (
        101,
        "TimeoutError",
    )


async def test_a_settle_cut_off_mid_write_still_records_that_archive(
    store: RackUsageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1086 review: the watcher's timeout cancels the hook, not the write already
    running in its thread. That archive is recorded anyway, as SETTLE_TIMEOUT says."""
    writing = threading.Event()
    cancelled = threading.Event()
    written = threading.Event()
    write = store._record_prints

    def held_write(*args: object, **kwargs: object) -> int:
        # The write starts, then waits until the hook has been cancelled, so the cut-off
        # happens strictly while it is running.
        writing.set()
        cancelled.wait(10)
        try:
            return write(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            written.set()

    monkeypatch.setattr(store, "_record_prints", held_write)
    archives = Archives(ArchiveDetail(id=101, status="completed", actual_time_seconds=40))
    hook = asyncio.ensure_future(settle(store, Links(link(101, 51)), archives))
    assert await asyncio.to_thread(writing.wait, 10)
    hook.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hook
    cancelled.set()

    assert await asyncio.to_thread(written.wait, 10)
    assert await store.recorded_archives([101]) == {101}
    assert (await store.usage([A]))[A].prints == 1


class FailingLinks:
    async def for_output(self, output_id: str) -> list[PrintLink]:
        raise RuntimeError(f"connection lost near {A}")


async def test_unreadable_links_are_logged_by_type_and_nothing_is_written(
    store: RackUsageStore, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        written = await record_settled(
            OUTPUT, client=Archives(), links=FailingLinks(), store=store, now=lambda: AT
        )
    assert written == 0
    [record] = [r for r in caplog.records if r.name == "scadbuddy.rack.usage"]
    assert getattr(record, "error", None) == "RuntimeError"
    assert A not in repr(record.__dict__) and record.exc_info is None


# --- The fast print (revised P3): dispatched and settled inside one backed-off poll ---


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[PgPool]:
    opened = open_pg_pool(pg_conninfo, size=2)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path)
    data.ensure()
    return data


@respx.mock
async def test_a_print_that_dispatches_and_settles_in_one_poll_is_counted(
    store: RackUsageStore, pool: PgPool, paths: DataPaths
) -> None:
    """The first read the watcher makes finds the item already finished. That read is
    also the one that links its archive: ``progress_for`` records the queue item's
    ``archive_id`` before it returns the settled progress, so the hook, which runs after,
    finds the link though nothing linked the print before."""
    links = PrintLinkStore(pool)
    uploads = BambuddyUploadStore(pool)
    settings = StoredSettings(bambuddy_url=BASE_URL, bambuddy_api_key="bb_test")
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(
            200,
            json={
                **recording("queue-item.json"),
                "id": 51,
                "status": "completed",
                "archive_id": 101,
            },
        )
    )
    respx.get(f"{API}/archives/101").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 101,
                "status": "completed",
                "actual_time_seconds": 75,
                "filament_used_grams": 1.5,
            },
        )
    )

    async def read(meta: OutputMeta) -> PrintProgress | None:
        async with client_for(settings) as client:
            return await progress_for(client, meta, uploads=uploads, links=links)

    write_output(paths)
    assert await links.for_output(OUTPUT) == []
    watcher, seen = watcher_for(paths, read)
    watcher.on_settled.append(settle_hook(store, links, lambda _timeout: settings))
    watcher.watch(OUTPUT)
    await until_idle(watcher)

    assert kinds(seen) == ["print.progress", "print.settled"]
    usage = (await store.usage([A]))[A]
    assert (usage.prints, usage.print_seconds, usage.grams) == (1, 75, 1.5)


async def test_the_hook_passes_its_read_timeout_and_a_failed_read_spares_the_next(
    store: RackUsageStore, pool: PgPool
) -> None:
    """#1111: the hook passes ``SETTINGS_READ_TIMEOUT`` to its settings read, and a read
    that failed does not stop the next settle from reading. The stub cannot show the
    bound ends a read; ``test_a_real_stuck_settings_read_gives_its_thread_back`` does."""
    asked: list[float] = []

    def load(timeout: float) -> StoredSettings:
        asked.append(timeout)
        if len(asked) == 1:
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
        return StoredSettings(bambuddy_url=BASE_URL, bambuddy_api_key="bb_test")

    hook = settle_hook(store, PrintLinkStore(pool), load)
    with pytest.raises(psycopg.errors.QueryCanceled):
        await hook(OutputMeta.model_construct(id=OUTPUT))
    with respx.mock(base_url=BASE_URL, assert_all_called=False):
        await hook(OutputMeta.model_construct(id=OUTPUT))
    assert asked == [SETTINGS_READ_TIMEOUT, SETTINGS_READ_TIMEOUT]
    assert SETTINGS_READ_TIMEOUT < SETTLE_TIMEOUT


async def test_a_real_stuck_settings_read_gives_its_thread_back(
    store: RackUsageStore,
    pool: PgPool,
    tmp_path: Path,
    pg_conninfo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1111, end to end: the hook's read through the real settings store, held on a
    locked table, ends within the bound, so its thread returns to the executor."""
    monkeypatch.setattr(usage, "SETTINGS_READ_TIMEOUT", 0.2)
    settings_store = SettingsStore(
        Settings(
            data_dir=tmp_path, database_url=pg_conninfo, temporal_address=UNUSED_TEMPORAL_ADDRESS
        )
    )
    settings_store.open()
    try:
        hook = settle_hook(store, PrintLinkStore(pool), settings_store.load)
        with psycopg.connect(pg_conninfo) as holder, holder.transaction():
            holder.execute("LOCK TABLE settings IN ACCESS EXCLUSIVE MODE")
            started = time.monotonic()
            with pytest.raises(psycopg.errors.QueryCanceled):
                await asyncio.wait_for(hook(OutputMeta.model_construct(id=OUTPUT)), timeout=30)
            elapsed = time.monotonic() - started
    finally:
        settings_store.close()
    # Ended at the hook's bound, not the real SETTINGS_READ_TIMEOUT or the wait_for.
    assert elapsed < 2


async def test_a_settings_read_that_blocks_is_cut_off_with_the_hook(
    store: RackUsageStore, pool: PgPool, paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    """#1083: the hook's settings read is a blocking database read. Run on the event
    loop it would freeze the watch, and the watcher's timeout could never fire."""
    release, returned = threading.Event(), threading.Event()
    settings = StoredSettings(bambuddy_url=BASE_URL, bambuddy_api_key="bb_test")

    def load(timeout: float) -> StoredSettings:
        release.wait(timeout=10)
        returned.set()
        return settings

    write_output(paths)
    watcher, seen = watcher_for(paths, Script(progress("done", settled=True, done=1)))
    watcher.settle_timeout = 0.1
    watcher.on_settled.append(settle_hook(store, PrintLinkStore(pool), load))
    try:
        with caplog.at_level(logging.DEBUG):
            watcher.watch(WATCHED)
            await until_idle(watcher)
            # The watch finished while the read was still blocked: it never froze the loop.
            assert not returned.is_set()
    finally:
        release.set()

    assert kinds(seen) == ["print.progress", "print.settled"]
    [record] = [r for r in caplog.records if r.getMessage() == "a settled-print hook failed"]
    assert getattr(record, "error", None) == "TimeoutError"
