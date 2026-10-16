"""Replicas follow the models repository's HEAD over Postgres (#2507).

Pod A and pod B are two `Catalogue`s on one models directory, each with its own
`ModelHistory`; only B follows the channel. B's NFS cache is simulated: B's lookup
of a file under a directory answers from before A's write until something has opened
that directory -- the revalidation the follower makes on hearing A's commit.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
import pytest_asyncio

from scadbuddy.api.deps import announce_commits
from scadbuddy.core.events import Event, VersionCommitted
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.pg_events import PgNotifyEventBus
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.library.catalogue import Catalogue, ModelMeta
from scadbuddy.library.history import HeadChange, ModelHistory
from scadbuddy.library.repo_lock import (
    MAX_PAYLOAD_BYTES,
    PgRepoCoordinator,
    decode_change,
    encode_change,
)
from scadbuddy.library.repo_signal import ModelsHeadFollower
from scadbuddy.render.pg_store import migrate
from scadbuddy.render.solids import WRAPPER_PREFIX
from tests.support.media import MemoryMediaStore

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]

PROMPTLY = 10.0


async def _until(predicate: Callable[[], bool], timeout: float = PROMPTLY) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(root=tmp_path / "data")
    data.models.mkdir(parents=True)
    return data


@pytest.fixture
def coordinator(pg_conninfo: str) -> PgRepoCoordinator:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate(conn)
    return PgRepoCoordinator(pg_conninfo)


def pod(paths: DataPaths, coordinator: PgRepoCoordinator) -> Catalogue:
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX, coordinator=coordinator)
    return Catalogue(paths, history, media_store=MemoryMediaStore(), wrapper_prefix=WRAPPER_PREFIX)


def history_of(catalogue: Catalogue) -> ModelHistory:
    assert catalogue.history is not None
    return catalogue.history


class Follows:
    """Pod B's follower on a real LISTEN connection, and what it opened."""

    def __init__(self, paths: DataPaths, catalogue: Catalogue, pg_conninfo: str) -> None:
        self.listener = PgListener(pg_conninfo, check_interval=1.0, backoff=0.05, max_backoff=0.2)
        self.follower = ModelsHeadFollower(paths, history_of(catalogue), self.listener)
        self.follower.catalogue = catalogue
        self.connects: list[bool] = []
        self.listener.listen(
            "scadbuddy_test_marker", on_notify=lambda _p: None, on_connect=self.connects.append
        )


@pytest_asyncio.fixture
async def opened(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[Path]]:
    """Every directory opened from now on, as `revalidate_dirs` opens them."""
    seen: list[Path] = []
    real_open = os.open

    def spying(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & os.O_DIRECTORY:
            seen.append(Path(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spying)
    yield seen


async def test_pod_b_sees_pod_a_commit_as_soon_as_it_is_announced(
    paths: DataPaths, coordinator: PgRepoCoordinator, pg_conninfo: str, opened: list[Path]
) -> None:
    writer, reader = pod(paths, coordinator), pod(paths, coordinator)
    history_of(writer).ensure_repo()
    first = await asyncio.to_thread(
        writer.create, "keychain", "cube(10);\n", ModelMeta(name="Keychain")
    )
    # B has a cached revision for the template, keyed on the HEAD it read then.
    assert reader.ui_version("keychain") is None
    assert reader.caches_ui_version("keychain")
    follows = Follows(paths, reader, pg_conninfo)
    listening = asyncio.create_task(follows.listener.run())
    try:
        await _until(lambda: bool(follows.connects))
        opened.clear()

        await asyncio.to_thread(writer.create, "tag", "cube(5);\n", ModelMeta(name="Tag"))
        await asyncio.to_thread(
            writer.write_source, "keychain", "cube(20);\n", expected_version=first.version
        )
        head = history_of(writer).head()
        await _until(lambda: follows.follower.head == head)

        # The template B had never seen, and the one it had cached, were revalidated
        # on the announcements alone: no request reached B.
        assert paths.model_dir("tag") in opened
        assert paths.model_dir("keychain") in opened
        assert paths.models / ".git" / "refs" / "heads" in opened
        assert opened.index(paths.models) < opened.index(paths.model_dir("tag"))
        assert not reader.caches_ui_version("keychain")
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


async def test_a_commits_realtime_events_reach_pod_b_after_pod_b_has_refreshed(
    paths: DataPaths, coordinator: PgRepoCoordinator, pg_conninfo: str, opened: list[Path]
) -> None:
    # The events the writer publishes about a commit -- `version.committed` from its
    # commit hook, `source.changed` from its route -- are what an open page acts on
    # by reading again. On pod B they must arrive after B's refresh for that commit.
    writer, reader = pod(paths, coordinator), pod(paths, coordinator)
    history_of(writer).ensure_repo()
    publishes = PgNotifyEventBus(pg_conninfo, listener=PgListener(pg_conninfo, check_interval=1.0))
    history_of(writer).on_commit = announce_commits(publishes, writer)
    listener = PgListener(pg_conninfo, check_interval=1.0)
    follower = ModelsHeadFollower(paths, history_of(reader), listener)
    follower.catalogue = reader
    hears = PgNotifyEventBus(pg_conninfo, listener=listener)
    heard: list[tuple[str, str | None, bool]] = []

    def on_event(event: Event) -> None:
        if isinstance(event, VersionCommitted) and event.slug == "tag":
            heard.append((event.commit, follower.head, paths.model_dir("tag") in opened))

    hears.add_listener(on_event)
    await publishes.start()
    await hears.start()
    try:
        opened.clear()
        record = await asyncio.to_thread(writer.create, "tag", "cube(5);\n", ModelMeta(name="Tag"))
        await _until(lambda: bool(heard))

        assert heard == [(record.version, record.version, True)]
    finally:
        await hears.aclose()
        await publishes.aclose()


async def test_a_dropped_listen_connection_refreshes_every_template_on_reconnect(
    paths: DataPaths, coordinator: PgRepoCoordinator, pg_conninfo: str, opened: list[Path]
) -> None:
    writer, reader = pod(paths, coordinator), pod(paths, coordinator)
    history_of(writer).ensure_repo()
    for slug in ("keychain", "tag"):
        await asyncio.to_thread(writer.create, slug, "cube(1);\n", ModelMeta(name=slug))
    reader.ui_version("keychain")
    follows = Follows(paths, reader, pg_conninfo)
    opened.clear()
    listening = asyncio.create_task(follows.listener.run())
    try:
        await _until(lambda: follows.connects == [False])
        # A first connect has nothing to catch up on.
        assert paths.model_dir("keychain") not in opened
        dropped = follows.listener.backend_pid
        with psycopg.connect(pg_conninfo, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (dropped,))
        opened.clear()
        await _until(lambda: follows.connects == [False, True])

        # Whatever was announced while it was down is unknown: everything is looked at.
        assert {paths.model_dir("keychain"), paths.model_dir("tag")} <= set(opened)
        assert not reader.caches_ui_version("keychain")
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


def test_a_change_naming_too_many_templates_asks_for_everything() -> None:
    change = HeadChange("a" * 40, [f"model-{i}" for i in range(500)])
    payload = encode_change(change)

    assert len(payload.encode()) <= MAX_PAYLOAD_BYTES
    assert decode_change(payload) == HeadChange("a" * 40, None)
    small = HeadChange("b" * 40, ["keychain"])
    assert decode_change(encode_change(small)) == small
    with pytest.raises(ValueError):
        decode_change('{"head": 1}')


def test_a_change_too_large_to_name_says_why_everything_is_refreshed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#2619: the fallback to `models: null` costs every replica a whole-library refresh."""
    with caplog.at_level(logging.DEBUG, logger="scadbuddy.library.repo_lock"):
        encode_change(HeadChange("a" * 40, [f"model-{i}" for i in range(500)]))
    [record] = [r for r in caplog.records if "too many templates" in r.getMessage()]
    assert (record.commit, record.models) == ("a" * 40, 500)  # type: ignore[attr-defined]
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="scadbuddy.library.repo_lock"):
        encode_change(HeadChange("b" * 40, ["keychain"]))
    assert not caplog.records


class _NoListener:
    def __init__(self, check_interval: float = 30.0) -> None:
        self.check_interval = check_interval

    def listen(self, channel: str, **_: Any) -> None:
        pass


def test_a_refresh_is_slow_past_its_listeners_liveness_interval(paths: DataPaths) -> None:
    """#2617 review: the warning threshold is the interval of the check it holds up."""
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
    follower = ModelsHeadFollower(paths, history, cast(PgListener, _NoListener(check_interval=7.5)))
    assert follower.slow_after == 7.5


async def test_a_cancelled_refresh_retrieves_its_walks_failure(
    paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    """#2617 review: a listener cancelled mid-walk leaves the walk running; its failure
    is retrieved then, not reported as never retrieved."""
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
    follower = ModelsHeadFollower(paths, history, cast(PgListener, _NoListener()))
    follower.slow_after = 5.0
    entered = threading.Event()

    release = threading.Event()
    failing = threading.Event()

    def revalidate(model_ids: list[str] | None) -> None:
        entered.set()
        release.wait(PROMPTLY)
        failing.set()
        raise OSError("stale NFS file handle")

    follower.revalidate = revalidate  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        with caplog.at_level(logging.DEBUG, logger="scadbuddy.library.repo_signal"):
            refreshing = asyncio.create_task(follower.refresh(["keychain"]))
            await asyncio.to_thread(entered.wait, PROMPTLY)
            refreshing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await refreshing
            del refreshing
            # The walk fails only now, after the cancel (#3377: an event, not a sleep).
            release.set()
            assert await asyncio.to_thread(failing.wait, PROMPTLY)
            await _until(
                lambda: any(r.getMessage().startswith("a cancelled models") for r in caplog.records)
            )
            gc.collect()
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
    assert [c["message"] for c in reported] == []
    [settled] = [r for r in caplog.records if r.getMessage().startswith("a cancelled models")]
    assert settled.exc_info is not None and isinstance(settled.exc_info[1], OSError)


async def test_the_follower_refreshes_off_the_loop_and_moves_head_after(
    paths: DataPaths,
) -> None:
    """#2617: the NFS walk runs on a thread, the catalogue forgets those templates after
    it, and `head` names a commit only once this replica has revalidated for it."""
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
    follower = ModelsHeadFollower(paths, history, cast(PgListener, _NoListener()))
    seen: list[tuple[str, bool, str | None, list[str] | None]] = []
    loop_thread = threading.current_thread()

    def revalidate(model_ids: list[str] | None) -> None:
        seen.append(("walk", threading.current_thread() is loop_thread, follower.head, model_ids))

    class _Catalogue:
        def forget(self, model_ids: list[str] | None) -> None:
            seen.append(
                ("forget", threading.current_thread() is loop_thread, follower.head, model_ids)
            )

    follower.revalidate = revalidate  # type: ignore[method-assign]
    follower.catalogue = cast(Catalogue, _Catalogue())
    await follower._received(encode_change(HeadChange("c" * 40, ["keychain"])))
    await follower._connected(True)
    assert seen == [
        ("walk", False, None, ["keychain"]),
        ("forget", True, None, ["keychain"]),
        ("walk", False, "c" * 40, None),
        ("forget", True, "c" * 40, None),
    ]
    assert follower.head == "c" * 40


async def test_a_slow_refresh_is_said_while_it_runs_and_when_it_ends(
    paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    """#2617 review: a stuck mount holds up the shared listener, so it is a warning, not
    a debug line no default level shows; a quick refresh stays at debug."""
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
    follower = ModelsHeadFollower(paths, history, cast(PgListener, _NoListener()))
    follower.slow_after = 0.05
    delay = 0.3

    def revalidate(model_ids: list[str] | None) -> None:
        time.sleep(delay)

    follower.revalidate = revalidate  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG, logger="scadbuddy.library.repo_signal"):
        await follower.refresh(["keychain"])
    said = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert [level for level, _ in said] == [logging.WARNING, logging.WARNING]
    assert "still walking" in said[0][1]
    assert said[1][1] == "refreshed the models library"
    # #3375: the final record says the refresh stalled, so it is not counted twice.
    assert [getattr(r, "stalled", None) for r in caplog.records] == [None, True]

    caplog.clear()
    delay = 0.0
    # Generous: a thread-pool round trip on a loaded worker can outlast 0.05 s.
    follower.slow_after = 5.0
    with caplog.at_level(logging.DEBUG, logger="scadbuddy.library.repo_signal"):
        await follower.refresh(["keychain"])
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.DEBUG, "refreshed the models library")
    ]
    assert caplog.records[0].stalled is False  # type: ignore[attr-defined]


async def test_a_failed_refresh_still_forgets_but_leaves_head(paths: DataPaths) -> None:
    """#3375: a walk that raises (a stale NFS handle) still drops what the catalogue
    caches about those templates; `head` stays on the last commit it revalidated for."""
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
    follower = ModelsHeadFollower(paths, history, cast(PgListener, _NoListener()))
    follower.head = "a" * 40
    forgotten: list[list[str] | None] = []

    def revalidate(model_ids: list[str] | None) -> None:
        raise OSError("stale NFS file handle")

    class _Catalogue:
        def forget(self, model_ids: list[str] | None) -> None:
            forgotten.append(model_ids)

    follower.revalidate = revalidate  # type: ignore[method-assign]
    follower.catalogue = cast(Catalogue, _Catalogue())
    with pytest.raises(OSError, match="stale NFS"):
        await follower._received(encode_change(HeadChange("b" * 40, ["keychain"])))
    assert forgotten == [["keychain"]]
    assert follower.head == "a" * 40


async def test_the_listener_awaits_a_callback_before_the_next(pg_conninfo: str) -> None:
    """A callback's awaitable (slow work moved to a thread) is awaited in delivery
    order: what is NOTIFYed after it is not handed on before it finishes."""
    listener = PgListener(pg_conninfo, check_interval=1.0)
    order: list[str] = []
    connected = asyncio.Event()

    async def slow(payload: str) -> None:
        await asyncio.sleep(0.2)
        order.append(f"slow {payload}")

    listener.listen("scadbuddy_test_slow", on_notify=slow, on_connect=lambda _r: connected.set())
    listener.listen("scadbuddy_test_fast", on_notify=lambda p: order.append(f"fast {p}"))
    running = asyncio.create_task(listener.run())
    try:
        await asyncio.wait_for(connected.wait(), PROMPTLY)
        with psycopg.connect(pg_conninfo, autocommit=True) as conn, conn.transaction():
            conn.execute("NOTIFY scadbuddy_test_slow, '1'")
            conn.execute("NOTIFY scadbuddy_test_fast, '2'")
        await _until(lambda: len(order) == 2)
        assert order == ["slow 1", "fast 2"]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
