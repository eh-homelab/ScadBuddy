"""An order's nested Bambuddy projects (#4253, spec 2026-10-18 §8.1).

On accept the order gets a Bambuddy project named by its number, and each line item that
prints one nested under it (``parent_id``, #930); ``order_projects`` keeps the mapping. An
item's print ensures its project again (an accept whose ensure failed, Bambuddy down) and
sets it as the run's ``project_id``, so the 3MF lands in the item's project folder and the
queue item carries the project, as any print filed under a project does. Nothing here
deletes a project; one deleted in Bambuddy is made again by the next ensure, and the
mapping names the new one (#5263). A project made or found there within the last
``EXISTS_TTL_SECONDS`` is not asked for again (#5273), so a deletion is seen by the
first ensure after that. When it was is ``order_projects.seen_at`` (#5276), which every
replica shares, so no replica keeps a cache of its own; a print of the order that fails
(``Lifecycle.follow_print``, or a start the print path refused, ``print_item``) clears it,
so the retry asks again rather than filing under a project just deleted there. A print's
claim records the project its run is keyed
with, so a retry keys it the same whatever the mapping says since. A cancel keeps the
mapping too (the spec dropped it): a retried print of a cancelled item must key its run
as its first try did (``existing_run``), and a reopened order goes on in the projects it
had rather than making a second of each name.

Every Bambuddy call goes through the seam (``OrderIntegrations.ensure_project``), so
without ``bambuddy_url`` nothing here is asked and nothing is recorded.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from scadbuddy.orders.integrations import OrderIntegrations
from scadbuddy.orders.lifecycle import has_design
from scadbuddy.orders.models import Order, OrderItem, OrderProject
from scadbuddy.orders.store import OrderStore, item_subject, order_subject

#: The longest project name sent; Bambuddy's own field has no documented bound, and a
#: title is up to 200 characters.
NAME_MAX = 120


def item_project_name(order: Order, item: OrderItem) -> str:
    """``ORD-2026-12 · 2 Bracket``: the order's number, the item's place (from 1) and
    title."""
    title = " ".join(item.title.split())
    name = f"{order.number} · {item.position + 1}" + (f" {title}" if title else "")
    return name[:NAME_MAX].rstrip()


def prints(item: OrderItem) -> bool:
    """Whether the item is one a print makes: a template's or a library file's (#5544),
    not a custom item's."""
    return has_design(item) and item.status != "cancelled"


#: How long one Bambuddy project create may take while the order's lock is held.
ENSURE_TIMEOUT_SECONDS = 30.0

#: How long a project made or found in Bambuddy is taken to stand without asking again
#: (#5273): an accept and the prints after it, or a run of prints, ask once. One deleted
#: in Bambuddy within it is made again by the first ensure after.
EXISTS_TTL_SECONDS = 60.0


@asynccontextmanager
async def _locked(store: OrderStore, order_id: str) -> AsyncIterator[None]:
    """``OrderStore.project_lock`` held across awaits. The lock is taken in a worker
    thread, which a cancelled task cannot stop, so that thread itself lets go of a lock
    it got after its task gave up waiting, and a task cancelled just after the thread got
    it lets go in a thread of its own: neither depends on the event loop still running.
    The release in ``finally`` runs in a thread that completes even if the task is
    cancelled again."""
    held = store.project_lock(order_id)
    guard = threading.Lock()
    state = {"abandoned": False, "held": False}

    def enter() -> None:
        held.__enter__()
        with guard:
            if state["abandoned"]:
                held.__exit__(None, None, None)
                return
            state["held"] = True

    try:
        await asyncio.to_thread(enter)
    except BaseException:
        with guard:
            state["abandoned"] = True
            late = state["held"]
        if late:
            threading.Thread(target=held.__exit__, args=(None, None, None), daemon=True).start()
        raise
    try:
        yield
    finally:
        await asyncio.shield(asyncio.to_thread(held.__exit__, None, None, None))


async def _ensure(integrations: OrderIntegrations, name: str, parent_id: int | None) -> int:
    """One create, bounded: nothing holds the order's lock longer than this."""
    async with asyncio.timeout(ENSURE_TIMEOUT_SECONDS):
        return await integrations.ensure_project(name, parent_id)


async def _kept(
    store: OrderStore,
    integrations: OrderIntegrations,
    subject: str,
    known: tuple[int, bool] | None,
) -> int | None:
    """The subject's recorded project while Bambuddy still has it; None for one deleted
    there (#5263), which the caller makes again. One made or found within
    ``EXISTS_TTL_SECONDS`` (``known``'s flag) is not asked of Bambuddy again (#5273)."""
    if known is None:
        return None
    project_id, fresh = known
    if fresh:
        return project_id
    async with asyncio.timeout(ENSURE_TIMEOUT_SECONDS):
        exists = await integrations.project_exists(project_id)
    if not exists:
        return None
    await asyncio.to_thread(store.project_seen, subject, project_id)
    return project_id


async def ensure_projects(
    store: OrderStore,
    integrations: OrderIntegrations,
    order_id: str,
    item_ids: Sequence[str] | None = None,
) -> list[OrderProject]:
    """The order's project, and one for each of ``item_ids`` (None: every item that
    prints), each made in Bambuddy once and recorded; the order's projects after. Without
    a Bambuddy linked, nothing is asked and the answer is what is recorded. A Bambuddy
    failure is raised, with whatever was made before it recorded."""
    if not integrations.linked:
        return await asyncio.to_thread(store.projects, order_id)
    async with _locked(store, order_id):
        order = await asyncio.to_thread(store.order, order_id)
        # A storefront cart (#5282) has no number and never a project.
        if order is None or order.number is None:
            return []
        known = await asyncio.to_thread(store.projects_seen, order_id, EXISTS_TTL_SECONDS)
        top_subject = order_subject(order_id)
        top = await _kept(store, integrations, top_subject, known.get(top_subject))
        if top is None:
            made = await _ensure(integrations, order.number, None)
            top = await asyncio.to_thread(
                store.set_project, order_id, top_subject, made, top_subject in known
            )
        items = await asyncio.to_thread(store.items, order_id)
        wanted = (
            [item for item in items if prints(item)]
            if item_ids is None
            else [item for item in items if item.id in set(item_ids)]
        )
        for item in wanted:
            subject = item_subject(item.id)
            if await _kept(store, integrations, subject, known.get(subject)) is not None:
                continue
            made = await _ensure(integrations, item_project_name(order, item), top)
            await asyncio.to_thread(store.set_project, order_id, subject, made, subject in known)
    return await asyncio.to_thread(store.projects, order_id)


async def item_project(
    store: OrderStore, integrations: OrderIntegrations, order_id: str, item_id: str
) -> int | None:
    """The item's project, ensured (with the order's above it); None without a
    Bambuddy linked."""
    if not integrations.linked:
        return None
    projects = await ensure_projects(store, integrations, order_id, [item_id])
    subject = item_subject(item_id)
    return next((p.project_id for p in projects if p.subject == subject), None)
