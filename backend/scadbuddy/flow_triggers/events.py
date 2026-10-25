"""Event triggers on the event bus (#2484, spec 2026-10-11 §12.3), never by polling.

Every replica hears each event and starts ``FlowTriggerFire`` for each matching trigger
under one workflow id per trigger and event, with ``REJECT_DUPLICATE``, so one fire runs;
the fire's own row holds it to one beyond Temporal's retention. An event lost while the
bus was disconnected (``bus.resync``) fires nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from scadbuddy.core.events import Event, EventBus, JobEvent, PrintOutcome, VersionCommitted
from scadbuddy.flow_triggers.models import EventKind
from scadbuddy.flow_triggers.store import TriggerStore
from scadbuddy.workflows.flow_triggers import FIRE_TIMEOUT, FIRE_WORKFLOW, schedule_id

logger = logging.getLogger(__name__)

#: A print's recorded outcome (#2665) and the trigger event it fires. A cancelled print
#: fires neither: a person stopped it, which is a decision, not a failure (Ruling 24);
#: nor does one whose end is unknown.
PRINT_KINDS: dict[str, EventKind] = {"finished": "print.finished", "failed": "print.failed"}


def trigger_event(event: Event) -> tuple[EventKind, str, str, dict[str, Any]] | None:
    """The trigger event a bus event is: its kind, its template, its key and its facts."""
    if isinstance(event, JobEvent) and event.kind == "job.done":
        facts = {"kind": "render.finished", "slug": event.slug, "job_id": event.job_id}
        return "render.finished", event.slug, f"job:{event.job_id}", facts
    if isinstance(event, VersionCommitted):
        facts = {"kind": "model.changed", "slug": event.slug, "commit": event.commit}
        return "model.changed", event.slug, f"commit:{event.slug}:{event.commit}", facts
    if isinstance(event, PrintOutcome) and (kind := PRINT_KINDS.get(event.outcome)) is not None:
        print_facts: dict[str, Any] = {
            "kind": kind,
            "slug": event.slug,
            "run_id": event.run_id,
            "output_id": event.output_id,
            "printer": event.printer,
        }
        return kind, event.slug, f"print:{event.run_id}", print_facts
    return None


def fire_workflow_id(trigger_id: str, key: str) -> str:
    return f"{schedule_id(trigger_id)}-ev-{hashlib.sha256(key.encode()).hexdigest()[:32]}"


async def fire_event(
    client: Client, task_queue: str, store: TriggerStore, event: Event
) -> list[str]:
    """Start a fire for each trigger `event` matches; the fire workflow ids started."""
    found = trigger_event(event)
    if found is None:
        return []
    kind, slug, key, facts = found
    started: list[str] = []
    for trigger_id in await store.event_triggers(kind, slug):
        workflow_id = fire_workflow_id(trigger_id, key)
        try:
            await client.start_workflow(
                FIRE_WORKFLOW,
                args=[trigger_id, facts],
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
