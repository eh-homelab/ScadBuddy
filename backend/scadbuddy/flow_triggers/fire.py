"""A trigger's fire (#2484, spec 2026-10-11 §12.2): the ``flow_trigger_fire`` activity on
the ``library`` queue, whose worker holds the data volume a version's script is read
from.

It takes the trigger id, its own fire key (the fire workflow's id) and the event's facts,
and acts only on what the trigger's row says: the version, the inputs, who made it and
its standing grant. The event's facts are recorded on the fire, nothing more.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from temporalio import activity
from temporalio.client import Client

from scadbuddy.flow_triggers.models import (
    MAX_EVENT_FIRES_PER_HOUR,
    FireOutcome,
    Trigger,
    state_hash,
)
from scadbuddy.flow_triggers.store import TriggerChangedError, TriggerStore
from scadbuddy.flows.component import Flows
from scadbuddy.flows.definitions import ScriptMissingError
from scadbuddy.flows.inputs import InputsError, resolve_inputs
from scadbuddy.flows.launch import approval_timeout, execution_status, start_execution
from scadbuddy.flows.typecheck import check_script
from scadbuddy.workflows.flow_triggers import FIRE_ACTIVITY, FIRE_ATTEMPTS, ensure_trigger_schedule

logger = logging.getLogger(__name__)

#: A triggered run's id is this namespace's uuid5 of its trigger and fire key: no
#: request's `Idempotency-Key` can reach it (the routes' runs are `FLOW_NAMESPACE`'s).
TRIGGER_NAMESPACE = uuid.UUID("0c6f5d3e-2484-4b1e-9f07-5f1a7e2c4d84")


def run_id_of(trigger_id: str, fire_key: str) -> str:
    return str(uuid.uuid5(TRIGGER_NAMESPACE, f"{trigger_id}:{fire_key}"))


def runs_as(created_by: dict[str, Any]) -> dict[str, Any]:
    """Who a triggered run acts as (§12.4): the trigger's creator from its row, without
    the session it was made in, which is not there when it fires."""
    if created_by.get("kind") == "agent" and isinstance(created_by.get("principal"), str):
        return {"kind": "agent", "principal": created_by["principal"]}
    if created_by.get("kind") == "browser":
        return {"kind": "browser"}
    raise ValueError("the trigger's creator is not one a run can act as")


async def pause_for_flow(
    store: TriggerStore,
    client: Client | None,
    task_queue: str,
    name: str,
    reason: str,
    by: dict[str, Any],
) -> list[Trigger]:
    """Pause the triggers of a flow archived or deleted (§12.4), row then Schedule. A
    Schedule that cannot be paused now is put right by the worker's next reconcile
    (`reconcile_trigger_schedules`); a fire it makes meanwhile finds the row paused."""
    paused = await store.pause_flow(name, reason, by)
    if client is not None:
        for trigger in paused:
            try:
                await ensure_trigger_schedule(client, trigger, task_queue)
            except Exception:
                logger.warning(
                    "could not pause a trigger's Schedule",
                    extra={"trigger": trigger.id},
                    exc_info=True,
                )
    return paused


class TriggerFires:
    def __init__(
        self,
        store: TriggerStore,
        flows: Callable[[], Flows],
        client: Client,
        task_queue: str,
    ) -> None:
        self.store = store
        self._flows = flows
        self.client = client
        self.task_queue = task_queue

    def all(self) -> list[Callable[..., Any]]:
        return [self.fire]

    @activity.defn(name=FIRE_ACTIVITY)
    async def fire(self, trigger_id: str, fire_key: str, event: dict[str, Any] | None) -> str:
        run_id = run_id_of(trigger_id, fire_key)
        claimed = await self.store.claim_fire(trigger_id, fire_key, run_id, event)
        if claimed is None:
            return "skipped"  # the trigger is gone
        if claimed.outcome not in ("pending", "admitted"):
            return claimed.outcome  # a retry, or the same event heard twice
        try:
            outcome, reason = await self._decide(trigger_id, fire_key, run_id, event is None)
        except Exception:
            # The last attempt: settled, so an admitted fire that never started does
            # not hold the overlap or count toward the rate cap.
            if activity.info().attempt >= FIRE_ATTEMPTS:
                await self.store.settle_fire(trigger_id, fire_key, "failed", "could not start")
            raise
        await self.store.settle_fire(trigger_id, fire_key, outcome, reason)
        return outcome

    async def _decide(
        self, trigger_id: str, fire_key: str, run_id: str, scheduled: bool
    ) -> tuple[FireOutcome, str | None]:
        trigger = await self.store.get(trigger_id)
        if trigger is None:
            return "skipped", "deleted"
        if trigger.paused:
            return "skipped", "paused"
        flows = self._flows()
        try:
            definition = await flows.definitions.get(trigger.definition_id)
        except ScriptMissingError:
            return "failed", "the pinned version's script is missing"
        if definition is None:
            return "failed", "the pinned version is gone"
        if definition.deleted_at is not None or definition.archived_at is not None:
            reason = "flow deleted" if definition.deleted_at is not None else "flow archived"
            await pause_for_flow(
                self.store, self.client, self.task_queue, trigger.name, reason, {"kind": "system"}
            )
            return "skipped", reason
        if flows.client is None:
            return "failed", "flows are unavailable"
        if problems := await check_script(definition.script, definition.inputs_schema):
            return "failed", "the flow no longer type-checks: " + problems[0].message
        try:
            inputs = resolve_inputs(definition.inputs_schema, trigger.inputs)
        except InputsError as err:
            return "failed", "the inputs no longer fit: " + "; ".join(err.problems)
        # What was checked above, which the admission and the grant are bound to: a
        # trigger edited meanwhile starts nothing on its old settings.
        checked = state_hash(trigger, None)
        cap = MAX_EVENT_FIRES_PER_HOUR if trigger.kind == "event" else None
        if await execution_status(flows.client, run_id) is not None:
            # A retry after its start: only an admitted fire starts its run, and no
            # request can name a run id in the trigger namespace.
            return "started", None
        # As the agent will enforce them now (#2704). Unanswered, the fire is retried,
        # admitting nothing yet: there is no file or copy to start it with.
        tool_tiers = await flows.agent_tools.tool_tiers()
        # Admitted again on a retry too: paused or changed since, it starts nothing.
        admitted = await self.store.admit(trigger_id, fire_key, cap, checked, scheduled)
        if admitted is not None:
            return "skipped", admitted
        started_by = runs_as(trigger.created_by)
        # No execution: a grant held for this run is an earlier attempt's.
        await flows.store.delete_grant(run_id)
        try:
            await self.store.seed_run_grant(
                run_id, trigger_id, started_by, datetime.now(UTC), checked
            )
        except TriggerChangedError:
            # Edited since it was admitted: nothing starts on its old settings.
            return "skipped", "changed"
        await start_execution(
            flows,
            flows.client,
            definition=definition,
            run_id=run_id,
            update_id=fire_key,
            started_by=started_by,
            inputs=inputs,
            approval_timeout_s=approval_timeout(flows, definition, trigger.approval_timeout_s),
            tool_tiers=tool_tiers,
        )
        return "started", None


#: Between attempts to put the Schedules right while Temporal does not take them.
SCHEDULES_RETRY = 5.0
#: Between passes once they are right: a route that changed a row while Temporal was
#: away (503) leaves its Schedule out of step until the next pass, not the next start.
RECONCILE_INTERVAL = 300.0


async def ensure_trigger_schedules(client: Client, task_queue: str, store: TriggerStore) -> None:
    """Every trigger's Schedule as its row says (§12.1): one edited while Temporal was
    away is put right here, and a trigger that is no longer a schedule loses its own."""
    for trigger in await store.list_triggers():
        await ensure_trigger_schedule(client, trigger, task_queue)


async def reconcile_trigger_schedules(
    client: Client, task_queue: str, store: TriggerStore, stop: asyncio.Event
) -> None:
    """`ensure_trigger_schedules` until ``stop``: every `RECONCILE_INTERVAL`, or every
    `SCHEDULES_RETRY` while Temporal does not take them."""
    while not stop.is_set():
        try:
            await ensure_trigger_schedules(client, task_queue, store)
            wait = RECONCILE_INTERVAL
        except Exception:
            logger.warning("could not set up the flow triggers' Schedules; retrying", exc_info=True)
            wait = SCHEDULES_RETRY
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), wait)
