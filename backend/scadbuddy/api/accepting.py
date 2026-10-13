"""A command answered 202 while its Update has not yet answered (spec 2026-10-13
§5.1, #2340): what the client follows instead of sending the request again."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlencode

from fastapi import Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from scadbuddy.core.problems import ApiError
from scadbuddy.workflows.commands import RETRY_AFTER_SECONDS

#: RFC 7240's preference: a client that follows the 202 itself asks for it.
RESPOND_ASYNC = "respond-async"
#: The followed Update is not known: send the request again with the same key (§5.2).
COMMAND_RESEND_PROBLEM = "https://scadbuddy.dev/problems/command-resend"


class CommandAccepting(BaseModel):
    """The command's execution exists and its Update has not answered yet: follow
    ``follow_url`` (also the ``Location`` header) for the answer the route would have
    given."""

    status: Literal["accepting"] = "accepting"
    workflow_id: str
    update_id: str
    follow_url: str
    retry_after: int = RETRY_AFTER_SECONDS


def prefers_async(request: Request) -> bool:
    """Whether the request asked for ``Prefer: respond-async``. Every other client keeps
    the 503 `command-still-accepting` it re-sends on (§5.1)."""
    preferences = request.headers.get("prefer", "").split(",")
    return any(
        preference.split(";")[0].split("=")[0].strip().lower() == RESPOND_ASYNC
        for preference in preferences
    )


def accepting(
    request: Request, workflow_id: str, update_id: str, **query: str | None
) -> JSONResponse:
    """The 202 for a command still accepting; ``query`` rides on the follow URL."""
    path = request.url_for("follow_command", workflow_id=workflow_id, update_id=update_id).path
    kept = {name: value for name, value in query.items() if value is not None}
    url = f"{path}?{urlencode(kept)}" if kept else path
    body = CommandAccepting(workflow_id=workflow_id, update_id=update_id, follow_url=url)
    return JSONResponse(
        body.model_dump(),
        status_code=status.HTTP_202_ACCEPTED,
        headers={
            "Location": url,
            "Retry-After": str(body.retry_after),
            "Preference-Applied": RESPOND_ASYNC,
        },
    )


def resend() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "This request is no longer being accepted; send it again with the same"
        " Idempotency-Key to start or join it.",
        type_=COMMAND_RESEND_PROBLEM,
    )
