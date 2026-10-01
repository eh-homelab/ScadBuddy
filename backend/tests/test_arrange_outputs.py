"""Every output records its objects (spec 2026-09-27 §7), and keeps their Parts alive."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.outputs import OutputStore, hold_parts, release_parts
from scadbuddy.render.job_models import ManifestObject
from scadbuddy.store import sweep_blobs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.workflows.models import Layout, OutputRequest
from scadbuddy.workflows.outputs import manifest_of
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from tests.support.arrange import finished_job, saved_output
from tests.test_packing_and_outputs import _deps, _record, _render


async def test_a_packed_output_lists_each_object_once_with_its_count(tmp_path: Path) -> None:
    paths, meta, written = await saved_output(tmp_path, count=3)
    [obj] = written.manifest
    assert obj.count == 3
    assert obj.slug == "demo" and obj.file == "model.scad" and obj.revision is None
    assert obj.bom_piece == "wall"
    assert obj.footprint == (obj.bbox.size[0], obj.bbox.size[1])
    assert OutputStore(paths).manifest(meta.id) == written.manifest


async def test_an_output_of_one_piece_as_rendered_has_a_manifest_too(tmp_path: Path) -> None:
    deps, _ = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    req = OutputRequest(
        job_id="j1",
        index=0,
        slug="demo",
        layout=Layout(own=part.piece_key),
        parts=[part],
        name=None,
        bom=[],
        files={},
        record=_record([part.piece_key]),
    )
    out = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert [(m.part, m.count, m.plates) for m in out.manifest] == [(part.piece_key, 1, part.plates)]


async def test_provenance_from_an_earlier_output_is_carried(tmp_path: Path) -> None:
    deps, _ = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    earlier = ManifestObject(
        part=part.piece_key,
        file="model.scad",
        slug="dollhouse-kit",
        revision="abc",
        bbox=part.bbox,
        footprint=(1.0, 1.0),
        colours=part.colours,
        count=4,
        bom_piece="wall",
        source_output="o-old",
    )
    req = OutputRequest(
        job_id="j2",
        index=0,
        slug="demo",
        layout=Layout(own=part.piece_key),
        parts=[part],
        name=None,
        bom=[],
        files={},
        record=_record([part.piece_key]),
        provenance={part.piece_key: earlier},
    )
    [obj] = manifest_of(req)
    assert (obj.slug, obj.revision, obj.bom_piece, obj.source_output) == (
        "dollhouse-kit",
        "abc",
        "wall",
        "o-old",
    )
    assert obj.count == 1  # the count is this layout's, never the source's


def test_an_unknown_output_reads_as_none(tmp_path: Path) -> None:
    assert OutputStore(DataPaths(tmp_path)).manifest("never-written") == []


async def test_an_output_saved_before_manifests_reads_as_none(tmp_path: Path) -> None:
    """A job without pipeline outputs (one saved before phase 5) writes no manifest.json."""
    paths, job, _ = await finished_job(tmp_path)
    legacy = job.model_copy(update={"outputs": []})
    store = OutputStore(paths)
    meta = store.create(legacy, name="old")
    assert not (store.directory(meta.id) / "manifest.json").exists()
    assert store.manifest(meta.id) == []
    assert store.arranged_from(meta.id) == []


@pytest.mark.requires_postgres
async def test_a_saved_output_keeps_its_parts_after_the_job_is_pruned(
    tmp_path: Path, pg_conninfo: str
) -> None:
    from tests.support.store import store_pool

    paths, meta, written = await saved_output(tmp_path)
    key = written.manifest[0].part
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        refs.add(key, "job", "j1")
        hold_parts(refs, meta.id, written.manifest)
        refs.drop_holder("job", "j1")  # what JobProjection.prune does
        blobs = LocalBlobStore(paths.blobs)
        assert key not in sweep_blobs(blobs, refs, grace=0, now=10**12)
        assert blobs.exists(key)
        release_parts(refs, meta.id)
        assert key in sweep_blobs(blobs, refs, grace=0, now=10**12)
