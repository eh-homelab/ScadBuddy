"""Following a command a route answered 202 for (spec 2026-10-13 §5.2, #2340): the
outcome of the Update it sent, answered as that route would have answered it."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, status
from fastapi.responses import JSONResponse

from scadbuddy.api.accepting import CommandAccepting, accepting, resend
from scadbuddy.api.deps import RenderDep
from scadbuddy.api.jobs import RenderAccepted, submit_problems
from scadbuddy.core.problems import ApiError
from scadbuddy.workflows.commands import CommandResendError, CommandStillAcceptingError

router = APIRouter(prefix="/commands", tags=["commands"])

#: A render's execution (`workflow_id_for_key`); the print run and the Operation kinds
#: follow in later steps (spec §6).
RENDER_PREFIX = "render-"

WorkflowIdPath = Annotated[str, Path(max_length=200)]
UpdateIdPath = Annotated[str, Path(max_length=128)]


@router.post(
    "/{workflow_id}/updates/{update_id}",
    name="follow_command",
    response_model=RenderAccepted,
    summary="Follow a command answered 202",
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": CommandAccepting,
            "description": "still accepting: follow again after `Retry-After` seconds",
        },
        status.HTTP_404_NOT_FOUND: {"description": "not a command this route follows"},
        status.HTTP_409_CONFLICT: {
            "description": (
                "`command-resend`: the Update is not known (never admitted, rejected by a"
                " closing run, or its execution gone); send the original request again"
                " with the same `Idempotency-Key`"
            )
        },
    },
)
async def follow_command(
    workflow_id: WorkflowIdPath,
    update_id: UpdateIdPath,
    request: Request,
    render: RenderDep,
    supersedes: Annotated[str | None, Query(max_length=64)] = None,
) -> RenderAccepted | JSONResponse:
    """Waits up to ten seconds on the Update, then answers it as the render route would
    have: its job, or its refusal."""
    if not workflow_id.startswith(RENDER_PREFIX):
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no command {workflow_id!r} to follow")
    with submit_problems():
        try:
            job = await render.follow(workflow_id, update_id, supersedes=supersedes)
        except CommandStillAcceptingError:
            return accepting(request, workflow_id, update_id, supersedes=supersedes)
        except CommandResendError:
            raise resend() from None
    return RenderAccepted(
        job_id=job.id,
        status_url=request.url_for("get_job", job_id=job.id).path,
        inputs=job.inputs,
    )
