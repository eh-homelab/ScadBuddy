"""A command answered 202 while its Update has not yet answered (spec 2026-10-13
§5.1, #2340): what the client follows instead of sending the request again."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Literal
from urllib.parse import urlencode

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import HTTPConnection

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


#: The request being answered, when it asked for ``Prefer: respond-async``: for a
#: command answered deep in a shared helper (`run_operation`), which has no request.
_respond_async: ContextVar[Request | None] = ContextVar("respond_async", default=None)


async def note_preference(connection: HTTPConnection) -> None:
    """An app-wide dependency (#2794): records whether this request follows a 202
    itself, for `respond_async_request`. Set on every request, so none inherits another's.
    It runs for the WebSocket routes too, which have no `Request` and never answer 202."""
    async_request = connection if isinstance(connection, Request) else None
    if async_request is not None and not prefers_async(async_request):
        async_request = None
    _respond_async.set(async_request)


def respond_async_request() -> Request | None:
    """The request being answered, if it asked for ``Prefer: respond-async``."""
    return _respond_async.get()


class AcceptingAnswer(Exception):  # noqa: N818 - an answer, not an error
    """Raised where a helper decides the route's answer is the 202 `accepting`: it
    leaves the route as the 503 `command-still-accepting` it replaces did, so whatever
    that unwound is unwound the same way."""

    def __init__(self, response: JSONResponse) -> None:
        super().__init__(response)
        self.response = response


def install_accepting_handler(app: FastAPI) -> None:
    @app.exception_handler(AcceptingAnswer)
    async def _accepting(request: Request, exc: AcceptingAnswer) -> JSONResponse:
        return exc.response


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
