"""Renders as activity (spec 2026-10-09 §3): ``render_jobs`` states mapped onto the six."""

from __future__ import annotations

from scadbuddy.activity.models import (
    ActivityKind,
    ActivityLink,
    ActivityRow,
    ActivityStatus,
    Facet,
)
from scadbuddy.render.job_models import Job
from scadbuddy.render.projection import workflow_id_for

#: `render/job_models.py` `JobState`; `cancelled` is a superseded render.
_STATUS: dict[str, ActivityStatus] = {
    "pending": "queued",
    "running": "running",
    "done": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}


def row_of(job: Job) -> ActivityRow:
    verb = "Arrange" if job.kind == "arrange" else "Render"
    return ActivityRow(
        id=f"render:{job.id}",
        kind=job.kind,
        subject=job.slug,
        status=_STATUS[job.state],
        title=f"{verb} {job.slug}",
        source="render_jobs",
        source_id=job.id,
        facets={"slug": job.slug, "model_version": job.model_version},
        workflow_id=job.workflow_id or workflow_id_for(job.id),
        traceparent=job.traceparent,
    )


def _links(row: ActivityRow) -> list[ActivityLink]:
    return [ActivityLink("job", "Render", f"/api/v1/jobs/{row.source_id}")]


RENDER = ActivityKind(
    key="render",
    label="Render",
    facets=(
        Facet("slug", "keyword", "Model"),
        Facet("model_version", "keyword", "Version", filterable=False),
    ),
    links=_links,
)
ARRANGE = ActivityKind(
    key="arrange", label="Arrange", facets=(Facet("slug", "keyword", "Model"),), links=_links
)
ACTIVITY_KINDS = (RENDER, ARRANGE)
