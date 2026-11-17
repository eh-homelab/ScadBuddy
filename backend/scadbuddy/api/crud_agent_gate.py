"""The tool-call gate's writes behind CRUD routes (#5299 step 10, D2): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`). Each
route is one of ``agent/src/gate/store.ts``'s writers, one transaction (the open two, as the
agent's); the SQL is ``agent_data/gate.py``. The agent's gate (``gate/store.ts``,
``gate/crudStore.ts`` `CrudGate`) calls them once ``…/areas`` lists ``gate``, and its own SQL
until then (D9), except the grant, which reads the backend's ``flow_grants`` and has no SQL
left in the agent (``auth/revoke.py`` ``RETIRED_GRANTS``).

Area ``gate``:

- ``POST …/gate/open``: an entry opened, after the Reset's cancels of the session's entries of
  another workflow run; ``opened`` False when it was open or resolved already;
- ``POST …/gate/grant``: a run grant's approval of a call, only while the backend's record of
  the grant's use holds (``granted`` False otherwise, and nothing written);
- ``POST …/gate/deny``: a flow's refusal of a call (``denied`` False when it was resolved);
- ``POST …/gate/resolve``: an entry resolved (``resolved`` False when it was gone);
- ``POST …/gate/decide``: a response decided against the row's record under the row's lock,
  the gate's validator on the API: ``refused`` with the validator's code, ``workflow`` when the
  workflow holds the entry, ``live_check`` when the agent must first find the row's run still
  running (then nothing is written), else ``decided``;
- ``GET …/gate/response?request_id=…``: the response row (``record: null`` while open).

Every write answers ``logged``: the batches of session events it appended, with their seqs,
which the agent hands to its event log once the answer is back. A request id travels in the
body or the query, never the path. A retried open, grant, deny or resolve writes nothing
twice; a decide of a row already gone is ``stale``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal, TypeVar

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from scadbuddy.agent_data import gate
from scadbuddy.agent_data.gate_validate import RESPONSE_MAX, RespondRefused, response_bytes
from scadbuddy.api.deps import StateDep
from scadbuddy.crud.guard import crud_router
from scadbuddy.flows.agent_reads import Pool

router = crud_router("agent")

T = TypeVar("T")

#: A session's id: a UUID.
UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
#: The longest request id (the agent's ``gate/crudStore.ts`` ``REQUEST_ID_MAX``): a longer one
#: is never sent, and reads as not found there.
REQUEST_ID_MAX = 2048
#: The longest name, id or hash a write carries (a tool, a run id, a workflow id).
NAME_MAX = 1024
#: The longest summary, prompt or reason.
TEXT_MAX = 64 * 1024
#: The largest JSON of a question card, an attention request or an entry's record.
CARD_JSON_MAX = 1024 * 1024
#: The largest response, in UTF-8 bytes as the validator counts them (``response_bytes``):
#: twice its cap, so one past the cap reaches the validator and is refused ``too_large``, as
#: on the SQL path, and never a 422.
RESPONSE_JSON_MAX = 2 * RESPONSE_MAX

RequestId = Annotated[str, Field(min_length=1, max_length=REQUEST_ID_MAX)]
SessionId = Annotated[str, Field(pattern=UUID_PATTERN)]
Name = Annotated[str, Field(max_length=NAME_MAX)]
Text = Annotated[str, Field(max_length=TEXT_MAX)]


def _pool(state: Any) -> Pool:
    pool: Pool = state.settings_store.pool
    return pool


async def _in_conn[T](state: Any, fn: Callable[[Any], T]) -> T:
    def run() -> T:
        with _pool(state).connection() as conn:
            return fn(conn)

    return await asyncio.to_thread(run)


def _bounded_response(value: Any) -> Any:
    if value is not None and response_bytes(value) > RESPONSE_JSON_MAX:
        raise ValueError(f"a response is larger than {RESPONSE_JSON_MAX} bytes")
    return value


def _bounded(value: Any, limit: int, what: str) -> Any:
    if value is not None and len(json.dumps(value)) > limit:
        raise ValueError(f"{what} is larger than {limit} characters of JSON")
    return value


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Principal(_Body):
    """``{kind, id, label}``: who a call was made for, or who answered it."""

    kind: Name
    id: Name
    label: Name


class Logged(BaseModel):
    session_id: str
    events: list[dict[str, Any]]
    seqs: list[int]


def _logged(batches: list[gate.Logged]) -> list[Logged]:
    return [Logged(session_id=b.session_id, events=b.events, seqs=b.seqs) for b in batches]


class Open(_Body):
    """The agent's ``OpenInput``."""

    request_id: RequestId
    session_id: SessionId
    workflow_id: Name
    workflow_run_id: Name
    kind: Literal["approval", "answer"]
    tool: Name
    tool_use_id: Name
    summary: Text
    input_hash: Name | None
    prompt: Text
    requested_by: Principal
    responders: list[Literal["browser", "grant"]] = Field(max_length=2)
    expires_at: datetime
    questions: list[Any] | None = None
    #: An attention request's ``{reason, on_timeout}``.
    attention: dict[str, Any] | None = None
    flow_run_id: Name | None = None
    hop: Literal["parent", "person"] | None = None
    hop_until: datetime | None = None
    parent_session: SessionId | None = None
    parent_principal: Name | None = None
    outside_reason: Text | None = None
    gate: dict[str, Any] | None = None

    @field_validator("questions", "attention", "gate")
    @classmethod
    def _card(cls, value: Any) -> Any:
        return _bounded(value, CARD_JSON_MAX, "a question card or an entry's record")


class Opened(BaseModel):
    #: False when the entry was open already, or its request resolved.
    opened: bool
    logged: list[Logged]


@router.post("/gate/open", name="crud_agent_gate_open")
async def open_input(body: Open, state: StateDep) -> Opened:
    args = gate.OpenInput(
        request_id=body.request_id,
        session_id=body.session_id,
        workflow_id=body.workflow_id,
        workflow_run_id=body.workflow_run_id,
        kind=body.kind,
        tool=body.tool,
        tool_use_id=body.tool_use_id,
        summary=body.summary,
        input_hash=body.input_hash,
        prompt=body.prompt,
        requested_by=body.requested_by.model_dump(),
        responders=list(body.responders),
        expires_at=body.expires_at,
        questions=body.questions,
        attention=body.attention,
        flow_run_id=body.flow_run_id,
        hop=body.hop,
        hop_until=body.hop_until,
        parent_session=body.parent_session,
        parent_principal=body.parent_principal,
        outside_reason=body.outside_reason,
        gate=body.gate,
    )
    written = await _in_conn(state, lambda conn: gate.open_input(conn, args))
    return Opened(opened=written.done, logged=_logged(written.logged))


class Grant(_Body):
    """The agent's ``GrantInput``."""

    request_id: RequestId
    session_id: SessionId
    tool: Name
    summary: Text
    input_hash: Name
    grant_id: Name
    requested_by: Principal


class Granted(BaseModel):
    #: False when the backend's record of the grant's use does not hold: nothing is written.
    granted: bool


@router.post("/gate/grant", name="crud_agent_gate_grant")
async def grant_input(body: Grant, state: StateDep) -> Granted:
    granted = await _in_conn(
        state,
        lambda conn: gate.grant_input(
            conn,
            request_id=body.request_id,
            session_id=body.session_id,
            tool=body.tool,
            summary=body.summary,
            input_hash=body.input_hash,
            grant_id=body.grant_id,
            requested_by=body.requested_by.model_dump(),
        ),
    )
    return Granted(granted=granted)


class Deny(_Body):
    """The agent's ``DenyInput``."""

    request_id: RequestId
    session_id: SessionId
    run_id: Name
    tool: Name
    summary: Text
    input_hash: Name
    reason: Text | None
    requested_by: Principal


class Denied(BaseModel):
    #: False when the request was resolved already.
    denied: bool


@router.post("/gate/deny", name="crud_agent_gate_deny")
async def deny_input(body: Deny, state: StateDep) -> Denied:
    denied = await _in_conn(
        state,
        lambda conn: gate.deny_input(
            conn,
            request_id=body.request_id,
            session_id=body.session_id,
            run_id=body.run_id,
            tool=body.tool,
            summary=body.summary,
            input_hash=body.input_hash,
            reason=body.reason,
            requested_by=body.requested_by.model_dump(),
        ),
    )
    return Denied(denied=denied)


Outcome = Literal["approved", "denied", "expired", "answered", "cancelled", "timed_out"]


class Resolve(_Body):
    """The agent's ``ResolveInput``; ``response_hash`` is its hash of the raw response."""

    request_id: RequestId
    outcome: Outcome
    responder: Principal
    response: Any = None
    response_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: Text | None = None

    @field_validator("response")
    @classmethod
    def _response(cls, value: Any) -> Any:
        return _bounded_response(value)


class Resolved(BaseModel):
    #: False when the entry was gone (resolved by another, or never opened).
    resolved: bool
    logged: list[Logged]


@router.post("/gate/resolve", name="crud_agent_gate_resolve")
async def resolve_input(body: Resolve, state: StateDep) -> Resolved:
    written = await _in_conn(
        state,
        lambda conn: gate.resolve_input(
            conn,
            request_id=body.request_id,
            outcome=body.outcome,
            responder=body.responder.model_dump(),
            response=body.response,
            digest=body.response_hash,
            reason=body.reason,
        ),
    )
    return Resolved(resolved=written.done, logged=_logged(written.logged))


class Decide(_Body):
    """The agent's ``DecideInput``: the response, who sent it and the role the route gave it.

    ``check_live``: the agent asks whether the row's run still runs before a write;
    ``live_run`` is the run it has just found running (``live_check``'s)."""

    request_id: RequestId
    response: Any
    responder: Principal
    role: Literal["browser", "grant", "owner", "parent", "run_grant"]
    check_live: bool = False
    live_run: Name | None = None
    #: The agent's hash of the response as the person sent it (#3724), as a resolve sends it.
    response_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("response")
    @classmethod
    def _response(cls, value: Any) -> Any:
        return _bounded_response(value)


class Refused(BaseModel):
    #: The validator's code (``validate.ts`` ``REFUSALS``).
    code: str
    message: str


class DecidedAs(BaseModel):
    kind: Literal["approval", "answer"]
    outcome: Outcome


class LiveCheck(BaseModel):
    workflow_id: str
    run_id: str


class DecideAnswer(BaseModel):
    """Exactly one of ``refused``, ``workflow``, ``live_check`` and ``decided`` is set."""

    refused: Refused | None = None
    workflow: bool = False
    live_check: LiveCheck | None = None
    decided: DecidedAs | None = None
    logged: list[Logged] = []


@router.post("/gate/decide", name="crud_agent_gate_decide")
async def decide_input(body: Decide, state: StateDep) -> DecideAnswer:
    def run(conn: Any) -> DecideAnswer:
        try:
            result = gate.decide_input(
                conn,
                request_id=body.request_id,
                response=body.response,
                responder=body.responder.model_dump(),
                role=body.role,
                live_run=body.live_run,
                check_live=body.check_live,
                digest=body.response_hash,
            )
        except RespondRefused as refusal:
            return DecideAnswer(refused=Refused(code=refusal.code, message=str(refusal)))
        if result == "workflow":
            return DecideAnswer(workflow=True)
        if isinstance(result, gate.LiveCheck):
            return DecideAnswer(
                live_check=LiveCheck(workflow_id=result.workflow_id, run_id=result.run_id)
            )
        return DecideAnswer(
            decided=DecidedAs(kind=result.kind, outcome=result.outcome),
            logged=_logged(result.logged),
        )

    return await _in_conn(state, run)


class InputRecord(BaseModel):
    outcome: Outcome
    response: Any
    responder: dict[str, Any]
    reason: str | None


class Record(BaseModel):
    record: InputRecord | None


@router.get("/gate/response", name="crud_agent_gate_response")
async def read_input(
    state: StateDep,
    request_id: Annotated[str, Query(min_length=1, max_length=REQUEST_ID_MAX)],
) -> Record:
    row = await _in_conn(state, lambda conn: gate.read_input(conn, request_id))
    return Record(record=None if row is None else InputRecord.model_validate(row))
