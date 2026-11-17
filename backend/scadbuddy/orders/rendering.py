"""The output a design renders to (#6324): :func:`output_for_reference` takes a
:class:`~scadbuddy.orders.models.Reference` (slug, commit, inputs, libraries, colours) and
answers with the output for it, rendered on the normal pipeline when none is there.

It needs no order item. Given one (:class:`AttachTo`), it reuses the item's own output
while that output still matches the reference, and sets a new one on the item. That
attach is not a design change: the item keeps its revision and its quotes
(``Lifecycle.update_item`` with ``output_id`` alone). An item made by hand, with no colour
slots, takes the render's slots, which amends its current revision.

The render goes through ``RenderService.submit`` (``render/submit.py``), which the
callers ask with ``reuse_finished``: a `done` job of the same render key answers at once,
with no second render. ``saved_as`` is the id a new output is saved under: an output of
that id that matches the reference is an earlier attempt's save, and is reused as it is.

The slice quote's run (``orders/quote.py`` ``ensure_output``) and the item's render
(the ``order_item_render`` operation, ``orders/operations.py``) call it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import status

from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import BACK_OFFICE, OutputMeta, OutputNotFoundError, OutputSource
from scadbuddy.orders.lifecycle import Lifecycle, LifecycleError
from scadbuddy.orders.models import Actor, Order, OrderItem, Reference
from scadbuddy.orders.problems import refused
from scadbuddy.orders.reference import colours_of, is_commit_id
from scadbuddy.render.inputs import inputs_key
from scadbuddy.render.job_models import Job
from scadbuddy.render.runner import with_defaults
from scadbuddy.render.schema import CustomizerSchema

#: How often a run reads a render it waits on (#4250 Ruling 7, Task 4).
FOLLOW_EVERY = 2.0

#: A settled render's states other than `done` (`render/job_models.py` `JobState`).
_SETTLED_JOB = frozenset({"failed", "cancelled"})


class OutputsRead(Protocol):
    """What :func:`output_for_reference` reads of an output (``OutputStore``'s)."""

    def get(self, output_id: str) -> OutputMeta: ...
    def inputs(self, output_id: str) -> dict[str, Any]: ...
    def arranged_from(self, output_id: str) -> list[str]: ...


class Renders(Protocol):
    """What :func:`output_for_reference` renders with: ``RenderService``'s submit and
    read."""

    async def submit(
        self,
        slug: str,
        params: Mapping[str, Any],
        *,
        model_version: str | None = ...,
        inputs: Mapping[str, Any] | None = ...,
        whole_inputs: bool = ...,
    ) -> Job: ...

    def read(self, job_id: str) -> Job: ...


@dataclass(frozen=True)
class RenderedOutput:
    """The output a reference renders to: its id and its colours, as ``meta.json``
    records them, the reference's ``render_key``, and the render job it was saved from
    (the output's ``job_id``)."""

    output_id: str
    colours: list[str]
    render_key: str
    job_id: str | None = None


@dataclass(frozen=True)
class AttachTo:
    """The order item an output is set on (``Lifecycle.update_item``'s ``output_id``).
    ``filaments`` go on the render's slots of an item that has none; ``carried`` (#4673,
    a slice's new design) are the filaments the item had pinned before its revision, put
    on the rendered slot of each number under ``filaments``. With ``revision_id`` nothing
    is written once the item has another revision (`revision_not_newest`). ``check``, when
    given, runs in a thread after the render and before the write, and refuses by raising
    (the item's render checks its refusals again, ``operations.py`` ``render_check``)."""

    lifecycle: Lifecycle
    order: Order
    item: OrderItem
    by: Actor
    filaments: Mapping[int, str] = field(default_factory=dict)
    revision_id: str | None = None
    carried: Mapping[int, str] | None = None
    check: Callable[[], None] | None = None


def reference_matches(
    outputs: OutputsRead,
    output_id: str,
    reference: Reference,
    schema: CustomizerSchema | None = None,
) -> OutputMeta | None:
    """The output's ``meta.json`` when it is the reference's render (#4250 Ruling 7): its
    ``slug``, ``model_version`` and normalised inputs, from one read of it. An arranged
    output records a content hash where a render records its commit, and an item pins
    the template's commit instead (#4565): no render of the reference remakes the
    arrangement, so its hash stands for the pinned commit.

    With ``schema`` (the commit's), both inputs are compared at its defaults where they
    omit one (#4774): an output saved before renders filled them records only the params
    it was sent, and is still the render of a reference that names its defaults."""
    try:
        meta = outputs.get(output_id)
        inputs = outputs.inputs(output_id)
        arranged = not is_commit_id(meta.model_version) and bool(outputs.arranged_from(output_id))
    except (OutputNotFoundError, OSError, ValueError):
        return None
    if (
        meta.slug == reference.slug
        and (meta.model_version == reference.commit or arranged)
        and _at_defaults(inputs, schema) == _at_defaults(reference.inputs, schema)
    ):
        return meta
    return None


def _at_defaults(inputs: Mapping[str, Any], schema: CustomizerSchema | None) -> dict[str, Any]:
    if schema is None:
        return dict(inputs)
    params = inputs.get("params", {})
    if not isinstance(params, dict):
        # A malformed row is compared exactly, never read as an all-defaults render.
        return dict(inputs)
    return {**inputs, "params": with_defaults(schema, params)}


async def output_for_reference(
    reference: Reference,
    *,
    outputs: OutputsRead,
    renders: Renders,
    create_output: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    name: str,
    saved_as: str | None = None,
    schema: CustomizerSchema | None = None,
    follow_every: float = FOLLOW_EVERY,
    item: AttachTo | None = None,
    source: OutputSource = BACK_OFFICE,
) -> RenderedOutput:
    """The output for ``reference``: the item's own while it matches (with ``item``),
    else the output saved under ``saved_as`` while it matches, else the reference
    rendered at its commit with its whole inputs, waited on through the projection and
    saved as ``POST /outputs`` saves one (``create_output``, ``output_create``'s run),
    named ``name``. With ``item`` the output is set on that item (:class:`AttachTo`).
    ``source`` marks who saves a new output (#6371): only the storefront slice run
    passes ``storefront``; an order's render and a quote keep the back office's.

    The save claims ``saved_as`` first (``OutputStore.claim``): a directory already
    there (another attempt's save still writing, or an output that does not match) is
    left alone, and the new output gets an id of its own. A render that fails or is
    cancelled is a 409 that names its job and its error."""
    if item is not None and item.item.output_id is not None:
        own = await asyncio.to_thread(
            reference_matches, outputs, item.item.output_id, reference, schema
        )
        if own is not None:
            return RenderedOutput(
                item.item.output_id, list(own.colors), reference.render_key, own.job_id
            )
    if saved_as is not None:
        earlier = await asyncio.to_thread(reference_matches, outputs, saved_as, reference, schema)
        if earlier is not None:
            found = RenderedOutput(
                saved_as, list(earlier.colors), reference.render_key, earlier.job_id
            )
            if item is not None:
                await attach_output(item, found)
            return found
    params = reference.inputs.get("params", {})
    pipeline = reference.render_key == inputs_key(
        reference.slug, reference.inputs, reference.commit
    )
    job = await renders.submit(
        reference.slug,
        params if isinstance(params, dict) else {},
        model_version=reference.commit,
        inputs=reference.inputs,
        whole_inputs=pipeline,
    )
    while job.state not in ("done", *_SETTLED_JOB):
        await asyncio.sleep(follow_every)
        job = await asyncio.to_thread(renders.read, job.id)
    if job.state != "done":
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"the render of {reference.slug}, job {job.id}, is {job.state}"
            + (f": {job.error}" if job.error else ""),
            job_id=job.id,
        )
    saved = await create_output(
        {
            "slug": reference.slug,
            "job_id": job.id,
            "index": 0,
            "inputs": reference.inputs,
            "name": name,
            "source": source,
            **({"output_id": saved_as} if saved_as is not None else {}),
        }
    )
    made = RenderedOutput(
        str(saved["id"]), list(saved.get("colors") or []), reference.render_key, job.id
    )
    if item is not None:
        await attach_output(item, made)
    return made


async def attach_output(target: AttachTo, output: RenderedOutput) -> None:
    """The item's ``output_id`` set to the output, with its colour slots when the item
    has none: ``filaments`` on them, over the ``carried`` ones the render has. An item
    with slots keeps them, its revision and its quotes."""
    item = target.item
    slots = colours_of(output.colours)
    rendered = {slot.slot for slot in slots}
    fill = {slot: f for slot, f in (target.carried or {}).items() if slot in rendered}
    fill.update(target.filaments)
    if target.check is not None:
        await asyncio.to_thread(target.check)
    try:
        await asyncio.to_thread(
            lambda: target.lifecycle.update_item(
                target.order.id,
                item.id,
                target.by,
                output_id=output.output_id,
                colours=slots if not item.colours else None,
                filaments=None if item.colours else fill or None,
                expected_revision_id=target.revision_id,
            )
        )
    except LifecycleError as error:
        raise refused(error) from None
