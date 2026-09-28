from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths, model_path
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import ModelHistory
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.store.content import ContentStore
from scadbuddy.store.fonts import FontMirror, font_key
from scadbuddy.store.index import Pool
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.store.snapshots import SnapshotStore, snapshot_key
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.models import PieceRequest, piece_key
from tests.support.store import local_content

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def content(tmp_path: Path, pool: Pool) -> ContentStore:
    return local_content(tmp_path / "remote", pool)


async def test_a_worker_without_the_volume_gets_the_template_at_its_revision(
    tmp_path: Path, content: ContentStore
) -> None:
    api_paths = DataPaths(tmp_path / "api")
    export = api_paths.model_revision_dir("demo", "a" * 40)
    export.mkdir(parents=True)
    (export / "model.scad").write_text("cube(1);")
    (export / "model.json").write_text('{"name": "Demo box"}')
    api = SnapshotStore(content, api_paths, history=None)
    assert await api.ensure("demo", "a" * 40) == snapshot_key("demo", "a" * 40)
    stat = content.index.get(snapshot_key("demo", "a" * 40))
    assert stat is not None and stat.slug == "demo"
    worker_paths = DataPaths(tmp_path / "worker")
    worker = SnapshotStore(content, worker_paths, history=None)
    assert await worker.materialize("demo", "a" * 40)
    got = worker_paths.model_revision_dir("demo", "a" * 40)
    assert (got / "model.scad").read_text() == "cube(1);"
    assert await worker.materialize("demo", "b" * 40) is False


@pytest.mark.requires_git
async def test_an_unpinned_request_renders_the_last_commit(
    tmp_path: Path, content: ContentStore
) -> None:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    # As the app builds it: without the wrapper prefix the ignore file drops every .scad.
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX)
    history.ensure_repo()
    source = paths.model_source("demo")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("cube(2);")
    history.commit("add demo", model_path("demo"))
    head = subprocess.run(
        ["git", "-C", str(paths.models), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    snapshots = SnapshotStore(content, paths, history)
    assert await snapshots.pin("demo", None) == head
    assert content.index.get(snapshot_key("demo", head)) is not None


async def test_prepare_on_a_worker_without_history_uses_the_materialized_snapshot(
    tmp_path: Path, content: ContentStore, pool: Pool
) -> None:
    rev = "c" * 40
    api_paths = DataPaths(tmp_path / "api")
    export = api_paths.model_revision_dir("demo", rev)
    export.mkdir(parents=True)
    (export / "model.scad").write_text("cube(3);")
    (export / "model.json").write_text('{"name": "Demo"}')
    await SnapshotStore(content, api_paths, history=None).ensure("demo", rev)
    worker_paths = DataPaths(tmp_path / "worker")
    deps = WorkerDeps(
        config=Config(data_dir=tmp_path / "worker"),
        paths=worker_paths,
        assets=AssetStore(tmp_path / "worker" / "assets"),
        blobs=LocalBlobStore(tmp_path / "worker" / "blobs"),
        refs=BlobRefs(pool),
        projection=cast(JobProjection, object()),
        history=None,
        snapshots=SnapshotStore(content, worker_paths, history=None),
    )
    # A real key: PieceRequest checks piece_key against its other fields.
    key = piece_key("demo", rev, "model.scad", {})
    req = PieceRequest(slug="demo", revision=rev, params={}, piece_key=key)
    prepared = await RenderActivities(deps).prepare(req)
    assert Path(prepared.scad) == worker_paths.model_revision_dir("demo", rev) / "model.scad"
    assert prepared.version == rev


async def test_downloaded_fonts_reach_a_worker_that_never_installed_them(
    tmp_path: Path, content: ContentStore
) -> None:
    api_fonts = FontService(tmp_path / "api")
    family = api_fonts.family_dir("Lobster Two")
    family.mkdir(parents=True)
    (family / "LobsterTwo-Regular.ttf").write_bytes(b"ttf")
    await FontMirror(content, api_fonts).publish("Lobster Two")
    worker_fonts = FontService(tmp_path / "worker")
    mirror = FontMirror(content, worker_fonts)
    assert await mirror.sync() == [family.name]
    assert (worker_fonts.root / family.name / "LobsterTwo-Regular.ttf").read_bytes() == b"ttf"
    assert await mirror.sync() == []
    assert content.index.get(font_key(family.name)) is not None
