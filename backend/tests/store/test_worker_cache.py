"""Two workers, no shared volume: a piece rendered on A is consumed on B (epic #426)."""

from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path
from typing import cast

import pytest

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import JobResult, PartInfo
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.archive import MARKER, pack_dir, unpack_dir
from scadbuddy.store.cache import CachedBlobStore, StaleBlobError
from scadbuddy.store.content import BlobScope, ContentStore
from scadbuddy.store.index import Pool
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.workflows.activities import PIECE_NAME, RenderActivities, WorkerDeps, _write_piece
from scadbuddy.workflows.models import PieceRequest, PieceResult, piece_key
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


async def test_eviction_keeps_unpublished_and_recent_pieces(
    tmp_path: Path, content: ContentStore
) -> None:
    a = worker(tmp_path / "a", content, max_bytes=0, min_age=60.0)
    for key in ("old", "recent"):
        (a.dir_for(key) / "m").write_bytes(b"x" * 10)
        await a.publish(key, scope=SCOPE)
    (a.dir_for("rendering") / "m").write_bytes(b"x" * 10)  # never published
    past = time.time() - 3600
    os.utime(a.local.root / "old", (past, past))
    os.utime(a.local.root / "rendering", (past, past))
    assert a.evict() == ["old"]
    assert a.local.exists("recent") and a.local.exists("rendering")


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
