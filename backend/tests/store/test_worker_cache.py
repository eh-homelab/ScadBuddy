"""Two workers, no shared volume: a piece rendered on A is consumed on B (epic #426)."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import JobResult, PartInfo
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.archive import MARKER, pack_dir, unpack_dir
from scadbuddy.store.cache import CachedBlobStore, StaleBlobError
from scadbuddy.store.content import BlobRef, BlobScope, ContentStore
from scadbuddy.store.index import Pool
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.workflows.activities import PIECE_NAME, RenderActivities, WorkerDeps, _write_piece
from scadbuddy.workflows.models import PieceRequest, PieceResult, piece_key
from tests.support.activities import demo_paths, piece_request, worker_deps
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres
SCOPE = BlobScope(slug="demo", title="Demo")


def worker(root: Path, content: ContentStore, **kw: float) -> CachedBlobStore:
    return CachedBlobStore(
        LocalBlobStore(root / "blobs"),
        content,
        max_bytes=int(kw.get("max_bytes", 1 << 30)),
        min_age=kw.get("min_age", 0.0),
    )


@pytest.fixture
def content(tmp_path: Path, pool: Pool) -> ContentStore:
    return local_content(tmp_path / "remote", pool)


async def test_a_published_piece_is_fetched_once_on_another_worker(
    tmp_path: Path, content: ContentStore
) -> None:
    a, b = worker(tmp_path / "a", content), worker(tmp_path / "b", content)
    (a.dir_for("k") / "model.3mf").write_bytes(b"3mf")
    await a.publish("k", scope=SCOPE)
    assert await b.fetch("k")
    assert (b.dir_for("k") / "model.3mf").read_bytes() == b"3mf"
    downloads = content.backend.download
    calls = 0

    def counting(backend_id: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return downloads(backend_id)

    content.backend.download = counting  # type: ignore[method-assign]
    assert await b.fetch("k")
    assert calls == 0  # the second fetch is a cache hit


async def test_a_later_stage_published_elsewhere_replaces_the_cached_copy(
    tmp_path: Path, content: ContentStore
) -> None:
    a, b = worker(tmp_path / "a", content), worker(tmp_path / "b", content)
    (a.dir_for("k") / "model.3mf").write_bytes(b"main")
    await a.publish("k", scope=SCOPE)
    assert await b.fetch("k")
    (b.dir_for("k") / "plate-1.3mf").write_bytes(b"solids")
    await b.publish("k", scope=SCOPE)
    assert await a.fetch("k")
    assert (a.dir_for("k") / "plate-1.3mf").read_bytes() == b"solids"


async def test_a_stale_publisher_cannot_overwrite_a_newer_piece(
    tmp_path: Path, content: ContentStore
) -> None:
    a, b = worker(tmp_path / "a", content), worker(tmp_path / "b", content)
    (a.dir_for("k") / "model.3mf").write_bytes(b"v1")
    await a.publish("k", scope=SCOPE)
    await b.fetch("k")
    (a.dir_for("k") / "model.3mf").write_bytes(b"v2 from the retry")
    await a.publish("k", scope=SCOPE)
    (b.dir_for("k") / "model.3mf").write_bytes(b"v2 from the zombie")
    with pytest.raises(StaleBlobError):
        await b.publish("k", scope=SCOPE)
    c = worker(tmp_path / "c", content)
    assert await c.fetch("k")
    assert (c.dir_for("k") / "model.3mf").read_bytes() == b"v2 from the retry"


async def test_a_piece_deleted_or_altered_in_the_backend_is_rendered_again(
    tmp_path: Path, content: ContentStore
) -> None:
    a = worker(tmp_path / "a", content)
    for key, damage in (("gone", "delete"), ("altered", "rewrite")):
        (a.dir_for(key) / "model.3mf").write_bytes(key.encode())
        await a.publish(key, scope=SCOPE)
        stat = content.index.get(key)
        assert stat is not None
        path = tmp_path / "remote" / stat.ref.backend_id
        path.unlink() if damage == "delete" else path.write_bytes(b"someone else's")
        b = worker(tmp_path / f"b-{key}", content)
        assert await b.fetch(key) is False
        assert content.index.get(key) is None  # forgotten: the next render stores it again


def test_an_archive_entry_outside_its_directory_is_refused(tmp_path: Path) -> None:
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../escape.txt", "x")
    with pytest.raises(ValueError, match="outside"):
        unpack_dir(evil.getvalue(), tmp_path / "piece")
    assert not (tmp_path / "escape.txt").exists()


def test_packing_is_deterministic_and_leaves_out_dotfiles(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.txt").write_text("a")
    (d / MARKER).write_text("x")
    (d / ".piece.json.123").write_text("staging")
    first = pack_dir(d)
    os.utime(d / "a.txt", (1, 1))
    assert pack_dir(d) == first
    assert zipfile.ZipFile(io.BytesIO(first)).namelist() == ["a.txt"]


async def test_eviction_keeps_recent_pieces_and_reclaims_abandoned_ones(
    tmp_path: Path, content: ContentStore
) -> None:
    a = worker(tmp_path / "a", content, max_bytes=0, min_age=60.0)
    for key in ("old", "recent"):
        (a.dir_for(key) / "m").write_bytes(b"x" * 10)
        await a.publish(key, scope=SCOPE)
    (a.dir_for("rendering") / "m").write_bytes(b"x" * 10)  # never published, in flight
    (a.dir_for("crashed") / "m").write_bytes(b"x" * 10)  # never published, abandoned
    staging = a.local.root / ".staging-1"
    staging.mkdir()
    (staging / "m").write_bytes(b"x" * 10)  # an `unpack_dir` a crash left
    past = time.time() - 3600
    for name in ("old", "crashed", ".staging-1"):
        os.utime(a.local.root / name, (past, past))
    assert sorted(a.evict()) == [".staging-1", "crashed", "old"]
    assert a.local.exists("recent") and a.local.exists("rendering")


async def test_a_publish_or_a_download_over_the_cap_trims_the_cache(
    tmp_path: Path, content: ContentStore
) -> None:
    """#689: the cap holds between the periodic passes, keeping what is in flight."""
    a = worker(tmp_path / "a", content, max_bytes=25, min_age=60.0)
    b = worker(tmp_path / "b", content, max_bytes=1 << 30, min_age=60.0)
    (b.dir_for("k") / "m").write_bytes(b"x" * 10)
    await b.publish("k", scope=SCOPE)
    past = time.time() - 3600

    def abandoned(key: str) -> None:
        (a.dir_for(key) / "m").write_bytes(b"x" * 10)
        os.utime(a.local.root / key, (past, past))

    abandoned("old-1")
    (a.dir_for("rendering") / "m").write_bytes(b"x" * 10)  # in flight, never published
    (a.dir_for("new") / "m").write_bytes(b"x" * 10)
    await a.publish("new", scope=SCOPE)
    assert sorted(a.local.keys()) == ["new", "rendering"]

    abandoned("old-2")
    assert await a.fetch("k")  # a miss, downloaded
    assert sorted(a.local.keys()) == ["k", "new", "rendering"]


async def test_only_one_eviction_pass_runs_at_a_time(
    tmp_path: Path, content: ContentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write's trim and the periodic pass never scan and remove side by side."""
    from scadbuddy.store import cache as cache_module

    a = worker(tmp_path / "a", content, max_bytes=0, min_age=60.0)
    (a.dir_for("old") / "m").write_bytes(b"x" * 10)
    past = time.time() - 3600
    os.utime(a.local.root / "old", (past, past))
    size = cache_module._size
    scanning, release = threading.Event(), threading.Event()

    def slow_size(directory: Path) -> int:
        if not scanning.is_set():  # the first pass holds here; any later one does not
            scanning.set()
            release.wait(5)
        return size(directory)

    monkeypatch.setattr(cache_module, "_size", slow_size)
    first = asyncio.create_task(asyncio.to_thread(a.evict))
    assert await asyncio.to_thread(scanning.wait, 5)
    assert a.evict() == []  # the second pass leaves it to the first
    release.set()
    assert await first == ["old"]


async def test_eviction_skips_a_directory_touched_after_the_scan(
    tmp_path: Path, content: ContentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scadbuddy.store import cache as cache_module

    a = worker(tmp_path / "a", content, max_bytes=0, min_age=60.0)
    (a.dir_for("claimed") / "m").write_bytes(b"x" * 10)
    past = time.time() - 3600
    os.utime(a.local.root / "claimed", (past, past))
    size = cache_module._size

    def size_then_claim(directory: Path) -> int:
        counted = size(directory)
        a.dir_for(directory.name)  # a claim lands between the scan and the removal
        return counted

    monkeypatch.setattr(cache_module, "_size", size_then_claim)
    assert a.evict() == []
    assert a.local.exists("claimed")


async def test_an_eviction_cut_short_leaves_no_hit_behind(
    tmp_path: Path, content: ContentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1271: a worker killed mid-removal leaves a directory whose marker no longer
    claims the published bytes, so the next fetch downloads it again."""
    a = worker(tmp_path / "a", content, max_bytes=0, min_age=60.0)
    (a.dir_for("old") / "m").write_bytes(b"x" * 10)
    await a.publish("old", scope=SCOPE)
    past = time.time() - 3600
    os.utime(a.local.root / "old", (past, past))

    def killed_part_way(path: Path, *args: object, **kwargs: object) -> None:
        (path / "m").unlink()  # the content goes first; the process dies before the rest
        raise SystemExit

    monkeypatch.setattr(shutil, "rmtree", killed_part_way)
    with pytest.raises(SystemExit):
        a.evict()
    monkeypatch.undo()
    assert not (a.local.root / "old" / MARKER).exists()
    assert await a.fetch("old")
    assert (a.local.root / "old" / "m").read_bytes() == b"x" * 10


async def test_render_main_on_a_worker_without_the_piece_publishes_over_the_index(
    tmp_path: Path, content: ContentStore
) -> None:
    a, b = worker(tmp_path / "a", content), worker(tmp_path / "b", content)
    a_baseline = await a.indexed_sha("k")
    (a.dir_for("k") / "model.3mf").write_bytes(b"from A")
    await a.publish_fresh("k", scope=SCOPE, expected=a_baseline)
    # B's cache has never seen k; its baseline comes from the index, not a marker.
    b_baseline = await b.indexed_sha("k")
    assert b_baseline is not None
    (b.dir_for("k") / "model.3mf").write_bytes(b"from B")
    await b.publish_fresh("k", scope=SCOPE, expected=b_baseline)
    c = worker(tmp_path / "c", content)
    assert await c.fetch("k")
    assert (c.dir_for("k") / "model.3mf").read_bytes() == b"from B"
    # A zombie render_main that read its baseline before B published is still refused.
    with pytest.raises(StaleBlobError):
        await a.publish_fresh("k", scope=SCOPE, expected=a_baseline)


async def test_local_store_fetch_and_publish_keep_phase_one_behaviour(tmp_path: Path) -> None:
    local = LocalBlobStore(tmp_path / "blobs")
    assert await local.fetch("k") is False
    local.dir_for("k")
    assert await local.fetch("k") is True
    await local.publish("k", scope=SCOPE)  # nothing to do on a shared volume


async def test_cached_piece_answers_on_a_worker_that_never_rendered_it(
    tmp_path: Path, content: ContentStore, pool: Pool
) -> None:
    a, b = worker(tmp_path / "a", content), worker(tmp_path / "b", content)
    # A real key: PieceRequest checks piece_key against its other fields.
    key = piece_key("demo", "a" * 40, "model.scad", {})
    result = JobResult(
        model_3mf=f"blobs/{key}/model.3mf",
        preview_glb=f"blobs/{key}/preview.glb",
        parts=[PartInfo(name="Color 1", colour="#FF0000", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )
    _write_piece(a.dir_for(key), PieceResult(result=result, log_tail=["ok"]))
    await a.publish(key, scope=SCOPE)
    deps = WorkerDeps(
        config=Config(data_dir=tmp_path / "b"),
        paths=DataPaths(tmp_path / "b"),
        assets=AssetStore(tmp_path / "b" / "assets"),
        blobs=b,
        refs=BlobRefs(pool),
        projection=cast(JobProjection, object()),
    )
    req = PieceRequest(slug="demo", revision="a" * 40, params={}, piece_key=key)
    piece = await RenderActivities(deps).cached_piece(req)
    assert piece is not None and piece.result == result
    assert (b.dir_for(key) / PIECE_NAME).is_file()


# ── fix round 1 ────────────────────────────────────────────────────────────────


def _stage_worker(tmp_path: Path, content: ContentStore) -> RenderActivities:
    """The activities over a cache of their own, as on a worker with no shared volume."""
    paths = demo_paths(tmp_path)
    deps = worker_deps(tmp_path, paths)
    blobs = CachedBlobStore(LocalBlobStore(paths.blobs), content, max_bytes=1 << 30, min_age=0)
    return RenderActivities(dataclasses.replace(deps, blobs=blobs))


async def test_the_stages_publish_so_a_third_worker_answers_the_piece(
    tmp_path: Path, content: ContentStore
) -> None:
    env = ActivityEnvironment()
    a, b, c = (_stage_worker(tmp_path / n, content) for n in "abc")
    req = piece_request()
    prepared = await env.run(a.prepare, req)
    main = await env.run(a.render_main, req, prepared)
    await env.run(b.render_solids, req, prepared, main)
    piece = await env.run(b.finish_piece, req, prepared, main)
    assert await env.run(c.cached_piece, req) == piece
    assert (c.deps.paths.root / piece.result.model_3mf).is_file()


async def test_a_finished_piece_whose_publish_failed_is_not_answered_by_its_worker(
    tmp_path: Path, content: ContentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = ActivityEnvironment()
    a, b = _stage_worker(tmp_path / "a", content), _stage_worker(tmp_path / "b", content)
    req = piece_request()
    prepared = await env.run(a.prepare, req)
    main = await env.run(a.render_main, req, prepared)
    await env.run(a.render_solids, req, prepared, main)

    async def store_down(*_: Any, **__: Any) -> None:
        raise RuntimeError("the store is full")

    with monkeypatch.context() as patched:
        patched.setattr(content, "replace", store_down)
        with pytest.raises(RuntimeError, match="full"):
            await env.run(b.finish_piece, req, prepared, main)
    # B wrote piece.json, but the store never got it: no worker may answer it.
    assert await env.run(b.cached_piece, req) is None


async def test_a_stage_whose_piece_is_gone_fails_by_name(
    tmp_path: Path, content: ContentStore
) -> None:
    env = ActivityEnvironment()
    a, b = _stage_worker(tmp_path / "a", content), _stage_worker(tmp_path / "b", content)
    req = piece_request()
    prepared = await env.run(a.prepare, req)
    main = await env.run(a.render_main, req, prepared)
    await content.forget(req.piece_key)  # deleted in the backend between two stages
    with pytest.raises(ApplicationError) as raised:
        await env.run(b.render_solids, req, prepared, main)
    assert raised.value.type == "PieceStateLost" and raised.value.non_retryable


async def test_two_misses_on_one_key_download_once(tmp_path: Path, content: ContentStore) -> None:
    a, b = worker(tmp_path / "a", content), worker(tmp_path / "b", content)
    (a.dir_for("k") / "model.3mf").write_bytes(b"3mf")
    await a.publish("k", scope=SCOPE)
    downloads = content.backend.download
    calls = 0

    def counting(backend_id: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return downloads(backend_id)

    content.backend.download = counting  # type: ignore[method-assign]
    assert list(await asyncio.gather(b.fetch("k"), b.fetch("k"))) == [True, True]
    assert calls == 1
    assert (b.dir_for("k") / "model.3mf").read_bytes() == b"3mf"


def test_unpacking_one_directory_from_two_threads_never_fails(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "model.3mf").write_bytes(b"x" * 4096)
    data = pack_dir(source)
    target = tmp_path / "blobs" / "k"
    errors: list[BaseException] = []

    def unpack() -> None:
        try:
            unpack_dir(data, target, sha256="s")
        except BaseException as error:
            errors.append(error)

    for _ in range(20):
        threads = [threading.Thread(target=unpack) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert errors == []
    assert (target / "model.3mf").read_bytes() == b"x" * 4096
    assert sorted(p.name for p in target.parent.iterdir()) == ["k"]  # nothing left aside


async def test_a_row_on_another_backend_is_not_this_caches_to_fetch(
    tmp_path: Path, content: ContentStore
) -> None:
    b = worker(tmp_path / "b", content)
    foreign = BlobRef(sha256="0" * 64, kind="piece", backend="bambuddy", backend_id="42", size=1)
    content.index.put("k", foreign, slug="demo", meta={})
    assert await b.fetch("k") is False
    assert await b.indexed_sha("k") is None
    assert b.exists("k") is False
