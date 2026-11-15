"""The agent's questions and `done` summaries behind CRUD routes (#5299 step 10): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`). Each route is
one statement or transaction of the agent's ``gate/questions.ts`` `QuestionService`; the SQL is
``agent_data/questions.py``. The agent calls them once ``…/areas`` lists ``questions``, and its own
SQL until then (D9).

Area ``questions``:

- ``GET …/questions/entry?id=…``, ``POST …/questions/pending``,
  ``GET …/questions/gate-answer?request_id=…``: the reads;
- ``POST …/questions/answer``: a summary dismissed, with its events and its audit row (409
  ``not_found``, ``conflict`` or ``invalid`` as ``QuestionError``'s codes);
- ``POST …/questions/attention-count``: the attention rate limit's count, only while the caller
  holds the lease ``attention-rate`` (409 ``lease-lost`` otherwise, never a count);
- ``POST …/questions/done-facts`` and ``…/done``: a turn's ``done`` summary's facts, and its
  post, written only while the turn's placement holds the session.

Ids travel in the body or the query, never the path; a malformed one names nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Query, status
from pydantic import BaseModel, Field, field_validator

from scadbuddy.agent_data import questions
from scadbuddy.api.crud_agent_access import LEASE_LOST
from scadbuddy.api.crud_agent_gate import (
    CARD_JSON_MAX,
    NAME_MAX,
    REQUEST_ID_MAX,
    UUID_PATTERN,
    Logged,
    Name,
    Principal,
    Text,
    _Body,
    _bounded,
    _in_conn,
    _logged,
)
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

IdQuery = Annotated[str, Query(min_length=1, max_length=NAME_MAX)]
Uuid = Annotated[str, Field(pattern=UUID_PATTERN)]
#: The most answers one dismissal carries (a card's questions).
ANSWERS_MAX = 16


class Entry(BaseModel):
    session_id: str
    kind: str
    questions: list[dict[str, Any]]
    outcome: str | None
    reason: str | None
    tool: str


class EntryAnswer(BaseModel):
    entry: Entry | None


@router.get("/questions/entry", name="crud_agent_questions_entry")
async def get_entry(state: StateDep, id: IdQuery) -> EntryAnswer:
    row = await _in_conn(state, lambda conn: questions.entry(conn, id))
    return EntryAnswer(entry=None if row is None else Entry.model_validate(row))


class Who(_Body):
    kind: Name
    id: Name


class Summary(BaseModel):
    id: str
    session_id: str
    kind: str
    tool: str
    tool_use_id: str
    questions: list[dict[str, Any]]
    attention_reason: str | None
    on_timeout: str | None
    summary: str | None
    created_at: datetime
    expires_at: datetime | None


class Summaries(BaseModel):
    #: At most ``PENDING_CAP`` plus one: the caller says there were more.
    questions: list[Summary]


@router.post("/questions/pending", name="crud_agent_questions_pending")
async def pending(body: Who, state: StateDep) -> Summaries:
    rows = await _in_conn(state, lambda conn: questions.pending(conn, body.kind, body.id))
    return Summaries(questions=[Summary.model_validate(r) for r in rows])


class GateAnswer(BaseModel):
    session_id: str
    tool: str | None
    requested_at: datetime | None
    response: Any


class GateAnswerAnswer(BaseModel):
    answer: GateAnswer | None


@router.get("/questions/gate-answer", name="crud_agent_questions_gate_answer")
async def gate_answer(
    state: StateDep, request_id: Annotated[str, Query(min_length=1, max_length=REQUEST_ID_MAX)]
) -> GateAnswerAnswer:
    row = await _in_conn(state, lambda conn: questions.gate_answer(conn, request_id))
    return GateAnswerAnswer(answer=None if row is None else GateAnswer.model_validate(row))


class AnswerBody(_Body):
    id: Name
    session_id: Name
    answers: Annotated[list[Text], Field(max_length=ANSWERS_MAX)]
    by: Principal
    #: The agent's keyed hash of the answers (its audit key): never their text.
    input_hash: Name | None = None
    surface: Literal["harness", "mcp", "http", "system"] = "http"
    client_ip: Annotated[str, Field(max_length=200)] | None = None


class Written(BaseModel):
    logged: list[Logged]


@router.post("/questions/answer", name="crud_agent_questions_answer")
async def answer(body: AnswerBody, state: StateDep) -> Written:
    def run(conn: Any) -> list[Any]:
        try:
            return questions.answer(
                conn,
                question_id=body.id,
                session_id=body.session_id,
                answers=list(body.answers),
                by=body.by.model_dump(),
                input_hash=body.input_hash,
                surface=body.surface,
                client_ip=body.client_ip,
            )
        except questions.RefusedError as refused:
            raise ApiError(status.HTTP_409_CONFLICT, str(refused), reason=refused.code) from None

    return Written(logged=_logged(await _in_conn(state, run)))


class CountBody(_Body):
    own: bool
    lease_token: Annotated[str, Field(min_length=1, max_length=NAME_MAX)]


class Count(BaseModel):
    n: int


@router.post("/questions/attention-count", name="crud_agent_questions_attention_count")
async def attention_count(body: CountBody, state: StateDep) -> Count:
    def run(conn: Any) -> int:
        try:
            return questions.attention_count(conn, own=body.own, lease_token=body.lease_token)
        except questions.LeaseLostError:
            raise ApiError(
                status.HTTP_409_CONFLICT, "The lease is not held by this token.", reason=LEASE_LOST
            ) from None

    return Count(n=await _in_conn(state, run))


class FactsBody(_Body):
    session_id: Name
    turn_id: Name
    #: Postgres's text of the turn's claim time, microseconds kept.
    since: Annotated[str, Field(min_length=1, max_length=64)]


class Window(BaseModel):
    id: str
    away_from: datetime
    away_until: datetime | None


class Touch(BaseModel):
    at: datetime
    in_turn: bool
    in_away: bool
    before_asked: bool
    tool: str
    resource_type: str
    resource_id: str | None
    action: str
    model_slug: str | None


class Facts(BaseModel):
    windows: list[Window]
    touches: list[Touch]


@router.post("/questions/done-facts", name="crud_agent_questions_done_facts")
async def done_facts(body: FactsBody, state: StateDep) -> Facts:
    windows, touches = await _in_conn(
        state, lambda conn: questions.done_facts(conn, body.session_id, body.turn_id, body.since)
    )
    return Facts(
        windows=[Window.model_validate(w) for w in windows],
        touches=[Touch.model_validate(t) for t in touches],
    )


class DoneBody(_Body):
    id: Uuid
    session_id: Name
    turn_id: Name
    attempt: Name
    tool: Name
    tool_use_id: Name
    questions: list[dict[str, Any]]
    summary: Text
    unattended: bool

    @field_validator("questions")
    @classmethod
    def _card(cls, value: Any) -> Any:
        return _bounded(value, CARD_JSON_MAX, "a question card")


class Done(BaseModel):
    asked: bool
    #: The refusal the model reads when the turn posted its most already.
    limited: str | None
    logged: list[Logged]


@router.post("/questions/done", name="crud_agent_questions_done")
async def done(body: DoneBody, state: StateDep) -> Done:
    post = questions.DonePost(
        question_id=body.id,
        session_id=body.session_id,
        turn_id=body.turn_id,
        attempt=body.attempt,
        tool=body.tool,
        tool_use_id=body.tool_use_id,
        questions=body.questions,
        summary=body.summary,
        unattended=body.unattended,
    )
    posted = await _in_conn(state, lambda conn: questions.post_done(conn, post))
    return Done(asked=posted.asked, limited=posted.limited, logged=_logged(posted.logged))
