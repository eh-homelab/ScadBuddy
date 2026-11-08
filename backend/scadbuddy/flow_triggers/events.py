"""Event triggers on the event bus (#2484, spec 2026-10-11 §12.3), never by polling.

Every replica hears each event and starts ``FlowTriggerFire`` for each matching trigger
under one workflow id per trigger and event (an order event: one per trigger and status
change, `_change_fires`), with ``REJECT_DUPLICATE``, so one fire runs;
the fire's own row holds it to one beyond Temporal's retention. A template or print event
lost while the bus was disconnected (``bus.resync``) fires nothing. An order or item status
change heard late fires on the next event for its order or item, while it is still among
the newest ``CHANGES_HEARD`` rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any, NamedTuple

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from scadbuddy.core.events import (
    Event,
    EventBus,
    JobEvent,
    OrderChanged,
    OrderItemChanged,
    PrintOutcome,
    VersionCommitted,
)
from scadbuddy.flow_triggers.models import EventKind
from scadbuddy.flow_triggers.store import StatusChange, TriggerStore
from scadbuddy.workflows.flow_triggers import FIRE_TIMEOUT, FIRE_WORKFLOW, schedule_id

logger = logging.getLogger(__name__)

#: A print's recorded outcome (#2665) and the trigger event it fires. A cancelled print
#: fires neither: a person stopped it, which is a decision, not a failure (Ruling 24);
#: nor does one whose end is unknown.
PRINT_KINDS: dict[str, EventKind] = {"finished": "print.finished", "failed": "print.failed"}


class TriggerEvent(NamedTuple):
    """A bus event as triggers see it: its kind, the subjects it is about (by subject
    kind, `flow_triggers/models.py` `SUBJECTS`), its fire key and its facts. An order
    event's key is completed by `fire_event` with the change that set its status."""

    kind: EventKind
    subjects: dict[str, str]
    key: str
    facts: dict[str, Any]


def trigger_event(event: Event) -> TriggerEvent | None:
    """The trigger event a bus event is, or None for one no trigger fires on."""
    if isinstance(event, JobEvent) and event.kind == "job.done":
        facts = {"kind": "render.finished", "slug": event.slug, "job_id": event.job_id}
        return TriggerEvent(
            "render.finished", {"template": event.slug}, f"job:{event.job_id}", facts
        )
    if isinstance(event, VersionCommitted):
        facts = {"kind": "model.changed", "slug": event.slug, "commit": event.commit}
        key = f"commit:{event.slug}:{event.commit}"
        return TriggerEvent("model.changed", {"template": event.slug}, key, facts)
    if isinstance(event, PrintOutcome) and (kind := PRINT_KINDS.get(event.outcome)) is not None:
        print_facts: dict[str, Any] = {
            "kind": kind,
            "slug": event.slug,
            "run_id": event.run_id,
            "output_id": event.output_id,
            "printer": event.printer,
        }
        return TriggerEvent(kind, {"template": event.slug}, f"print:{event.run_id}", print_facts)
    if isinstance(event, OrderChanged | OrderItemChanged) and event.cart:
        # A storefront visitor's cart (#5282): a visitor never starts the owner's flows.
        return None
    if isinstance(event, OrderChanged):
        # The key names the order; `fire_event` adds each status change it fires for.
        order_facts: dict[str, Any] = {
            "kind": "order.changed",
            "order_id": event.order_id,
            "status": event.status,
        }
        key = f"order:{event.order_id}"
        return TriggerEvent("order.changed", {"order": event.order_id}, key, order_facts)
    if isinstance(event, OrderItemChanged):
        item_facts: dict[str, Any] = {
            "kind": "order_item.changed",
            "order_id": event.order_id,
            "item_id": event.item_id,
            "status": event.status,
            "attempt": event.attempt,
        }
        key = f"order_item:{event.item_id}"
        subjects = {"order": event.order_id, "order_item": event.item_id}
        return TriggerEvent("order_item.changed", subjects, key, item_facts)
    return None


async def _change_fires(
    store: TriggerStore, found: TriggerEvent
) -> list[tuple[str, str, dict[str, Any]]]:
    """An order event's fires (§7.1): one per trigger and status change recorded
    (`order_status_changes`), keyed by the change, never by the event. A plain edit, a
    hold or a release records none, so it fires nothing new; a return to a status is a
    change of its own; changes made before a trigger listened (`live_since`: made, its event last
    edited or last resumed) and fires recorded already are skipped. The facts are the
    row's now: none when it is gone."""
    kind, subjects, key, facts = found
    order_id, item_id = facts["order_id"], facts.get("item_id")
    if kind == "order.changed":
        row = await store.order_facts(order_id)
        if row is None:
            return []  # the order is gone
        facts = {**facts, **row}
    elif not await store.item_exists(order_id, str(item_id)):
        return []  # the item is gone
    changes = await store.status_changes(order_id, item_id)
    each: list[dict[str, Any]] = []
    for change in changes:
        change_facts = {**facts, "status": change.status}
        if kind == "order_item.changed" and (
            change is not changes[-1] or change.status != facts["status"]
        ):
            change_facts["attempt"] = None  # the event's attempt is its own change's
        # This gives the event's attempt to the newest change of the event's status. It
        # assumes an attempt event with no change of its own never arrives while an older
        # change to that status is still unfired.
        each.append(change_facts)
    attempt = facts.get("attempt") if kind == "order_item.changed" else None
    # One read of the triggers for every change.
    heard = await store.event_triggers_each(kind, subjects, each)
    planned: list[tuple[str, str, dict[str, Any], StatusChange]] = []
    for change, change_facts, trigger_ids in zip(changes, each, heard, strict=True):
        for trigger_id in trigger_ids:
            fire_key = fire_workflow_id(trigger_id, f"{key}:{change.status}:{change.id}")
            planned.append((trigger_id, fire_key, change_facts, change))
    if not planned:
        return []
    since = await store.live_since({p[0] for p in planned})
    fired = await store.fired({(p[0], p[1]) for p in planned})
    kept = [
        (trigger_id, fire_key, change_facts, change)
        for trigger_id, fire_key, change_facts, change in planned
        if trigger_id in since
        and change.at >= since[trigger_id]
        and (trigger_id, fire_key) not in fired
    ]
    if attempt is not None:
        # A burst (`printed` attempt 1, `queued`, `printed` attempt 2) heard late leaves
        # a trigger two unfired changes to the event's status; which attempt each was
        # is not recorded, so neither is given one, never a wrong one.
        unfired: dict[str, int] = {}
        for trigger_id, _, _, change in kept:
            if change.status == facts["status"]:
                unfired[trigger_id] = unfired.get(trigger_id, 0) + 1
        kept = [
            (t, k, {**f, "attempt": None} if unfired.get(t, 0) > 1 else f, c) for t, k, f, c in kept
        ]
    return [(trigger_id, fire_key, change_facts) for trigger_id, fire_key, change_facts, _ in kept]


def fire_workflow_id(trigger_id: str, key: str) -> str:
    return f"{schedule_id(trigger_id)}-ev-{hashlib.sha256(key.encode()).hexdigest()[:32]}"


async def fire_event(
    client: Client, task_queue: str, store: TriggerStore, event: Event
) -> list[str]:
    """Start a fire for each trigger `event` matches; the fire workflow ids started."""
    found = trigger_event(event)
    if found is None:
        return []
    kind, subjects, key, facts = found
    if kind in ("order.changed", "order_item.changed"):
        fires = await _change_fires(store, found)
    else:
        ids = await store.event_triggers(kind, subjects, facts)
        fires = [(t, fire_workflow_id(t, key), facts) for t in ids]
    started: list[str] = []
    for trigger_id, workflow_id, fire_facts in fires:
        try:
            await client.start_workflow(
                FIRE_WORKFLOW,
                args=[trigger_id, fire_facts],
                id=workflow_id,
                task_queue=task_queue,
                execution_timeout=FIRE_TIMEOUT,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            continue  # another replica heard it first
        started.append(workflow_id)
    return started


def listen_for_triggers(
    events: EventBus, client: Client, task_queue: str, store: TriggerStore
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Fire the event triggers of every event heard, off the listener's thread. Returns
    the remover, which stops hearing events and waits for the fires in flight."""
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()
    stopped = False

    async def run(event: Event) -> None:
        try:
            await fire_event(client, task_queue, store, event)
        except Exception:
            logger.exception("could not fire the triggers of an event", extra={"event": event.id})

    def schedule(event: Event) -> None:
        if stopped:
            return
        task = loop.create_task(run(event))
        pending.add(task)
        task.add_done_callback(pending.discard)

    def on_event(event: Event) -> None:
        if trigger_event(event) is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(schedule, event)

    remove_listener = events.add_listener(on_event)

    async def remove() -> None:
        nonlocal stopped
        stopped = True
        remove_listener()
        while pending:
            await asyncio.wait(set(pending))

    return remove
