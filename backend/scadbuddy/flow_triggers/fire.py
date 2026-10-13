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
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import activity
from temporalio.client import Client

from scadbuddy.flow_triggers.models import MAX_EVENT_FIRES_PER_HOUR, FireOutcome, Trigger
from scadbuddy.flow_triggers.store import TriggerStore
from scadbuddy.flows.component import Flows
from scadbuddy.flows.definitions import ScriptMissingError
from scadbuddy.flows.inputs import InputsError, resolve_inputs
from scadbuddy.flows.launch import approval_timeout, execution_status, start_execution
from scadbuddy.flows.manifest import raise_tiers, tiers
from scadbuddy.flows.typecheck import check_script
from scadbuddy.workflows.flow_triggers import FIRE_ACTIVITY, ensure_trigger_schedule

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
    Schedule that cannot be paused now is put right at the worker's next start; a fire
    it makes meanwhile finds the row paused."""
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
        if claimed.outcome != "pending":
            return claimed.outcome  # a retry, or the same event heard twice
        outcome, reason = await self._decide(trigger_id, fire_key, run_id)
        await self.store.settle_fire(trigger_id, fire_key, outcome, reason)
        return outcome

    async def _decide(
        self, trigger_id: str, fire_key: str, run_id: str
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
        if trigger.overlap == "skip" and await self.store.previous_run_open(trigger_id, fire_key):
            return "skipped", "overlap"
        if (
            trigger.kind == "event"
            and await self.store.started_since(trigger_id, timedelta(hours=1))
            >= MAX_EVENT_FIRES_PER_HOUR
        ):
            return "skipped", "rate"
        if flows.client is None:
            return "failed", "flows are unavailable"
        if problems := await check_script(definition.script, definition.inputs_schema):
            return "failed", "the flow no longer type-checks: " + problems[0].message
        try:
            inputs = resolve_inputs(definition.inputs_schema, trigger.inputs)
        except InputsError as err:
            return "failed", "the inputs no longer fit: " + "; ".join(err.problems)
        started_by = runs_as(trigger.created_by)
        if await execution_status(flows.client, run_id) is None:
            # No execution: a grant held for this run is an earlier attempt's.
            await flows.store.delete_grant(run_id)
            await self.store.seed_run_grant(run_id, trigger_id, started_by, datetime.now(UTC))
            await start_execution(
                flows,
                flows.client,
                definition=definition,
                run_id=run_id,
                update_id=fire_key,
                started_by=started_by,
                inputs=inputs,
                approval_timeout_s=approval_timeout(flows, definition, trigger.approval_timeout_s),
                tool_tiers=raise_tiers(tiers(), await flows.store.tool_overrides()),
            )
        return "started", None


#: Between attempts to put the Schedules right while Temporal does not take them.
SCHEDULES_RETRY = 5.0


async def ensure_trigger_schedules(client: Client, task_queue: str, store: TriggerStore) -> None:
    """Every schedule trigger's Schedule as its row says (§12.1): one edited while
    Temporal was away is put right here."""
    for trigger in await store.list_triggers():
        if trigger.kind == "schedule":
            await ensure_trigger_schedule(client, trigger, task_queue)


async def ensure_trigger_schedules_logged(
    client: Client, task_queue: str, store: TriggerStore, stop: asyncio.Event
) -> None:
    """`ensure_trigger_schedules`, retried until Temporal takes them or ``stop``."""
    while not stop.is_set():
        try:
            await ensure_trigger_schedules(client, task_queue, store)
            return
        except Exception:
            logger.warning("could not set up the flow triggers' Schedules; retrying", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), SCHEDULES_RETRY)
