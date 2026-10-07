from __future__ import annotations

import asyncio
import math
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast
from unittest import mock

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from trimesh.creation import box

from scadbuddy.bambuddy.client import DEFAULT_UPLOAD_TIMEOUT
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
from scadbuddy.store.content import ContentStore, StoreFullError
from scadbuddy.store.fonts import FontMirror, font_key, model_dir, wanted_families
from scadbuddy.store.index import Pool
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.store.snapshots import (
    SnapshotPendingError,
    SnapshotStore,
    SnapshotUnavailableError,
    snapshot_key,
)
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


async def test_a_republished_family_reaches_a_worker_that_has_the_old_one(
    tmp_path: Path, content: ContentStore
) -> None:
    """#687: `install(force=True)` republishes a family under the same key; a worker
    holding the old files fetches the new ones, and the API's own install (no marker,
    the source of what was published) is never overwritten by a sync."""
    api_fonts = FontService(tmp_path / "api")
    family = api_fonts.family_dir("Lobster Two")
    family.mkdir(parents=True)
    (family / "LobsterTwo-Regular.ttf").write_bytes(b"v1")
    api = FontMirror(content, api_fonts)
    await api.publish("Lobster Two")
    worker_fonts = FontService(tmp_path / "worker")
    worker = FontMirror(content, worker_fonts)
    assert await worker.sync() == [family.name]
    (family / "LobsterTwo-Regular.ttf").write_bytes(b"v2")
    await api.publish("Lobster Two")
    assert await worker.sync() == [family.name]
    assert (worker_fonts.root / family.name / "LobsterTwo-Regular.ttf").read_bytes() == b"v2"
    assert await worker.sync() == []
    (family / "LobsterTwo-Regular.ttf").write_bytes(b"v3, not yet published")
    assert await api.sync() == []
    assert (family / "LobsterTwo-Regular.ttf").read_bytes() == b"v3, not yet published"


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


async def test_ensure_marks_an_old_export_used_before_packing_it(
    tmp_path: Path, content: ContentStore
) -> None:
    """#688: `ensure` packs an existing export, so it touches it first; otherwise the
    prune (its mtime past the TTL) could remove it mid-pack."""
    api_paths = DataPaths(tmp_path / "api")
    rev = "7" * 40
    export = api_paths.model_revision_dir("demo", rev)
    export.mkdir(parents=True)
    (export / "model.scad").write_text("cube(7);")
    old = time.time() - 7 * 86400
    os.utime(export, (old, old))
    await SnapshotStore(content, api_paths, history=None).ensure("demo", rev)
    assert prune_revision_exports(api_paths, 86400) == []


async def test_concurrent_ensures_of_a_new_revision_store_it_once(
    tmp_path: Path, content: ContentStore
) -> None:
    """#686: debounced submits of a revision's first render all `pin` at once; one
    packs and uploads, the others find its row."""
    api_paths = DataPaths(tmp_path / "api")
    rev = "6" * 40
    export = api_paths.model_revision_dir("demo", rev)
    export.mkdir(parents=True)
    (export / "model.scad").write_text("cube(6);")
    put = content.put
    calls = 0

    async def counting(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)  # an upload takes a while
        return await put(*args, **kwargs)  # type: ignore[arg-type]

    content.put = counting  # type: ignore[method-assign,assignment]
    api = SnapshotStore(content, api_paths, history=None)
    keys = await asyncio.gather(*(api.ensure("demo", rev) for _ in range(5)))
    assert keys == [snapshot_key("demo", rev)] * 5
    assert calls == 1


def _gated(content: ContentStore) -> asyncio.Event:
    """Hold every `put` until the returned event is set: a Bambuddy taking its time."""
    put = content.put
    gate = asyncio.Event()

    async def slow(*args: object, **kwargs: object) -> object:
        await gate.wait()
        return await put(*args, **kwargs)  # type: ignore[arg-type]

    content.put = slow  # type: ignore[method-assign,assignment]
    return gate


def _export(paths: DataPaths, rev: str) -> None:
    export = paths.model_revision_dir("demo", rev)
    export.mkdir(parents=True)
    (export / "model.scad").write_text("cube(7);")


async def test_a_slow_first_pin_stops_the_request_waiting_and_stores_behind_it(
    tmp_path: Path, content: ContentStore
) -> None:
    """#686: a revision's first pin exports, packs and uploads inside the submit. Past
    `pin_timeout` the request stops waiting (a 503 with Retry-After) instead of hanging
    for the upload's own 180 s, and the store it started finishes behind it, so the
    retry finds the snapshot stored."""
    api_paths = DataPaths(tmp_path / "api")
    rev = "7" * 40
    _export(api_paths, rev)
    gate = _gated(content)
    api = SnapshotStore(content, api_paths, history=None, pin_timeout=0.1)
    with pytest.raises(SnapshotPendingError) as raised:
        await asyncio.wait_for(api.pin("demo", rev), 5)
    assert raised.value.retry_after >= 1
    assert content.index.get(snapshot_key("demo", rev)) is None
    behind = set(api._storing)
    assert len(behind) == 1
    gate.set()
    await asyncio.wait_for(asyncio.gather(*behind), 5)
    assert not api._storing  # the done-callback let it go
    assert content.index.get(snapshot_key("demo", rev)) is not None
    assert await asyncio.wait_for(api.pin("demo", rev), 5) == rev


async def test_a_store_done_as_the_pin_times_out_reports_its_own_outcome(
    tmp_path: Path, content: ContentStore
) -> None:
    """#1419 review: `wait_for` can time out after the store finished. A store that
    succeeded is the revision, not a 503; one that failed is its own error (a full
    store's 507), not a retryable "pending"."""
    api_paths = DataPaths(tmp_path / "api")
    rev = "a1" * 20
    _export(api_paths, rev)
    real_wait_for = asyncio.wait_for

    async def late(awaitable: Any, timeout: float | None) -> Any:
        await real_wait_for(awaitable, 5)  # the store finishes...
        raise TimeoutError  # ...and the wait reports a timeout anyway

    api = SnapshotStore(content, api_paths, history=None, pin_timeout=0.1, wait=late)
    assert await api.pin("demo", rev) == rev

    async def full(*args: object, **kwargs: object) -> object:
        raise StoreFullError("past SCADBUDDY_STORE_MAX_TOTAL_BYTES (1)")

    content.put = full  # type: ignore[method-assign,assignment]
    _export(api_paths, "b2" * 20)
    with pytest.raises(StoreFullError):
        await api.pin("demo", "b2" * 20)


async def _gives_up(awaitable: Any, timeout: float | None) -> Any:
    """A pin's wait that runs out at once, as `wait_for` does: the shield is cancelled."""
    asyncio.ensure_future(awaitable).cancel()
    raise TimeoutError


async def test_pins_of_a_revision_still_storing_join_its_one_store(
    tmp_path: Path, content: ContentStore
) -> None:
    """#1435: each timed-out pin used to start one more store, queued on the key's lock
    behind the stalled one. A retry joins the store already running instead."""
    api_paths = DataPaths(tmp_path / "api")
    rev = "c3" * 20
    _export(api_paths, rev)
    gate = _gated(content)
    api = SnapshotStore(content, api_paths, history=None, wait=_gives_up)
    for _ in range(3):
        with pytest.raises(SnapshotPendingError):
            await api.pin("demo", rev)
    behind = set(api._storing)
    assert len(behind) == 1
    gate.set()
    await asyncio.wait_for(asyncio.gather(*behind), 5)
    assert content.index.get(snapshot_key("demo", rev)) is not None


async def test_retry_after_grows_with_the_time_a_store_has_taken(
    tmp_path: Path, content: ContentStore
) -> None:
    """#1436: an upload can take far longer than `pin_timeout`, so a fixed hint sends
    the client back for one 503 after another. The hint is as long as the store has
    run, at least `pin_timeout` and at most the upload's own timeout."""
    api_paths = DataPaths(tmp_path / "api")
    rev = "d4" * 20
    _export(api_paths, rev)
    gate = _gated(content)
    now = [1000.0]
    api = SnapshotStore(
        content, api_paths, history=None, pin_timeout=30, wait=_gives_up, clock=lambda: now[0]
    )
    hints = []
    for later in (0, 100, 10_000):
        now[0] = 1000.0 + later
        with pytest.raises(SnapshotPendingError) as raised:
            await api.pin("demo", rev)
        hints.append(raised.value.retry_after)
    assert hints == [30, 100, math.ceil(DEFAULT_UPLOAD_TIMEOUT)]
    gate.set()
    await asyncio.wait_for(asyncio.gather(*api._storing), 5)


async def test_stores_of_different_revisions_run_a_few_at_a_time(
    tmp_path: Path, content: ContentStore
) -> None:
    """#1436: a pin that stops waiting leaves its store running, so the preview pass
    could otherwise have every template uploading to Bambuddy at once."""
    api_paths = DataPaths(tmp_path / "api")
    revs = ["e5" * 20, "f6" * 20]
    for rev in revs:
        _export(api_paths, rev)
    put = content.put
    gate = asyncio.Event()
    entered: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]
    uploading = 0

    async def slow(*args: object, **kwargs: object) -> object:
        nonlocal uploading
        entered[uploading].set()
        uploading += 1
        await gate.wait()
        return await put(*args, **kwargs)  # type: ignore[arg-type]

    content.put = slow  # type: ignore[method-assign,assignment]
    api = SnapshotStore(content, api_paths, history=None, wait=_gives_up, max_stores=1)
    queued = asyncio.Event()
    acquire = api._stores.acquire

    async def acquiring() -> Literal[True]:
        if api._stores.locked():
            queued.set()  # this store waits its turn
        return await acquire()

    api._stores.acquire = acquiring  # type: ignore[method-assign]
    with pytest.raises(SnapshotPendingError):
        await api.pin("demo", revs[0])
    await asyncio.wait_for(entered[0].wait(), 5)
    with pytest.raises(SnapshotPendingError):
        await api.pin("demo", revs[1])
    second = asyncio.ensure_future(entered[1].wait())
    turn = asyncio.ensure_future(queued.wait())
    await asyncio.wait_for(asyncio.wait({second, turn}, return_when="FIRST_COMPLETED"), 5)
    assert queued.is_set()
    assert not entered[1].is_set()
    gate.set()
    await asyncio.wait_for(asyncio.gather(*api._storing), 5)
    second.cancel()
    assert all(content.index.get(snapshot_key("demo", rev)) is not None for rev in revs)


async def test_a_caller_gone_mid_pin_leaves_the_store_running(
    tmp_path: Path, content: ContentStore
) -> None:
    """#686: a client that disconnects cancels its request; the shielded store carries
    on, so the next submit finds the snapshot."""
    api_paths = DataPaths(tmp_path / "api")
    rev = "8" * 40
    _export(api_paths, rev)
    gate = _gated(content)
    api = SnapshotStore(content, api_paths, history=None, pin_timeout=30)
    request = asyncio.create_task(api.pin("demo", rev))
    await asyncio.sleep(0.1)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    behind = set(api._storing)
    assert len(behind) == 1
    gate.set()
    await asyncio.wait_for(asyncio.gather(*behind), 5)
    assert content.index.get(snapshot_key("demo", rev)) is not None


async def test_shutdown_cancels_a_store_still_running_past_its_grace(
    tmp_path: Path, content: ContentStore
) -> None:
    """#686 review: a store no request waits for is not left pending on a closed loop."""
    api_paths = DataPaths(tmp_path / "api")
    rev = "9" * 40
    _export(api_paths, rev)
    _gated(content)  # never opens
    api = SnapshotStore(content, api_paths, history=None, pin_timeout=0.05)
    with pytest.raises(SnapshotPendingError):
        await api.pin("demo", rev)
    behind = set(api._storing)
    await asyncio.wait_for(api.aclose(grace=0.05), 5)
    assert all(task.cancelled() for task in behind)
    assert not api._storing


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
