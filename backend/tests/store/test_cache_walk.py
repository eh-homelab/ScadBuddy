"""The piece cache's size is walked once at a time and reused (#6354): in the API the
cache is on the shared NFS volume, and each `/metrics` scrape walked all of it."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.store import cache as cache_module
from scadbuddy.store.cache import CachedBlobStore
from scadbuddy.store.content import ContentStore
from scadbuddy.store.local import LocalBlobStore


def store(root: Path) -> CachedBlobStore:
    content = cast(ContentStore, SimpleNamespace(name="local"))
    return CachedBlobStore(LocalBlobStore(root), content, max_bytes=1 << 30, min_age=0)


def piece(root: Path, key: str, size: int) -> None:
    (root / key).mkdir(parents=True)
    (root / key / "piece.bin").write_bytes(b"x" * size)


def test_a_recent_walk_answers_and_an_old_one_is_walked_again(tmp_path: Path) -> None:
    blobs = store(tmp_path)
    piece(tmp_path, "a", 10)
    assert blobs.cached_bytes(max_age=300) == 10
    piece(tmp_path, "b", 5)
    assert blobs.cached_bytes(max_age=300) == 10
    assert blobs.cached_bytes() == 15


def test_a_call_while_another_walks_answers_the_last_total_and_starts_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = store(tmp_path)
    piece(tmp_path, "a", 10)
    assert blobs.cached_bytes() == 10
    inside, release = threading.Event(), threading.Event()
    walks = 0
    real_size = cache_module._size

    def slow_size(directory: Path) -> int:
        nonlocal walks
        walks += 1
        inside.set()
        release.wait(5)
        return real_size(directory)

    monkeypatch.setattr(cache_module, "_size", slow_size)
    piece(tmp_path, "b", 5)
    answers: list[Any] = []
    walker = threading.Thread(target=lambda: answers.append(blobs.cached_bytes()))
    walker.start()
    assert inside.wait(5)
    assert blobs.cached_bytes() == 10
    release.set()
    walker.join(5)
    assert answers == [15]
    assert walks == 2  # the one walk's two keys; the second call walked nothing


def test_before_the_first_walk_ends_a_second_call_answers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blobs = store(tmp_path)
    piece(tmp_path, "a", 10)
    inside, release = threading.Event(), threading.Event()

    def slow_size(directory: Path) -> int:
        inside.set()
        release.wait(5)
        return 10

    monkeypatch.setattr(cache_module, "_size", slow_size)
    walker = threading.Thread(target=blobs.cached_bytes)
    walker.start()
    assert inside.wait(5)
    assert blobs.cached_bytes() is None
    release.set()
    walker.join(5)
