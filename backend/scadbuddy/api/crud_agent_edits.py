"""An edit of a session behind a CRUD route (#5299 step 9, #6449): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`), area
``session-edits``. ``POST …/sessions/{id}/edit`` is the agent's ``sessions/edits.ts``
``PgEditRows.edit`` transaction (`agent_data/edits.py`): a rename, marking it done and
archiving it, under the row's lock and only while ``principal`` owns it, with its warm
eviction, an archive's ``done`` summaries dismissed, and their events, in one transaction
(#5299 D2). It answers what it logged (as the gate's routes do) and the summaries it dismissed,
which the agent audits; a refusal is 404 ``gone`` or 409 with its reason (``changed_owner``,
``busy_done``, ``busy_archive``, ``parked``, and last ``untold``: a newly done session whose
workflow the agent did not say it told, which the agent sends first and then tells the
workflow). A lock or connection wait past the turn routes' bounds is 503, as theirs
(``crud_agent_turns.py`` ``_in_conn``): 10 s for a connection, then 10 s for the row's lock, under
the agent's 30 s request (``sessions/turnRows.ts`` ``ROW_WRITE_TIMEOUT_MS``, which ``CrudEditRows``
uses too). Change them together.

The session id is the only id in a path, a UUID checked by its pattern. The body is bounded."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, Path, Request, status
from pydantic import BaseModel, Field

from scadbuddy.agent_data import edits
from scadbuddy.api.crud_agent_gate import Logged, _Body, _logged
from scadbuddy.api.crud_agent_turns import _in_conn
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
#: An owner's id (``crud_agent_sessions.py`` ``NAME_MAX``).
NAME_MAX = 512
#: A title: a person's or a tool's is at most 200 characters (``routes/sessions.ts``); the row
#: keeps up to ``crud_agent_sessions.py`` ``TITLE_MAX``.
TITLE_MAX = 4096
#: The body: a title and a principal.
BODY_BYTES_MAX = 64 * 1024

SessionIdPath = Annotated[str, Path(pattern=UUID_PATTERN)]


async def _bounded(request: Request) -> None:
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        raise ApiError(status.HTTP_411_LENGTH_REQUIRED, "a body names its Content-Length")
    if int(length) > BODY_BYTES_MAX:
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE, f"a body is at most {BODY_BYTES_MAX} bytes"
        )


class Who(_Body):
    kind: Literal["browser", "bearer", "oidc", "anonymous", "flow"]
    id: str = Field(min_length=1, max_length=NAME_MAX)


class Edit(_Body):
    principal: Who
    #: Trimmed, never empty; null leaves it.
    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    done: bool = False
    #: True archives, false unarchives, null leaves it.
    archived: bool | None = None
    #: The session's workflow was told it is done (the agent's ``endDurable``).
    told: bool = False


class Dismissed(BaseModel):
    #: Every column is ``NOT NULL`` in ``ai_questions`` (``20261001T1826Z_questions.sql``,
    #: ``20261003T1500Z_questions_tool.sql``).
    id: str
    turn_id: str
    tool: str
    tool_use_id: str
    created_at: datetime


class Edited(BaseModel):
    logged: list[Logged]
    dismissed: list[Dismissed]


@router.post(
    "/sessions/{session_id}/edit",
    name="crud_agent_edits_edit",
    dependencies=[Depends(_bounded)],
)
async def edit(session_id: SessionIdPath, body: Edit, state: StateDep) -> Edited:
    principal = (body.principal.kind, body.principal.id)
    try:
        logged, gone = await _in_conn(
            state,
            lambda c: edits.edit(
                c, session_id, principal, body.title, body.done, body.archived, body.told
            ),
        )
    except edits.EditRefusedError as err:
        code = status.HTTP_404_NOT_FOUND if err.why == "gone" else status.HTTP_409_CONFLICT
        raise ApiError(code, f"the edit was refused: {err.why}", reason=err.why) from None
    return Edited(logged=_logged(logged), dismissed=[Dismissed.model_validate(r) for r in gone])
