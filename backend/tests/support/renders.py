"""Starting a render the way `RenderService.submit` does (#1053), for the workflow tests."""

from __future__ import annotations

import asyncio
import uuid

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy

from scadbuddy.render.job_models import Job, render_key
from scadbuddy.workflows.commands import start_command
from scadbuddy.workflows.models import RenderAnswer, RenderStart
from scadbuddy.workflows.print_models import ACCEPTED_UPDATE


def start_of(job: Job, *, max_pending: int = 0) -> RenderStart:
    """What the route would send for ``job``'s request."""
    return RenderStart(
        slug=job.slug,
        params=dict(job.params),
        inputs=dict(job.inputs),
        model_version=job.model_version,
        render_key=render_key(job.slug, job.params, job.model_version),
        kind=job.kind,
        max_pending=max_pending,
    )


async def start_render(client: Client, queue: str, start: RenderStart, *, id: str) -> RenderAnswer:
    """Update-with-start ``id`` and return the `accepted` answer."""
    return await start_command(
        client,
        "TemplatePipeline",
        start,
        id=id,
        task_queue=queue,
        update=ACCEPTED_UPDATE,
        result_type=RenderAnswer,
        reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )


async def render_to_end(client: Client, queue: str, job: Job, *, timeout: float = 120) -> Job:
    """Render ``job``'s request on ``queue`` (a workflow id of its own) to its end, and
    return the job its first activity recorded."""
    id = f"render-{uuid.uuid4().hex}"
    answer = await start_render(client, queue, start_of(job), id=id)
    assert answer.job is not None, answer
    await asyncio.wait_for(client.get_workflow_handle(id).result(), timeout)
    return answer.job
