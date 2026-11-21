"""The output a design renders to (#6324, ``orders/rendering.py``): with no order item
(a listing variant priced before anyone orders it) it renders and saves only; with one it
attaches the output with no new revision and no quote dropped. The render and the
outputs are fakes; the rows are Postgres."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.orders.lifecycle import ManualQuoteInput
from scadbuddy.orders.rendering import AttachTo, output_for_reference
from scadbuddy.orders.store import OrderStore
from scadbuddy.render.job_models import Job
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
