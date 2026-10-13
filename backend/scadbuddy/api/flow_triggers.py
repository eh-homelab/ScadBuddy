"""Flow triggers (#2484, spec 2026-10-11 §12): runs of a pinned flow version started on a
Temporal Schedule or on an event, as the trigger's creator, under a standing grant only
if a person approved one.

Who made, changed or paused a trigger, and who asked for its grant, is written here from
the request's author (`core/authorship.py`), never from a body: every body forbids extra
fields. Changing a trigger is open to the browser and to the agent principal that made
it (§12.4); approving or declining its standing grant is the browser's alone.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.api.flows import (
    FLOW_ARCHIVED,
    FLOW_DELETED,
    FLOW_INPUTS,
    GRANT_BROWSER_ONLY,
    GRANT_INVALID,
    GRANT_STATE,
)
from scadbuddy.core.authorship import current_author
from scadbuddy.core.problems import ApiError
from scadbuddy.flow_triggers.component import Triggers, TriggersDep
from scadbuddy.flow_triggers.models import (
    MAX_STANDING_EXPIRY,
    Fire,
    StandingGrant,
    Trigger,
    TriggerCreate,
    TriggerUpdate,
    timeout_seconds,
)
from scadbuddy.flow_triggers.store import (
    LiveGrantError,
    StandingGrantStateError,
    TriggerChangedError,
)
from scadbuddy.flows.definitions import ScriptMissingError
from scadbuddy.flows.grants import GrantSpec, GrantStatus, validate_rules
from scadbuddy.flows.inputs import InputsError, resolve_inputs
from scadbuddy.flows.manifest import raise_tiers, tiers
from scadbuddy.flows.models import Definition
from scadbuddy.workflows.flow_triggers import (
    delete_trigger_schedule,
    ensure_trigger_schedule,
    next_fires,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["flows"])

CREATOR_ONLY = "https://scadbuddy.dev/problems/flow-trigger-creator-only"
TRIGGER_INVALID = "https://scadbuddy.dev/problems/flow-trigger-invalid"
TRIGGER_CHANGED = "https://scadbuddy.dev/problems/flow-trigger-changed"
TEMPORAL_DOWN = "https://scadbuddy.dev/problems/flow-trigger-schedule-unavailable"
FIRES_SHOWN = 20


class StandingGrantView(BaseModel):
    """A trigger's standing grant: what its runs may do without asking."""

    id: str
    status: GrantStatus
    spec: GrantSpec
    requested_by: dict[str, Any]
    approved_by: str | None = None
    approved_at: datetime | None = None
    revoked_by: str | None = None
    revoked_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime
    #: Approved, not revoked and not expired: each fire's run gets a grant from it.
    in_force: bool


class TriggerView(Trigger):
    """A trigger, its standing grant, when it fires next and how its last fires went."""

    grant: StandingGrantView | None = None
    #: The Schedule's next fires; none for an event trigger or a paused one.
    next_fires: list[datetime] = Field(default_factory=list)
    #: The newest fires first, each with its run's status.
    fires: list[Fire] = Field(default_factory=list)
    #: A hash of what is shown of the trigger and its live standing grant: an approval
    #: sends it back, and is refused when either changed since (§12.4).
    seen: str = ""


class ApproveStandingGrant(BaseModel):
    """A person's approval of a trigger's requested standing grant."""

    model_config = ConfigDict(extra="forbid")

    #: The grant as approved, when the person edited the request. Omitted: as requested.
    spec: GrantSpec | None = None
    #: The trigger's `seen` as the person was shown it: refused (409) when the trigger or
    #: the grant changed since.
    seen: str | None = Field(default=None, max_length=64)


class AttachStandingGrant(BaseModel):
    """A standing grant to attach to a trigger."""

    model_config = ConfigDict(extra="forbid")

    spec: GrantSpec
    #: The trigger's `seen` as the caller was shown it. The browser's attach is an
    #: approval, so it needs one (409 when the trigger changed since); an agent's is only
    #: a request, which a person approves later against what they see then.
    seen: str | None = Field(default=None, max_length=64)


def _who() -> dict[str, Any]:
    """Who the request is: the agent it is authored as, else the browser."""
    author = current_author()
    if author is None:
        return {"kind": "browser"}
    return {"kind": "agent", "principal": author.principal, "session": author.session}


def _actor() -> str:
    author = current_author()
    return "browser" if author is None else f"agent {author.principal}"


def _creator_only(trigger: Trigger) -> None:
    """The browser, or the agent principal its row names as its creator (§12.4)."""
    author = current_author()
    if author is None:
        return
    creator = trigger.created_by
    principal = creator.get("principal")
    # Named on both sides: no principal never matches another missing one.
    if (
        creator.get("kind") != "agent"
        or not isinstance(principal, str)
        or not principal
        or not author.principal
        or principal != author.principal
    ):
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "Only a person or the trigger's creator changes it.",
            type_=CREATOR_ONLY,
        )


def _browser_only() -> None:
    if current_author() is not None:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "Only a person approves or declines a trigger's grant.",
            type_=GRANT_BROWSER_ONLY,
        )


async def _load(triggers: Triggers, trigger_id: str) -> Trigger:
    trigger = await triggers.store.get(trigger_id)
    if trigger is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such trigger.")
    return trigger


async def _definition(triggers: Triggers, definition_id: str) -> Definition:
    try:
        definition = await triggers.flows.definitions.get(definition_id)
    except ScriptMissingError:
        definition = None
    if definition is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such flow.")
    return definition


def _refuse_hidden(definition: Definition) -> None:
    if definition.deleted_at is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "This flow was deleted.", type_=FLOW_DELETED)
    if definition.archived_at is not None:
        raise ApiError(status.HTTP_409_CONFLICT, "This flow is archived.", type_=FLOW_ARCHIVED)


def _check_inputs(definition: Definition, inputs: dict[str, Any] | None) -> None:
    try:
        resolve_inputs(definition.inputs_schema, inputs)
    except InputsError as err:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The inputs do not fit the flow's inputs schema: " + "; ".join(err.problems),
            type_=FLOW_INPUTS,
            problems=err.problems,
        ) from None


async def _check_grant(triggers: Triggers, spec: GrantSpec) -> None:
    tool_tiers = raise_tiers(tiers(), await triggers.flows.store.tool_overrides())
    problems = validate_rules(spec.rules, tool_tiers=tool_tiers)
    now = datetime.now(UTC)
    if spec.expires_at <= now:
        problems.append("expires_at is in the past")
    elif spec.expires_at - now > MAX_STANDING_EXPIRY:
        problems.append("expires_at is more than 30 days away")
    if problems:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The grant is not valid.",
            type_=GRANT_INVALID,
            problems=[{"message": p} for p in problems],
        )


async def _sync_schedule(triggers: Triggers, trigger: Trigger) -> None:
    """The trigger's Schedule as its row says. Temporal refusing the spec is the
    request's fault (422); Temporal away is 503, and the worker's next start puts the
    Schedule right."""
    try:
        await ensure_trigger_schedule(triggers.client, trigger, triggers.task_queue)
    except RPCError as err:
        if err.status == RPCStatusCode.INVALID_ARGUMENT:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Temporal refused the schedule: {err.message}",
                type_=TRIGGER_INVALID,
            ) from None
        logger.warning("could not reach Temporal for a trigger's Schedule", exc_info=True)
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ScadBuddy's background service did not answer. Try again shortly.",
            type_=TEMPORAL_DOWN,
            headers={"Retry-After": "5"},
        ) from err


def _grant_view(grant: StandingGrant | None) -> StandingGrantView | None:
    if grant is None:
        return None
    return StandingGrantView(
        **grant.model_dump(exclude={"trigger_id"}), in_force=grant.in_force(datetime.now(UTC))
    )


async def _view(triggers: Triggers, trigger: Trigger) -> TriggerView:
    upcoming: list[datetime] = []
    if trigger.kind == "schedule" and not trigger.paused:
        try:
            upcoming = await next_fires(triggers.client, trigger.id)
        except (RPCError, TimeoutError, ConnectionError):
            logger.warning("could not read a trigger's Schedule", exc_info=True)
    return TriggerView(
        **trigger.model_dump(),
        grant=_grant_view(await triggers.store.current_grant(trigger.id)),
        next_fires=upcoming,
        fires=await triggers.store.fires(trigger.id, FIRES_SHOWN),
        seen=await asyncio.to_thread(triggers.store.live_hash, trigger),
    )


@router.get("/flow-triggers", response_model=list[TriggerView], summary="List flow triggers")
async def list_flow_triggers(
    triggers: TriggersDep,
    name: Annotated[str | None, Query(description="Only this flow's.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[TriggerView]:
    """Newest first, at most `limit`."""
    return [await _view(triggers, t) for t in await triggers.store.list_triggers(name, limit)]


@router.get("/flow-triggers/{trigger_id}", response_model=TriggerView, summary="Get a flow trigger")
async def get_flow_trigger(trigger_id: str, triggers: TriggersDep) -> TriggerView:
    return await _view(triggers, await _load(triggers, trigger_id))


@router.post(
    "/flow-triggers",
    status_code=status.HTTP_201_CREATED,
    response_model=TriggerView,
    summary="Make a flow trigger: a schedule or an event that starts runs of a flow version",
)
async def create_flow_trigger(body: TriggerCreate, triggers: TriggersDep) -> TriggerView:
    """Its runs act as whoever makes it, with their tiers as they are at each fire, and
    carry a grant only from a standing grant a person approves (`.../grant`)."""
    definition = await _definition(triggers, body.definition_id)
    _refuse_hidden(definition)
    _check_inputs(definition, body.inputs)
    trigger = await triggers.store.insert(
        Trigger(
            id=str(uuid.uuid4()),
            name=definition.name,
            definition_id=definition.id,
            kind="schedule" if body.schedule is not None else "event",
            schedule=body.schedule,
            event=body.event,
            overlap=body.overlap,
            inputs=body.inputs,
            approval_timeout_s=timeout_seconds(body.approval_timeout),
            paused=False,
            created_by=_who(),
            created_at=datetime.now(UTC),
        )
    )
    # A new event trigger has no Schedule to make or remove.
    if trigger.kind == "schedule":
        try:
            await _sync_schedule(triggers, trigger)
        except ApiError:
            await triggers.store.delete(trigger.id, revoked_by="system")
            raise
    return await _view(triggers, trigger)


@router.patch(
    "/flow-triggers/{trigger_id}",
    response_model=TriggerView,
    summary="Change a flow trigger; its standing grant goes back to requested",
)
async def update_flow_trigger(
    trigger_id: str, body: TriggerUpdate, triggers: TriggersDep
) -> TriggerView:
    """Any change returns the trigger's standing grant to `requested` (Ruling 20): a person
    approved it for what the trigger was. Runs already started spend nothing more from it;
    its pause is kept."""
    trigger = await _load(triggers, trigger_id)
    _creator_only(trigger)
    sent = body.model_fields_set
    if not sent:
        return await _view(triggers, trigger)
    fields: dict[str, Any] = {}
    definition_id = body.definition_id if "definition_id" in sent else trigger.definition_id
    if definition_id is None:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "definition_id cannot be null.")
    definition = await _definition(triggers, definition_id)
    if "definition_id" in sent:
        _refuse_hidden(definition)
        fields.update(definition_id=definition.id, name=definition.name)
    inputs = body.inputs if "inputs" in sent else trigger.inputs
    _check_inputs(definition, inputs)
    if "inputs" in sent:
        fields["inputs"] = body.inputs
    if body.schedule is not None:
        fields.update(kind="schedule", schedule=body.schedule.model_dump(), event=None)
    elif body.event is not None:
        fields.update(kind="event", event=body.event.model_dump(), schedule=None)
    elif "schedule" in sent or "event" in sent:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "A trigger needs its schedule or event."
        )
    if "overlap" in sent:
        if body.overlap is None:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "overlap cannot be null.")
        fields["overlap"] = body.overlap
    if "approval_timeout" in sent:
        fields["approval_timeout_s"] = timeout_seconds(body.approval_timeout)
    updated = await triggers.store.update(trigger_id, fields, _who(), f"edit by {_actor()}")
    if updated is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such trigger.")
    await _sync_schedule(triggers, updated)
    return await _view(triggers, updated)


@router.post(
    "/flow-triggers/{trigger_id}/pause", response_model=TriggerView, summary="Pause a flow trigger"
)
async def pause_flow_trigger(trigger_id: str, triggers: TriggersDep) -> TriggerView:
    trigger = await _load(triggers, trigger_id)
    _creator_only(trigger)
    paused = await triggers.store.set_paused(trigger_id, True, _who(), None)
    if paused is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such trigger.")
    await _sync_schedule(triggers, paused)
    return await _view(triggers, paused)


@router.post(
    "/flow-triggers/{trigger_id}/resume",
    response_model=TriggerView,
    summary="Resume a flow trigger",
)
async def resume_flow_trigger(trigger_id: str, triggers: TriggersDep) -> TriggerView:
    """Refused (409) while its flow is archived or deleted."""
    trigger = await _load(triggers, trigger_id)
    _creator_only(trigger)
    _refuse_hidden(await _definition(triggers, trigger.definition_id))
    resumed = await triggers.store.set_paused(trigger_id, False, None, None)
    if resumed is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such trigger.")
    await _sync_schedule(triggers, resumed)
    return await _view(triggers, resumed)


@router.delete(
    "/flow-triggers/{trigger_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a flow trigger; its standing grant is revoked",
)
async def delete_flow_trigger(trigger_id: str, triggers: TriggersDep) -> Response:
    """Its Schedule first, then its row, revoking its standing grant and the run grants it
    seeded. Runs already started go on."""
    trigger = await _load(triggers, trigger_id)
    _creator_only(trigger)
    try:
        await delete_trigger_schedule(triggers.client, trigger.id)
    except RPCError as err:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ScadBuddy's background service did not answer. Try again shortly.",
            type_=TEMPORAL_DOWN,
            headers={"Retry-After": "5"},
        ) from err
    await triggers.store.delete(trigger_id, revoked_by=f"delete by {_actor()}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/flow-triggers/{trigger_id}/grant",
    response_model=StandingGrantView,
    summary="Attach a standing grant to a flow trigger",
)
async def attach_flow_trigger_grant(
    trigger_id: str, body: AttachStandingGrant, triggers: TriggersDep
) -> StandingGrantView:
    """From the browser it is approved as sent, bound to the trigger as it was shown
    (`seen`); from an agent it is only requested, until a person approves it. At most 30
    days. 409 while the trigger has a live one, or when it changed since it was shown."""
    trigger = await _load(triggers, trigger_id)
    _creator_only(trigger)
    browser = current_author() is None
    if browser and body.seen is None:
        raise _unseen()
    await _check_grant(triggers, body.spec)
    try:
        grant = await triggers.store.attach_grant(
            trigger_id,
            body.spec,
            "approved" if browser else "requested",
            _who(),
            body.seen if browser else None,
        )
    except LiveGrantError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "This trigger has a grant already: revoke it first.",
            type_=GRANT_STATE,
        ) from None
    except TriggerChangedError:
        raise _changed() from None
    view = _grant_view(grant)
    if view is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such trigger.")
    return view


def _unseen() -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "Say what was approved: send the trigger's `seen` as it was shown.",
        type_=TRIGGER_INVALID,
    )


def _changed() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT,
        "The trigger or its grant changed since it was shown. Look again before approving.",
        type_=TRIGGER_CHANGED,
    )


def _no_grant() -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, "This trigger has no grant.")


def _grant_state(err: StandingGrantStateError) -> ApiError:
    grant = err.grant
    state = "revoked" if grant.revoked_at is not None else grant.status
    return ApiError(
        status.HTTP_409_CONFLICT,
        f"The trigger's grant is {state}: a grant is decided once, and revoking it is final.",
        type_=GRANT_STATE,
    )


@router.post(
    "/flow-triggers/{trigger_id}/grant/approve",
    response_model=StandingGrantView,
    summary="Approve a flow trigger's requested standing grant",
)
async def approve_flow_trigger_grant(
    trigger_id: str,
    triggers: TriggersDep,
    body: Annotated[ApproveStandingGrant | None, Body()] = None,
) -> StandingGrantView:
    """Browser only: no agent approves a grant, its own or another's. Bound to the
    trigger and the grant as they were shown (`seen`): 409 when either changed since."""
    _browser_only()
    await _load(triggers, trigger_id)
    if body is None or body.seen is None:
        raise _unseen()
    current = await triggers.store.current_grant(trigger_id)
    if current is None:
        raise _no_grant()
    spec = body.spec or current.spec
    if current.status == "requested" and current.revoked_at is None:
        await _check_grant(triggers, spec)
    try:
        approved = await triggers.store.approve_grant(trigger_id, spec, body.seen)
    except StandingGrantStateError as err:
        raise _grant_state(err) from None
    except TriggerChangedError:
        raise _changed() from None
    view = _grant_view(approved)
    if view is None:
        raise _no_grant()
    return view


@router.post(
    "/flow-triggers/{trigger_id}/grant/decline",
    response_model=StandingGrantView,
    summary="Decline a flow trigger's requested standing grant",
)
async def decline_flow_trigger_grant(trigger_id: str, triggers: TriggersDep) -> StandingGrantView:
    _browser_only()
    await _load(triggers, trigger_id)
    try:
        declined = await triggers.store.decline_grant(trigger_id)
    except StandingGrantStateError as err:
        raise _grant_state(err) from None
    view = _grant_view(declined)
    if view is None:
        raise _no_grant()
    return view


@router.post(
    "/flow-triggers/{trigger_id}/grant/revoke",
    response_model=StandingGrantView,
    summary="Revoke a flow trigger's standing grant",
)
async def revoke_flow_trigger_grant(trigger_id: str, triggers: TriggersDep) -> StandingGrantView:
    """The browser, or the trigger's creator: revoking only takes authority away. Final;
    the run grants it seeded are revoked with it."""
    trigger = await _load(triggers, trigger_id)
    _creator_only(trigger)
    try:
        revoked = await triggers.store.revoke_grant(trigger_id, _actor())
    except StandingGrantStateError as err:
        raise _grant_state(err) from None
    view = _grant_view(revoked)
    if view is None:
        raise _no_grant()
    return view
