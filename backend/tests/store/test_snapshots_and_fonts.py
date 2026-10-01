from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from trimesh.creation import box

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths, model_path
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.fonts import FontService
from scadbuddy.library.history import ModelHistory
from scadbuddy.render import previews as previews_module
from scadbuddy.render.jobs import prune_revision_exports, resolve_source
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.schema import CustomizerSchema
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.store import snapshots as snapshots_module
from scadbuddy.store.content import ContentStore
from scadbuddy.store.fonts import FontMirror, font_key, model_dir, wanted_families
from scadbuddy.store.index import Pool
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.store.snapshots import SnapshotStore, SnapshotUnavailableError, snapshot_key
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.models import PieceRequest, piece_key
from tests.conftest import write_openscad_3mf
from tests.support.openscad import install_fake_openscad
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
        # `prepare` derives the schema (phase 4): the fake openscad answers for it.
        config=install_fake_openscad(tmp_path, worker_paths),
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


def _worker_deps(tmp_path: Path, pool: Pool, snapshots: SnapshotStore) -> WorkerDeps:
    return WorkerDeps(
        config=Config(data_dir=tmp_path / "worker"),
        paths=snapshots.paths,
        assets=AssetStore(tmp_path / "worker" / "assets"),
        blobs=LocalBlobStore(tmp_path / "worker" / "blobs"),
        refs=BlobRefs(pool),
        projection=cast(JobProjection, object()),
        history=None,
        snapshots=snapshots,
    )


async def _stored(tmp_path: Path, content: ContentStore, rev: str) -> SnapshotStore:
    api_paths = DataPaths(tmp_path / "api")
    export = api_paths.model_revision_dir("demo", rev)
    export.mkdir(parents=True)
    (export / "model.scad").write_text("cube(4);")
    api = SnapshotStore(content, api_paths, history=None)
    await api.ensure("demo", rev)
    return api


async def test_a_snapshot_lost_from_the_store_fails_prepare_clearly_and_pin_stores_it_again(
    tmp_path: Path, content: ContentStore, pool: Pool
) -> None:
    rev = "d" * 40
    api = await _stored(tmp_path, content, rev)
    stat = content.index.get(snapshot_key("demo", rev))
    assert stat is not None
    (tmp_path / "remote" / stat.ref.backend_id).unlink()  # deleted in Bambuddy's Work/
    worker = SnapshotStore(content, DataPaths(tmp_path / "worker"), history=None)
    req = PieceRequest(
        slug="demo", revision=rev, params={}, piece_key=piece_key("demo", rev, "model.scad", {})
    )
    with pytest.raises(ApplicationError) as raised:
        await RenderActivities(_worker_deps(tmp_path, pool, worker)).prepare(req)
    assert raised.value.non_retryable and raised.value.type == "SnapshotUnavailableError"
    assert raised.value.message == (
        f"the template's source at {rev} is no longer in the store; render again"
    )
    assert content.index.get(snapshot_key("demo", rev)) is None
    assert await api.pin("demo", rev) == rev  # the next submit stores it again
    assert content.index.get(snapshot_key("demo", rev)) is not None


async def test_concurrent_materializes_on_a_fresh_worker_download_once(
    tmp_path: Path, content: ContentStore
) -> None:
    rev = "e" * 40
    await _stored(tmp_path, content, rev)
    download = content.backend.download
    calls = 0

    def counting(backend_id: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return download(backend_id)

    content.backend.download = counting  # type: ignore[method-assign]
    worker = SnapshotStore(content, DataPaths(tmp_path / "worker"), history=None)
    results = await asyncio.gather(*(worker.materialize("demo", rev) for _ in range(6)))
    assert results == [True] * 6
    assert calls == 1


async def test_a_worker_syncs_only_the_families_its_template_names(
    tmp_path: Path, content: ContentStore
) -> None:
    api_fonts = FontService(tmp_path / "api")
    for family in ("Lobster Two", "Pacifico"):
        directory = api_fonts.family_dir(family)
        directory.mkdir(parents=True)
        (directory / "Regular.ttf").write_bytes(family.encode())
        await FontMirror(content, api_fonts).publish(family)
    source = tmp_path / "src"
    source.mkdir()
    (source / "model.scad").write_text(
        'font = "Lobster Two:style=Bold"; // font\ntext("hi", font=font);'
    )
    wanted = wanted_families(source, {"size": 3})
    assert "lobstertwo" in wanted and "pacifico" not in wanted
    assert "pacifico" in wanted_families(source, {"font": "Pacifico"})
    mirror = FontMirror(content, FontService(tmp_path / "worker"))
    assert await mirror.sync(wanted) == ["lobstertwo"]
    assert await mirror.sync(wanted) == []


def test_a_piece_in_a_subdirectory_scans_the_templates_root_files(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "parts").mkdir(parents=True)
    (source / "model.scad").write_text('label_font = "Lobster Two"; // font\n')
    (source / "parts" / "roof.scad").write_text("include <../model.scad>\ncube(1);\n")
    root = model_dir(source / "parts" / "roof.scad", "parts/roof.scad")
    assert root == source
    assert "lobstertwo" in wanted_families(root, {})
    assert model_dir(source / "model.scad", "model.scad") == source


async def test_a_worker_without_the_volume_renders_a_preview_from_the_stored_snapshot(
    tmp_path: Path, content: ContentStore, pool: Pool
) -> None:
    """Final review C1: nothing is read from the worker's `models/`, which is empty;
    the source comes from the snapshot, the font it names from the store."""
    rev = "f" * 40
    api_paths = DataPaths(tmp_path / "api")
    export = api_paths.model_revision_dir("demo", rev)
    export.mkdir(parents=True)
    (export / "model.scad").write_text('text("hi", font = "Lobster Two");')
    await SnapshotStore(content, api_paths, history=None).ensure("demo", rev)
    api_fonts = FontService(tmp_path / "api")
    family = api_fonts.family_dir("Lobster Two")
    family.mkdir(parents=True)
    (family / "LobsterTwo-Regular.ttf").write_bytes(b"ttf")
    await FontMirror(content, api_fonts).publish("Lobster Two")

    worker_paths = DataPaths(tmp_path / "worker")
    worker_fonts = FontService(tmp_path / "worker")
    deps = replace(
        _worker_deps(tmp_path, pool, SnapshotStore(content, worker_paths, history=None)),
        fonts_mirror=FontMirror(content, worker_fonts),
    )
    rendered: list[Path] = []

    async def one_box(*args: object, **kwargs: object) -> object:
        rendered.append(cast(Path, args[0]))
        write_openscad_3mf(cast(Path, args[3]), [("Color 1", "#0047BB00", box())])
        return mock.Mock(log_tail=[], missing_files=())

    async def no_parameters(*args: object, **kwargs: object) -> CustomizerSchema:
        return CustomizerSchema()

    with (
        mock.patch.object(previews_module, "render_3mf", one_box),
        mock.patch.object(previews_module, "cached_schema", no_parameters),
    ):
        png = await ActivityEnvironment().run(
            RenderActivities(deps).render_preview_png, "demo", rev
        )

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert rendered == [worker_paths.model_revision_dir("demo", rev) / "model.scad"]
    assert not worker_paths.model_source("demo").exists()
    assert (worker_fonts.root / family.name / "LobsterTwo-Regular.ttf").read_bytes() == b"ttf"


async def test_a_preview_whose_snapshot_is_gone_fails_clearly_on_a_worker(
    tmp_path: Path, content: ContentStore, pool: Pool
) -> None:
    worker = SnapshotStore(content, DataPaths(tmp_path / "worker"), history=None)
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(
            RenderActivities(_worker_deps(tmp_path, pool, worker)).render_preview_png,
            "demo",
            "0" * 40,
        )
    assert raised.value.non_retryable and raised.value.type == "SnapshotUnavailableError"


async def test_an_export_a_worker_uses_again_is_not_pruned_within_the_ttl(
    tmp_path: Path, content: ContentStore
) -> None:
    """Final re-review n1: a `materialize` hit is a use, so the prune's TTL runs from
    the last render that read the export, not from when it was unpacked."""
    rev = "9" * 40
    await _stored(tmp_path, content, rev)
    worker_paths = DataPaths(tmp_path / "worker")
    worker = SnapshotStore(content, worker_paths, history=None)
    assert await worker.materialize("demo", rev)
    export = worker_paths.model_revision_dir("demo", rev)
    old = time.time() - 7 * 86400
    os.utime(export, (old, old))
    assert await worker.materialize("demo", rev)  # a hit
    assert prune_revision_exports(worker_paths, 86400) == []
    assert (export / "model.scad").is_file()


def _pruned_on_touch(monkeypatch: pytest.MonkeyPatch, times: int = 1) -> None:
    """The prune takes the export between `materialize`'s `is_dir` and its touch."""
    left = [times]

    def touch(directory: Path) -> None:
        if left[0] > 0:
            left[0] -= 1
            shutil.rmtree(directory)

    monkeypatch.setattr(snapshots_module, "touch_export", touch)


async def test_an_export_pruned_as_it_is_used_is_not_reported_present(
    tmp_path: Path, content: ContentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#674 gate: a hit the prune took is no hit, so `_materialize` fails clearly."""
    worker_paths = DataPaths(tmp_path / "worker")
    export = worker_paths.model_revision_dir("demo", "7" * 40)
    export.mkdir(parents=True)
    (export / "model.scad").write_text("cube(7);")
    _pruned_on_touch(monkeypatch)
    worker = SnapshotStore(content, worker_paths, history=None)
    assert await worker.materialize("demo", "7" * 40) is False


async def test_an_export_pruned_as_it_is_used_comes_back_from_the_store(
    tmp_path: Path, content: ContentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rev = "6" * 40
    await _stored(tmp_path, content, rev)
    worker_paths = DataPaths(tmp_path / "worker")
    worker = SnapshotStore(content, worker_paths, history=None)
    assert await worker.materialize("demo", rev)
    _pruned_on_touch(monkeypatch)
    assert await worker.materialize("demo", rev)
    assert (worker_paths.model_revision_dir("demo", rev) / "model.scad").read_text() == "cube(4);"


async def test_an_export_lost_to_the_prune_on_a_worker_without_git_fails_clearly(
    tmp_path: Path,
) -> None:
    with pytest.raises(SnapshotUnavailableError, match="demo@"):
        await resolve_source("demo", "8" * 40, paths=DataPaths(tmp_path / "worker"), history=None)
