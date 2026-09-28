"""Finished renders are kept under their template -- `<slug>/.renders/<key>/`,
beside the source the way #274 keeps a template's media -- so a resubmit of the
same parameters at the same revision is answered without running OpenSCAD."""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_store import render_key
from scadbuddy.render.jobs import Job, JobResult, PartInfo
from scadbuddy.render.render_cache import (
    MANIFEST_NAME,
    RENDERS_DIR_NAME,
    cached_render,
    keep_render,
    prune_render_cache,
)


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path)
    data.ensure()
    return data


def _job(slug: str = "demo", version: str | None = "abc123", **params: int) -> Job:
    return Job(
        id=uuid.uuid4().hex,
        slug=slug,
        params=dict(params),
        model_version=version,
        created_at=datetime.now(UTC),
    )


def _rendered(paths: DataPaths, job: Job, version: str = "abc123") -> JobResult:
    """What a worker holds once `render_job` returns: the files in the work dir."""
    work = paths.job_work_dir(job.id)
    work.mkdir(parents=True)
    (work / "model.3mf").write_bytes(b"3mf " + job.id.encode())
    (work / "preview.glb").write_bytes(b"glb " + job.id.encode())
    return JobResult(
        model_3mf=str((work / "model.3mf").relative_to(paths.root)),
        preview_glb=str((work / "preview.glb").relative_to(paths.root)),
        source_version=version,
        parts=[PartInfo(name="Color 1", colour="#FF6AC1", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )


def _entry(paths: DataPaths, job: Job, version: str | None = "abc123") -> Path:
    return paths.model_dir(job.slug) / RENDERS_DIR_NAME / render_key(job.slug, job.params, version)


def test_a_finished_render_is_kept_under_its_template(paths: DataPaths) -> None:
    job = _job(n=1)
    result = _rendered(paths, job)

    kept = keep_render(paths, job, result, ["log line"])

    entry = _entry(paths, job)
    assert paths.root / kept.model_3mf == entry / "model.3mf"
    assert paths.root / kept.preview_glb == entry / "preview.glb"
    assert (entry / "model.3mf").read_bytes() == b"3mf " + job.id.encode()
    assert (entry / "preview.glb").read_bytes() == b"glb " + job.id.encode()
    assert (entry / MANIFEST_NAME).is_file()
    # Moved, not copied: the work directory is pruned with the job, the entry is not.
    assert not (paths.job_work_dir(job.id) / "model.3mf").exists()


def test_a_built_in_s_render_is_kept_under_its_mirror(paths: DataPaths) -> None:
    job = _job(slug="builtin:demo", n=1)
    result = _rendered(paths, job)

    kept = keep_render(paths, job, result, [])

    assert (paths.root / kept.model_3mf).is_relative_to(paths.builtins / "demo" / RENDERS_DIR_NAME)


def test_the_kept_render_answers_the_same_key(paths: DataPaths) -> None:
    job = _job(n=1)
    kept = keep_render(paths, job, _rendered(paths, job), ["log line"])

    hit = cached_render(paths, "demo", render_key("demo", {"n": 1}, "abc123"))

    assert hit is not None
    assert hit.result == kept
    assert hit.log_tail == ["log line"]


def test_other_parameters_or_another_revision_miss(paths: DataPaths) -> None:
    job = _job(n=1)
    keep_render(paths, job, _rendered(paths, job), [])

    assert cached_render(paths, "demo", render_key("demo", {"n": 2}, "abc123")) is None
    assert cached_render(paths, "demo", render_key("demo", {"n": 1}, "def456")) is None


def test_an_entry_missing_a_file_is_not_a_hit(paths: DataPaths) -> None:
    job = _job(n=1)
    keep_render(paths, job, _rendered(paths, job), [])
    (_entry(paths, job) / "preview.glb").unlink()

    assert cached_render(paths, "demo", render_key("demo", {"n": 1}, "abc123")) is None


def test_a_render_without_a_revision_is_not_kept(paths: DataPaths) -> None:
    """No repository, no revision in the key: an edit would then be invisible."""
    job = _job(version=None, n=1)
    result = _rendered(paths, job, version="sha256:contenthash")

    assert keep_render(paths, job, result, []) == result
    assert not (paths.model_dir("demo") / RENDERS_DIR_NAME).exists()
    assert (paths.job_work_dir(job.id) / "model.3mf").exists()


def test_a_second_render_of_the_same_key_replaces_the_entry(paths: DataPaths) -> None:
    """A running job cannot be coalesced, so two renders of one key do happen."""
    first, second = _job(n=1), _job(n=1)
    keep_render(paths, first, _rendered(paths, first), [])
    kept = keep_render(paths, second, _rendered(paths, second), [])

    assert (paths.root / kept.model_3mf).read_bytes() == b"3mf " + second.id.encode()


def test_a_hit_marks_the_entry_used(paths: DataPaths) -> None:
    job = _job(n=1)
    keep_render(paths, job, _rendered(paths, job), [])
    entry = _entry(paths, job)
    stale = time.time() - 7200
    os.utime(entry, (stale, stale))

    assert cached_render(paths, "demo", render_key("demo", {"n": 1}, "abc123")) is not None

    assert entry.stat().st_mtime > stale + 3600


def test_entries_unused_for_the_job_ttl_are_evicted(paths: DataPaths) -> None:
    old, fresh, builtin = _job(n=1), _job(n=2), _job(slug="builtin:demo", n=1)
    for job in (old, fresh, builtin):
        keep_render(paths, job, _rendered(paths, job), [])
    stale = time.time() - 7200
    for job in (old, builtin):
        os.utime(_entry(paths, job), (stale, stale))

    removed = prune_render_cache(paths, 3600.0)

    assert sorted(removed) == sorted(
        [f"demo/{_entry(paths, old).name}", f"builtin:demo/{_entry(paths, builtin).name}"]
    )
    assert not _entry(paths, old).exists()
    assert _entry(paths, fresh).exists()
    assert not _entry(paths, builtin).exists()
