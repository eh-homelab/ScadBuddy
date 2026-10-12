"""Replicas follow the models repository's HEAD over Postgres (#2507).

Pod A and pod B are two `Catalogue`s on one models directory, each with its own
`ModelHistory`; only B follows the channel. B's NFS cache is simulated: B's lookup
of a file under a directory answers from before A's write until something has opened
that directory -- the revalidation the follower makes on hearing A's commit.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

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
    assert "keychain" in reader._ui_versions
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
        assert "keychain" not in reader._ui_versions
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
        assert reader._ui_versions == {}
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
