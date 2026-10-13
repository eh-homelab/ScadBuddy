"""#2788: whole Bambuddy downloads kept by sha256 on a volume processes share."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.store.content import BlobCorruptError, BlobScope, ContentStore
from scadbuddy.store.download_cache import PARTIAL_MAX_AGE, DownloadCache, TooLargeError, prune
from scadbuddy.store.index import BlobIndex, Pool
from scadbuddy.store.local import LocalContentBackend

SCOPE = BlobScope(slug="demo", title="Demo")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _aged(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


# --- the cache itself ----------------------------------------------------------------


def test_a_fill_is_kept_only_when_its_bytes_are_the_ones_named(tmp_path: Path) -> None:
    cache = DownloadCache(tmp_path, max_bytes=1 << 20)
    good = b"the file"
    with cache.fill(_sha(good)) as fill:
        fill.write(good)
        assert fill.commit()
    with cache.fill(_sha(b"what Bambuddy said it was")) as fill:
        fill.write(b"what it sent")
        assert not fill.commit()
    assert sorted(p.name for p in tmp_path.iterdir()) == [_sha(good)]
    handle = cache.open(_sha(good), what="test")
    assert handle is not None
    with handle:
        assert handle.read() == good


def test_an_unfinished_fill_leaves_nothing_a_reader_finds(tmp_path: Path) -> None:
    cache = DownloadCache(tmp_path, max_bytes=1 << 20)
    data = b"half"
    with pytest.raises(RuntimeError), cache.fill(_sha(data)) as fill:
        fill.write(data)
        raise RuntimeError("the download broke")
    assert list(tmp_path.iterdir()) == []
    assert cache.open(_sha(data), what="test") is None
    assert cache.open("not a sha", what="test") is None


def test_two_fills_of_one_entry_both_land_on_the_same_bytes(tmp_path: Path) -> None:
    """Two processes downloading the same file each write their own file and rename
    it in: whichever lands last, the entry is whole."""
    cache = DownloadCache(tmp_path, max_bytes=1 << 20)
    data = os.urandom(4096)
    with cache.fill(_sha(data)) as first, cache.fill(_sha(data)) as second:
        first.write(data[:100])
        second.write(data)
        first.write(data[100:])
        assert second.commit()
        assert first.commit()
    assert [p.name for p in tmp_path.iterdir()] == [_sha(data)]
    assert (tmp_path / _sha(data)).read_bytes() == data


def test_the_least_recently_used_go_past_the_byte_cap(tmp_path: Path) -> None:
    cache = DownloadCache(tmp_path, max_bytes=1 << 20, min_age=0)
    blobs = [bytes([n]) * 100 for n in range(3)]
    for age, data in zip((30, 20, 10), blobs, strict=True):
        cache.put(_sha(data), data)
        _aged(tmp_path / _sha(data), age)
    cache.max_bytes = 150
    # Reading the oldest makes it the most recently used.
    handle = cache.open(_sha(blobs[0]), what="test")
    assert handle is not None
    handle.close()
    cache.put(_sha(b"new" * 10), b"new" * 10)
    kept = {p.name for p in tmp_path.iterdir()}
    assert kept == {_sha(blobs[0]), _sha(b"new" * 10)}


def test_an_entry_used_within_min_age_is_never_evicted(tmp_path: Path) -> None:
    """A read may be under way: past the cap the directory runs over instead."""
    cache = DownloadCache(tmp_path, max_bytes=1 << 20, min_age=600)
    for n in range(3):
        cache.put(_sha(bytes([n]) * 100), bytes([n]) * 100)
    cache.max_bytes = 150
    cache.prune()
    assert len(list(tmp_path.iterdir())) == 3
    for path in tmp_path.iterdir():
        _aged(path, 601)
    cache.prune()
    assert len(list(tmp_path.iterdir())) == 1


def test_a_dead_processs_partial_fill_is_removed_and_a_live_ones_kept(tmp_path: Path) -> None:
    dead = tmp_path / ".abc.dead"
    live = tmp_path / ".abc.live"
    dead.write_bytes(b"x")
    live.write_bytes(b"x")
    _aged(dead, PARTIAL_MAX_AGE + 1)
    prune(tmp_path, max_bytes=0)
    assert [p.name for p in tmp_path.iterdir()] == [".abc.live"]


def test_a_lookup_is_a_span_saying_hit_or_miss(tmp_path: Path, spans: InMemorySpanExporter) -> None:
    cache = DownloadCache(tmp_path, max_bytes=1 << 20)
    cache.put(_sha(b"x"), b"x")
    handle = cache.open(_sha(b"x"), what="blob")
    assert handle is not None
    handle.close()
    assert cache.open(_sha(b"y"), what="library_file") is None
    found = [
        (s.attributes or {}).get("scadbuddy.cache.hit")
        for s in spans.get_finished_spans()
        if s.name == "download_cache.lookup"
    ]
    assert found == [True, False]


def test_a_verified_copy_is_the_callers_own_and_a_bad_entry_a_miss(tmp_path: Path) -> None:
    cache = DownloadCache(tmp_path / "shared", max_bytes=1 << 20)
    data = b"the file"
    cache.put(_sha(data), data)
    with (tmp_path / "mine").open("w+b") as out:
        assert cache.copy_verified(_sha(data), out, what="test")
        # Whatever happens to the shared entry now, the copy is what was checked.
        cache.path(_sha(data)).write_bytes(b"changed after the check")
        assert out.read() == data
    with (tmp_path / "again").open("w+b") as out:
        assert not cache.copy_verified(_sha(data), out, what="test")
        assert out.read() == b""
    assert not cache.has(_sha(data))


def test_a_fill_past_the_entry_limit_stops_and_keeps_nothing(tmp_path: Path) -> None:
    cache = DownloadCache(tmp_path, max_bytes=400)
    data = b"x" * 101
    with pytest.raises(TooLargeError), cache.fill(_sha(data)) as fill:
        fill.write(data)
    assert list(tmp_path.iterdir()) == []
    cache.put(_sha(data), data)
    assert not cache.has(_sha(data))


def test_a_kept_file_is_a_copy_and_the_callers_file_is_left_at_its_start(
    tmp_path: Path,
) -> None:
    cache = DownloadCache(tmp_path / "shared", max_bytes=1 << 20)
    data = b"the file"
    with (tmp_path / "mine").open("w+b") as mine:
        mine.write(data)
        cache.keep_file(_sha(data), mine)
        assert mine.tell() == 0
        cache.path(_sha(data)).write_bytes(b"changed in the directory")
        assert mine.read() == data
    with (tmp_path / "big").open("w+b") as big:
        big.write(b"y" * (1 << 19))
        cache.keep_file(_sha(b"y" * (1 << 19)), big)
        assert big.tell() == 0
    assert not cache.has(_sha(b"y" * (1 << 19)))


# --- through the content store: what one process put or got, another reads ----------


class _CountingBackend(LocalContentBackend):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.downloads = 0

    async def download(self, backend_id: str) -> AsyncGenerator[bytes]:
        self.downloads += 1
        async for chunk in super().download(backend_id):
            yield chunk


def _process(remote: Path, shared: Path, pool: Pool) -> tuple[ContentStore, _CountingBackend]:
    """A process's store: the same remote and index, and the same shared directory."""
    backend = _CountingBackend(remote)
    return ContentStore(backend, BlobIndex(pool), downloads=DownloadCache(shared, 1 << 30)), backend


async def _settled(store: ContentStore) -> None:
    """Wait for a put's copy into the cache, which runs after the put answered."""
    await asyncio.gather(*store._filling)


@pytest.mark.requires_postgres
async def test_a_blob_one_process_put_is_read_by_another_without_a_download(
    tmp_path: Path, pool: Pool
) -> None:
    """The worker publishes an arranged output; the API saving it reads it from the
    shared directory rather than downloading it from Bambuddy (#2788)."""
    worker, _ = _process(tmp_path / "remote", tmp_path / "shared", pool)
    api, api_backend = _process(tmp_path / "remote", tmp_path / "shared", pool)
    data = os.urandom(3 << 20)
    ref = await worker.put("piece", data, name="p", scope=SCOPE)
    await _settled(worker)
    assert await api.read(ref) == data
    assert api_backend.downloads == 0


@pytest.mark.requires_postgres
async def test_a_blob_downloaded_once_is_read_from_the_cache_after(
    tmp_path: Path, pool: Pool
) -> None:
    plain = ContentStore(LocalContentBackend(tmp_path / "remote"), BlobIndex(pool))
    data = os.urandom(3 << 20)
    ref = await plain.put("piece", data, name="p", scope=SCOPE)
    first, first_backend = _process(tmp_path / "remote", tmp_path / "shared", pool)
    second, second_backend = _process(tmp_path / "remote", tmp_path / "shared", pool)
    assert await first.read(ref) == data
    assert await second.read(ref) == data
    assert (first_backend.downloads, second_backend.downloads) == (1, 0)


@pytest.mark.requires_postgres
async def test_a_cached_copy_that_is_not_its_sha_is_refused_and_dropped(
    tmp_path: Path, pool: Pool
) -> None:
    store, backend = _process(tmp_path / "remote", tmp_path / "shared", pool)
    data = os.urandom(1 << 20)
    ref = await store.put("piece", data, name="p", scope=SCOPE)
    await _settled(store)
    (tmp_path / "shared" / ref.sha256).write_bytes(b"rot")
    with pytest.raises(BlobCorruptError):
        await store.read(ref)
    # Gone, so the retry downloads it.
    assert await store.read(ref) == data
    assert backend.downloads == 1


@pytest.mark.requires_postgres
async def test_a_read_stopped_early_keeps_nothing(tmp_path: Path, pool: Pool) -> None:
    plain = ContentStore(LocalContentBackend(tmp_path / "remote"), BlobIndex(pool))
    ref = await plain.put("piece", os.urandom(3 << 20), name="p", scope=SCOPE)
    store, _ = _process(tmp_path / "remote", tmp_path / "shared", pool)
    chunks = store.get(ref)
    await anext(chunks)
    await chunks.aclose()
    assert list((tmp_path / "shared").iterdir()) == []


@pytest.mark.requires_postgres
async def test_a_blob_larger_than_the_entry_limit_is_read_and_not_kept(
    tmp_path: Path, pool: Pool
) -> None:
    plain = ContentStore(LocalContentBackend(tmp_path / "remote"), BlobIndex(pool))
    data = os.urandom(3 << 20)
    ref = await plain.put("piece", data, name="p", scope=SCOPE)
    store = ContentStore(
        LocalContentBackend(tmp_path / "remote"),
        BlobIndex(pool),
        downloads=DownloadCache(tmp_path / "shared", max_bytes=4 << 20),
    )
    assert await store.read(ref) == data
    assert list((tmp_path / "shared").iterdir()) == []
