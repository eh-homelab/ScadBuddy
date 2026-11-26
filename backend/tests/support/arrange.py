"""A real output with a manifest, rendered by the fake openscad (phase 4's helpers)."""

from __future__ import annotations

from pathlib import Path

from temporalio.testing import ActivityEnvironment

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.render.job_models import BomEntry, Job, PipelineOutput, now
from scadbuddy.store.index import Pool
from scadbuddy.workflows.models import Layout, LayoutPlate, OutputRequest, Placed
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from tests.support.row_outputs import copy_row, output_store
from tests.test_packing_and_outputs import _deps, _record, _render


async def finished_job(
    tmp_path: Path,
    *,
    width: int = 12,
    count: int = 2,
    name: str = "two",
    job_id: str = "j1",
    plates: int = 1,
) -> tuple[DataPaths, Job, PipelineOutput]:
    """A done job for template `demo` whose one output lays out ``count`` copies of one
    Part on each of ``plates`` plates. Its data directory is ``tmp_path / "data"``,
    which is also the API tests' `paths`, so an app built on those fixtures can save it.
    Once per ``tmp_path``: `_deps` creates the template."""
    deps, paths = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": width})
    layout = Layout(
        plates=[
            LayoutPlate(
                items=[
                    Placed(piece_key=part.piece_key, x=i * (width + 10.0), y=0.0)
                    for i in range(count)
                ]
            )
            for _ in range(plates)
        ]
    )
    req = OutputRequest(
        job_id=job_id,
        index=0,
        slug="demo",
        layout=layout,
        parts=[part],
        name=name,
        bom=[BomEntry(piece="wall", label="Wall", count=count * plates, part=part.piece_key)],
        files={},
        record=_record([part.piece_key]),
    )
    written = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    job = Job(
        id=job_id,
        slug="demo",
        state="done",
        created_at=now(),
        result=written.result,
        outputs=[written],
    )
    return paths, job, written


async def saved_output(
    tmp_path: Path, pool: Pool, *, width: int = 12, count: int = 2, name: str = "two"
) -> tuple[OutputStore, OutputMeta, PipelineOutput]:
    """The job of `finished_job` saved with its rows on ``pool`` and its blobs where an
    app on the same data directory reads them (`output_store`)."""
    paths, job, written = await finished_job(tmp_path, width=width, count=count, name=name)
    store = output_store(tmp_path, pool, paths=paths)
    meta = await store.save(job, name=name, index=0)
    return store, meta, written


def copied_output(store: OutputStore, meta: OutputMeta, *, slug: str, output_id: str) -> OutputMeta:
    """``meta``'s output again, as ``output_id`` of template ``slug``: a second
    template's output without rendering a second template (#1864)."""
    return copy_row(store, meta, slug=slug, output_id=output_id)
