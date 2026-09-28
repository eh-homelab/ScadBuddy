"""Finished renders kept under their template.

A preview submits on every settled change, and the same values come back often:
reset to the defaults, a slider moved away and back, a preset reapplied. The queue
coalesces identical *waiting* jobs, but a render that already finished was a full
OpenSCAD run again. Here it is kept instead, beside the template's source the way
#274 keeps its media: ``models/<slug>/.renders/<render key>/`` holds the 3MF, the
preview GLB and a manifest, and a submit of the same key at the same revision is
answered with them.

- The key is `job_store.render_key`: slug, revision and parameters. A render is
  kept only when it names a revision; without a repository the key could not tell
  an edited source from the original.
- The directory (`core.paths.RENDERS_DIR_NAME`) is hidden so a duplicate or upload
  staging (which skip ``.*``) never copies another slug's renders, and the models
  repository ignores it (`library.history`).
- An entry is evicted when nothing has used it for ``SCADBUDDY_JOB_TTL`` -- the same
  clock and sweep as the jobs and revision exports. A hit touches it. An entry
  therefore outlives every job that points at it: a job is pruned that long after
  it finished, and its entry was used no earlier than that.
- Anything missing -- no manifest, a file gone -- is a miss, and the render runs
  again and rewrites the entry.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from contextlib import suppress
from pathlib import Path

from pydantic import BaseModel, ValidationError

from scadbuddy.core.paths import BUILTIN_PREFIX, DataPaths
from scadbuddy.core.paths import RENDERS_DIR_NAME as RENDERS_DIR_NAME
from scadbuddy.render.job_models import Job, JobResult
from scadbuddy.render.job_store import render_key
from scadbuddy.render.schema import ParamValue

logger = logging.getLogger(__name__)

MANIFEST_NAME = "render.json"
MODEL_NAME = "model.3mf"
PREVIEW_NAME = "preview.glb"


class CachedRender(BaseModel):
    """The manifest beside the files: what the entry is, and the job's answer."""

    slug: str
    model_version: str
    params: dict[str, ParamValue]
    result: JobResult
    log_tail: list[str] = []


def render_cache_dir(paths: DataPaths, slug: str, key: str) -> Path:
    return paths.model_dir(slug) / RENDERS_DIR_NAME / key


def keep_render(paths: DataPaths, job: Job, result: JobResult, log_tail: list[str]) -> JobResult:
    """Move a finished render's files under its template and return the result
    pointing at them. A render of a model without a revision is returned as it is.

    Keyed by the revision that was *rendered* (`result.source_version`), which is
    the commit `render_job` resolved -- the model may have moved on between submit
    and render, and the entry must name what its files came from. Best effort: if
    the move fails the result stays on the work directory and the render is still
    served from there.
    """
    version = result.source_version
    if job.model_version is None or not version:
        return result
    entry = render_cache_dir(paths, job.slug, render_key(job.slug, job.params, version))
    kept = result.model_copy(
        update={
            "model_3mf": str((entry / MODEL_NAME).relative_to(paths.root)),
            "preview_glb": str((entry / PREVIEW_NAME).relative_to(paths.root)),
        }
    )
    manifest = CachedRender(
        slug=job.slug, model_version=version, params=job.params, result=kept, log_tail=log_tail
    )
    staging = entry.with_name(f"{entry.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        shutil.move(paths.root / result.model_3mf, staging / MODEL_NAME)
        shutil.move(paths.root / result.preview_glb, staging / PREVIEW_NAME)
        (staging / MANIFEST_NAME).write_text(manifest.model_dump_json(indent=2) + "\n", "utf-8")
        # A second render of the same key (a running job cannot be coalesced)
        # replaces the entry; the files are equivalent, the newer ones stay.
        shutil.rmtree(entry, ignore_errors=True)
        os.replace(staging, entry)
    except OSError:
        logger.exception("could not keep the render of %r under its template", job.slug)
        # Put back whatever moved, so the job's own paths still answer.
        with suppress(OSError):
            shutil.move(staging / MODEL_NAME, paths.root / result.model_3mf)
        with suppress(OSError):
            shutil.move(staging / PREVIEW_NAME, paths.root / result.preview_glb)
        shutil.rmtree(staging, ignore_errors=True)
        return result
    return kept


def cached_render(paths: DataPaths, slug: str, key: str) -> CachedRender | None:
    """The kept render for ``key``, if it is whole; a hit marks the entry used."""
    entry = render_cache_dir(paths, slug, key)
    try:
        manifest = CachedRender.model_validate_json((entry / MANIFEST_NAME).read_bytes())
    except (OSError, ValidationError):
        return None
    for name in (manifest.result.model_3mf, manifest.result.preview_glb):
        if not (paths.root / name).is_file():
            return None
    with suppress(OSError):
        os.utime(entry)
    return manifest


def prune_render_cache(paths: DataPaths, ttl: float, *, now: float | None = None) -> list[str]:
    """Evict entries nothing has used in ``ttl`` seconds; returns ``slug/key`` each."""
    cutoff = (now if now is not None else time.time()) - ttl
    removed: list[str] = []
    for model_dir, slug in _model_dirs(paths):
        renders = model_dir / RENDERS_DIR_NAME
        if not renders.is_dir():
            continue
        for entry in sorted(renders.iterdir()):
            if not entry.is_dir() or entry.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(f"{slug}/{entry.name}")
        with suppress(OSError):
            renders.rmdir()  # only when it emptied
    return removed


def _model_dirs(paths: DataPaths) -> list[tuple[Path, str]]:
    """Every template directory with the id it is known by."""
    if not paths.models.is_dir():
        return []
    found: list[tuple[Path, str]] = []
    for child in sorted(paths.models.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child == paths.builtins:
            found += [
                (mirror, f"{BUILTIN_PREFIX}{mirror.name}")
                for mirror in sorted(child.iterdir())
                if mirror.is_dir()
            ]
        else:
            found.append((child, child.name))
    return found
