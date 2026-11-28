"""The output a design renders to (#6324, ``orders/rendering.py``): with no order item
(a listing variant priced before anyone orders it) it renders and saves only; with one it
attaches the output with no new revision and no quote dropped. The render and the
outputs are fakes; the rows are Postgres."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, storefront_output_id
from scadbuddy.orders.lifecycle import ManualQuoteInput
from scadbuddy.orders.quote import ensure_output
from scadbuddy.orders.rendering import (
    AttachTo,
    RenderedOutput,
    kit_count,
    kit_output_id,
    kit_output_ids,
    later_output_id,
    output_for_reference,
    outputs_for_reference,
)
from scadbuddy.orders.store import OrderStore
from scadbuddy.render.job_models import Job, JobNotFoundError, PipelineOutput
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_quote import BY, NOW, FakeRenders, SavedOutputs, Shop

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


class FailedRenders(FakeRenders):
    """A render that fails, as openscad's refusal fails one."""

    async def submit(
        self,
        slug: str,
        params: Mapping[str, Any],
        *,
        model_version: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        whole_inputs: bool = False,
    ) -> Job:
        self.submitted.append((slug, model_version))
        return Job(id="job-9", slug=slug, state="running", created_at=NOW)

    def read(self, job_id: str) -> Job:
        return Job(
            id=job_id, slug="demo", state="failed", created_at=NOW, error="width is too wide"
        )


def reference_of(shop: Shop) -> Any:
    reference = shop.get_item().reference
    assert reference is not None
    return reference


async def test_a_reference_renders_and_saves_with_no_item(shop: Shop) -> None:
    outputs = SavedOutputs()
    renders = FakeRenders()
    before = shop.get_item()
    made = await output_for_reference(
        reference_of(shop),
        outputs=outputs,
        renders=renders,
        create_output=outputs.create,
        name="Listing variant",
        follow_every=0.0,
    )
    assert renders.submitted == [("demo", "a" * 40)]
    assert outputs.created == [made.output_id]
    assert made.colours == ["#ff0000", "#00ff00"]
    assert (made.render_key, made.job_id) == ("c" * 64, "job-1")
    # No item named: nothing written on any.
    assert shop.get_item() == before


async def test_a_new_output_is_the_back_offices_unless_the_caller_says(shop: Shop) -> None:
    """#6371: an order's render and a quote save with the default, the back office's;
    only the storefront slice run passes ``storefront``."""
    outputs = SavedOutputs()
    bodies: list[dict[str, Any]] = []

    async def create(body: dict[str, Any]) -> dict[str, Any]:
        bodies.append(body)
        return await outputs.create(body)

    await output_for_reference(
        reference_of(shop),
        outputs=outputs,
        renders=FakeRenders(),
        create_output=create,
        name="Variant",
        follow_every=0.0,
    )
    await output_for_reference(
        reference_of(shop),
        outputs=outputs,
        renders=FakeRenders(),
        create_output=create,
        name="Variant",
        follow_every=0.0,
        source="storefront",
    )
    assert [body["source"] for body in bodies] == ["back_office", "storefront"]


async def test_an_output_saved_under_the_id_is_reused_with_no_render(shop: Shop) -> None:
    outputs = SavedOutputs()
    outputs.metas["kept"] = OutputMeta.model_construct(
        id="kept", slug="demo", model_version="a" * 40, colors=["#ff0000"], job_id="job-0"
    )
    renders = FakeRenders()
    found = await output_for_reference(
        reference_of(shop),
        outputs=outputs,
        renders=renders,
        create_output=outputs.create,
        name="Listing variant",
        saved_as="kept",
        follow_every=0.0,
    )
    assert (found.output_id, found.job_id) == ("kept", "job-0")
    assert renders.submitted == [] and outputs.created == []


async def test_a_failed_render_names_its_job_and_error(shop: Shop) -> None:
    outputs = SavedOutputs()
    with pytest.raises(ApiError) as raised:
        await output_for_reference(
            reference_of(shop),
            outputs=outputs,
            renders=FailedRenders(),
            create_output=outputs.create,
            name="Listing variant",
            follow_every=0.0,
        )
    assert raised.value.status == 409
    assert "job-9" in raised.value.detail and "width is too wide" in raised.value.detail
    assert outputs.created == []


async def test_an_item_with_slots_takes_the_output_and_keeps_its_revision_and_quotes(
    shop: Shop,
) -> None:
    """An item with colour slots and no render (a storefront item): the render of its own
    reference is set on it with no new revision, and its quotes stay."""
    item = shop.lifecycle.ensure_revision(shop.order.id, shop.item.id, BY)
    shop.lifecycle.record_manual_quote(
        shop.order.id, item.id, ManualQuoteInput(unit_price_minor=1200, label="typed"), BY
    )
    revisions = shop.rows.revisions(item.id)
    quotes = shop.rows.quotes(item.id)
    outputs = SavedOutputs()
    made = await output_for_reference(
        reference_of(shop),
        outputs=outputs,
        renders=FakeRenders(),
        create_output=outputs.create,
        name="ORD 1 Demo",
        follow_every=0.0,
        item=AttachTo(
            lifecycle=shop.lifecycle,
            order=shop.get_order(),
            item=shop.get_item(),
            by=BY,
            revision_id=item.current_revision_id,
        ),
    )
    after = shop.get_item()
    assert after.output_id == made.output_id
    assert after.current_revision_id == item.current_revision_id
    assert shop.rows.revisions(item.id) == revisions
    assert shop.rows.quotes(item.id) == quotes
    assert after.reference == item.reference and after.colours == item.colours


async def test_a_refusing_check_writes_nothing_on_the_item(shop: Shop) -> None:
    """``AttachTo.check`` runs after the render and before the write: a refusal (the
    order moved on while it rendered) leaves the item's output unset."""
    outputs = SavedOutputs()

    def moved_on() -> None:
        raise ApiError(409, "order ORD-1 is accepted")

    with pytest.raises(ApiError):
        await output_for_reference(
            reference_of(shop),
            outputs=outputs,
            renders=FakeRenders(),
            create_output=outputs.create,
            name="ORD 1 Demo",
            follow_every=0.0,
            item=AttachTo(
                lifecycle=shop.lifecycle,
                order=shop.get_order(),
                item=shop.get_item(),
                by=BY,
                check=moved_on,
            ),
        )
    assert shop.get_item().output_id is None


async def test_a_refusing_check_writes_nothing_on_a_retry_that_finds_its_output(
    shop: Shop,
) -> None:
    """The retry's path (an output saved under ``saved_as`` by an earlier attempt, no render)
    runs ``AttachTo.check`` before the write too."""
    outputs = SavedOutputs()
    outputs.metas["kept"] = OutputMeta.model_construct(
        id="kept", slug="demo", model_version="a" * 40, colors=["#ff0000"], job_id="job-0"
    )
    renders = FakeRenders()

    def moved_on() -> None:
        raise ApiError(409, "order ORD-1 is accepted")

    with pytest.raises(ApiError):
        await output_for_reference(
            reference_of(shop),
            outputs=outputs,
            renders=renders,
            create_output=outputs.create,
            name="ORD 1 Demo",
            saved_as="kept",
            follow_every=0.0,
            item=AttachTo(
                lifecycle=shop.lifecycle,
                order=shop.get_order(),
                item=shop.get_item(),
                by=BY,
                check=moved_on,
            ),
        )
    assert renders.submitted == []
    assert shop.get_item().output_id is None


# -- every output of a render (#5701, spec 2026-11-17 §7.4) --------------------------------


class SeveralRenders(FakeRenders):
    """A render that makes ``count`` outputs. Each submit is a new job (``job-1``,
    ``job-2``, ...) unless one is still there; ``prune`` takes every job made so far."""

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count
        self.jobs: list[str] = []
        self.gone: set[str] = set()

    async def submit(
        self,
        slug: str,
        params: Mapping[str, Any],
        *,
        model_version: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        whole_inputs: bool = False,
    ) -> Job:
        self.submitted.append((slug, model_version))
        live = [job for job in self.jobs if job not in self.gone]
        if not live:
            self.jobs.append(f"job-{len(self.jobs) + 1}")
            live = self.jobs[-1:]
        return Job(id=live[0], slug=slug, state="done", created_at=NOW)

    def read(self, job_id: str) -> Job:
        if job_id in self.gone:
            raise JobNotFoundError(job_id)
        outputs = [PipelineOutput.model_construct()] * self.count
        return super().read(job_id).model_copy(update={"outputs": outputs})

    def prune(self) -> None:
        self.gone.update(self.jobs)


async def every_output(
    shop: Shop, outputs: SavedOutputs, renders: SeveralRenders
) -> list[RenderedOutput]:
    reference = reference_of(shop)
    return await outputs_for_reference(
        reference,
        outputs=outputs,
        renders=renders,
        create_output=outputs.create,
        name="Storefront: Demo",
        saved_as=lambda index: storefront_output_id(reference.render_key, index),
        source="storefront",
        follow_every=0.0,
    )


async def test_every_output_of_a_render_is_saved_once(shop: Shop) -> None:
    outputs = SavedOutputs()
    renders = SeveralRenders(3)
    made = await every_output(shop, outputs, renders)
    key = reference_of(shop).render_key
    ids = [storefront_output_id(key, index) for index in range(3)]
    # The first output's id is the one it always had, pinned.
    assert ids[0] == storefront_output_id("c" * 64) == "006aa087f2c45e6eab7910206b45b336"
    assert len(set(ids)) == 3
    assert [one.output_id for one in made] == outputs.created == ids
    assert {one.job_id for one in made} == {"job-1"}
    assert len(renders.submitted) == 1
    # A second call finds each one, with no new render and no new save.
    assert await every_output(shop, outputs, renders) == made
    assert outputs.created == ids and len(renders.submitted) == 1


@pytest.mark.parametrize("count", [0, 1])
async def test_a_render_of_one_output_is_the_one_output(shop: Shop, count: int) -> None:
    outputs = SavedOutputs()
    [made] = await every_output(shop, outputs, SeveralRenders(count))
    key = reference_of(shop).render_key
    assert (made.output_id, made.job_id) == (storefront_output_id(key), "job-1")
    assert outputs.created == [made.output_id]


async def test_an_attempt_that_stopped_part_way_saves_only_what_is_missing(shop: Shop) -> None:
    outputs = SavedOutputs()
    renders = SeveralRenders(3)
    made = await every_output(shop, outputs, renders)
    del outputs.metas[made[2].output_id]
    outputs.created.clear()
    assert await every_output(shop, outputs, renders) == made
    assert outputs.created == [made[2].output_id] and len(renders.submitted) == 1


@pytest.mark.parametrize("count", [1, 2])
async def test_the_outputs_of_a_pruned_job_are_rendered_again(shop: Shop, count: int) -> None:
    """#7655: once the prune took the job its outputs were saved from, the reference
    renders again, and every output is the new job's, saved under ids of their own, so
    the caller's check of the job (`SliceRun` ``rendered_sha256``) can read it. Before,
    the old outputs came back, the check read no job, and the price failed for good."""
    outputs = SavedOutputs()
    renders = SeveralRenders(count)
    made = await every_output(shop, outputs, renders)
    renders.prune()
    again = await every_output(shop, outputs, renders)
    assert len(again) == count and {one.job_id for one in again} == {"job-2"}
    assert not {one.output_id for one in again} & {one.output_id for one in made}
    assert len(renders.submitted) == 2
    # Another attempt finds the new job's outputs under the same ids: nothing saved again.
    saved = list(outputs.created)
    assert await every_output(shop, outputs, renders) == again
    assert outputs.created == saved


async def test_a_later_output_of_another_job_is_not_part_of_the_render(shop: Shop) -> None:
    """A later output whose id holds another job's output (the first was rendered again
    by another caller) is saved for this job under the id for it, once."""
    outputs = SavedOutputs()
    renders = SeveralRenders(2)
    made = await every_output(shop, outputs, renders)
    outputs.metas[made[1].output_id] = outputs.metas[made[1].output_id].model_copy(
        update={"job_id": "job-9"}
    )
    again = await every_output(shop, outputs, renders)
    assert again[0] == made[0] and again[1].job_id == "job-1"
    assert again[1].output_id != made[1].output_id
    saved = list(outputs.created)
    assert await every_output(shop, outputs, renders) == again
    assert outputs.created == saved


async def test_a_back_office_reuse_does_not_ask_for_the_job(shop: Shop) -> None:
    """Only a caller that reads the job (`needs_job`) checks it: a caller of
    `output_for_reference` alone keeps reusing a matching output whose job is gone. The
    order's render and its slice quote take every output (`outputs_for_reference`) and so
    read the job (#5701 PR 5)."""
    outputs = SavedOutputs()
    outputs.metas["kept"] = OutputMeta.model_construct(
        id="kept", slug="demo", model_version="a" * 40, colors=["#ff0000"], job_id="job-0"
    )
    renders = SeveralRenders(1)
    renders.gone.add("job-0")
    found = await output_for_reference(
        reference_of(shop),
        outputs=outputs,
        renders=renders,
        create_output=outputs.create,
        name="Listing variant",
        saved_as="kept",
        follow_every=0.0,
    )
    assert (found.output_id, found.job_id) == ("kept", "job-0")
    assert renders.submitted == []


# -- the order's render of a kit (#5701 PR 5, spec 2026-11-17 §7.4 "The order") ----------


async def order_render(shop: Shop, outputs: SavedOutputs, renders: SeveralRenders) -> list[str]:
    """The item's render as ``order_item_render`` runs it: output 0 set on the item, each
    later output saved under an id of the first's (`kit_output_id`)."""
    made = await outputs_for_reference(
        reference_of(shop),
        outputs=outputs,
        renders=renders,
        create_output=outputs.create,
        name="ORD 1 Demo",
        saved_as=lambda index: "op-out",
        later_as=kit_output_id,
        follow_every=0.0,
        item=AttachTo(
            lifecycle=shop.lifecycle, order=shop.get_order(), item=shop.get_item(), by=BY
        ),
    )
    return [one.output_id for one in made]


async def test_an_items_render_attaches_every_output_by_its_job(shop: Shop) -> None:
    outputs = SavedOutputs()
    renders = SeveralRenders(3)
    ids = await order_render(shop, outputs, renders)
    assert ids == ["op-out", kit_output_id("op-out", 1), kit_output_id("op-out", 2)]
    assert len(set(ids)) == 3 and outputs.created == ids
    assert {outputs.get(one).job_id for one in ids} == {"job-1"}
    # The item holds the first; the rest are found from it, by its job.
    assert shop.get_item().output_id == "op-out"
    assert kit_output_ids(outputs, "op-out") == ids
    # The render again reuses each one: no new render, no new save.
    assert await order_render(shop, outputs, renders) == ids
    assert outputs.created == ids and len(renders.submitted) == 1


async def test_a_later_output_is_found_from_the_items_own_output(shop: Shop) -> None:
    """An item that holds an output already (an earlier operation's) keeps it, and the
    later outputs go under ids of that output, where the reader finds them."""
    outputs = SavedOutputs()
    outputs.metas["own"] = OutputMeta.model_construct(
        id="own", slug="demo", model_version="a" * 40, colors=["#ff0000"], job_id="job-1"
    )
    shop.lifecycle.update_item(shop.order.id, shop.item.id, BY, output_id="own")
    ids = await order_render(shop, outputs, SeveralRenders(2))
    assert ids == ["own", kit_output_id("own", 1)]
    assert kit_output_ids(outputs, "own") == ids


async def test_a_storefront_kit_is_found_from_its_first_output(shop: Shop) -> None:
    """#8481: an order line holds the storefront slice run's first output; the later ones
    are its `storefront_output_id`s, found by the line's render key."""
    outputs = SavedOutputs()
    made = await every_output(shop, outputs, SeveralRenders(3))
    key = reference_of(shop).render_key
    ids = [one.output_id for one in made]
    assert kit_output_ids(outputs, ids[0], key) == ids
    # Without the key, or for another key's output, no storefront id is looked for.
    assert kit_output_ids(outputs, ids[0]) == [ids[0]]
    assert later_output_id(key)("other", 1) == kit_output_id("other", 1)


@pytest.mark.parametrize("count", [0, 1])
async def test_an_output_of_one_render_is_no_kit(shop: Shop, count: int) -> None:
    outputs = SavedOutputs()
    assert await order_render(shop, outputs, SeveralRenders(count)) == ["op-out"]
    assert kit_output_ids(outputs, "op-out") == ["op-out"]


async def test_an_output_of_another_job_is_not_part_of_the_kit(shop: Shop) -> None:
    outputs = SavedOutputs()
    ids = await order_render(shop, outputs, SeveralRenders(3))
    later = outputs.metas[ids[1]]
    outputs.metas[ids[1]] = later.model_copy(update={"job_id": "job-2"})
    assert kit_output_ids(outputs, "op-out") == ["op-out"]
    assert kit_output_ids(outputs, "gone") == ["gone"]


async def test_an_items_render_after_the_prune_saves_every_output_again(shop: Shop) -> None:
    """The item's own output is reused only while its job can be read: once the prune took
    it, the item gets the new job's first output and every later one under its ids."""
    outputs = SavedOutputs()
    renders = SeveralRenders(2)
    before = await order_render(shop, outputs, renders)
    renders.prune()
    after = await order_render(shop, outputs, renders)
    assert after[0] != before[0] and after[1] == kit_output_id(after[0], 1)
    assert shop.get_item().output_id == after[0]
    assert {outputs.get(one).job_id for one in after} == {"job-2"}
    assert kit_output_ids(outputs, after[0]) == after


async def test_a_slice_quotes_render_saves_every_output_too(shop: Shop) -> None:
    """Review of #7790: the slice quote (`ensure_output`) renders an item with no output as
    the item's render does, so the order knows a kit whichever of the two rendered it."""
    outputs = SavedOutputs()
    first = await ensure_output(
        lifecycle=shop.lifecycle,
        outputs=outputs,
        renders=SeveralRenders(2),
        create_output=outputs.create,
        order=shop.get_order(),
        item=shop.get_item(),
        filaments={},
        by=BY,
        follow_every=0.0,
        saved_as="quote-out",
    )
    assert first == "quote-out" == shop.get_item().output_id
    assert kit_output_ids(outputs, first) == ["quote-out", kit_output_id("quote-out", 1)]


def test_a_kit_is_counted_from_its_job_when_its_outputs_are_not_saved() -> None:
    """An output set on the item by another path saves none of the others: its job still
    lists them, until the prune takes it."""
    outputs = SavedOutputs()
    outputs.metas["picked"] = OutputMeta.model_construct(id="picked", slug="demo", job_id="job-1")
    renders = SeveralRenders(3)
    assert kit_count(outputs, renders, "picked", 1) == 3
    assert kit_count(outputs, None, "picked", 1) == 1
    renders.gone.add("job-1")
    assert kit_count(outputs, renders, "picked", 1) == 1
    assert kit_count(outputs, renders, "gone", 1) == 1


def test_a_kit_whose_saves_stopped_part_way_is_counted_from_its_job() -> None:
    """Review of #7876: the render saves the later outputs one at a time after it
    attaches the first, so a save that failed part way leaves two of three saved. The
    job still says three, so the order page and the print say the third is not saved."""
    outputs = SavedOutputs()
    outputs.metas["first"] = OutputMeta.model_construct(id="first", slug="demo", job_id="job-1")
    assert kit_count(outputs, SeveralRenders(3), "first", 2) == 3
