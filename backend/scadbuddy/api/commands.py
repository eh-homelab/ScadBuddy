"""Following a command a route answered 202 for (spec 2026-10-13 §5.2, #2340): the
outcome of the Update it sent, answered as that route would have answered it."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, status
from fastapi.responses import JSONResponse

from scadbuddy.activity.component import ACTIVITY, ActivityComponent
from scadbuddy.activity.index import attribute_request
from scadbuddy.api.accepting import CommandAccepting, accepting, resend
from scadbuddy.api.components import component_dep
from scadbuddy.api.deps import PrintRunsDep, RenderDep
from scadbuddy.api.jobs import RenderAccepted, submit_problems
from scadbuddy.api.printing import follow_run
from scadbuddy.bambuddy.runs import PrintRun
from scadbuddy.core.problems import ApiError
from scadbuddy.workflows.commands import CommandResendError, CommandStillAcceptingError

router = APIRouter(prefix="/commands", tags=["commands"])

#: A render's execution (`workflow_id_for_key`), and a print run's (`accept_run`); the
#: Operation kinds follow in a later step (spec §6).
RENDER_PREFIX = "render-"
PRINT_PREFIX = "print-"

WorkflowIdPath = Annotated[str, Path(max_length=200)]
UpdateIdPath = Annotated[str, Path(max_length=128)]


@router.post(
    "/{workflow_id}/updates/{update_id}",
    name="follow_command",
    response_model=RenderAccepted | PrintRun,
    summary="Follow a command answered 202",
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": CommandAccepting,
            "description": "still accepting: follow again after `Retry-After` seconds",
        },
        status.HTTP_404_NOT_FOUND: {"description": "not a command this route follows"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`supersedes` on a command other than a render's"
        },
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
    runs: PrintRunsDep,
    activity: Annotated[ActivityComponent, component_dep(ACTIVITY)],
    supersedes: Annotated[str | None, Query(max_length=64)] = None,
) -> RenderAccepted | PrintRun | JSONResponse:
    """Waits up to `FOLLOW_WAIT` on the Update, then answers it as the route that answered
    202 would have: a render's job (`RenderAccepted`) or a print's run (`PrintRun`),
    or the refusal. An answer is always **200**: a print run answers 200 here whether
    its own route would have said 202 (a new run) or 200 (a repeat); its ``repeated``
    says which. ``supersedes`` is a render's only (§5.3). The answer is attributed in
    Activity as the route attributes it, since that route answered 202 first and
    attributed nothing (#2805, #2834)."""
    if workflow_id.startswith(PRINT_PREFIX):
        if supersedes is not None:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "only a render's follow supersedes a job"
            )
        try:
            run = await follow_run(runs, workflow_id, update_id)
        except CommandStillAcceptingError:
            return accepting(request, workflow_id, update_id)
        except CommandResendError:
            raise resend() from None
        await attribute_request(activity.index, f"print_run:{run.id}")
        return run
    if not workflow_id.startswith(RENDER_PREFIX):
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no command {workflow_id!r} to follow")
    with submit_problems():
        try:
            job = await render.follow(workflow_id, update_id, supersedes=supersedes)
        except CommandStillAcceptingError:
            return accepting(request, workflow_id, update_id, supersedes=supersedes)
        except CommandResendError:
            raise resend() from None
    await attribute_request(activity.index, f"render:{job.id}")
    return RenderAccepted(
        job_id=job.id,
        status_url=request.url_for("get_job", job_id=job.id).path,
        inputs=job.inputs,
    )
