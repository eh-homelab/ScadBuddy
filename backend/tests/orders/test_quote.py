"""A slice quote's steps (#4250, spec 2026-10-18 §5.2; plan Rulings 2, 3, 6-8): the
check on the rows alone, the run's writes in their order, each plate read from the sliced
file, the file kept by a ref taken before the quote's row. The slicer and the render are
fakes; the rows are Postgres."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.bambuddy.preview import PreviewStarted
from scadbuddy.core.events import InProcessEventBus
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputNotFoundError
from scadbuddy.orders.lifecycle import QUOTE_HOLDER, Lifecycle, ManualQuoteInput
from scadbuddy.orders.models import (
    Actor,
    AddressInput,
    ColourSlot,
    FilamentInput,
    FilamentPatch,
    MachineRateBreakdown,
    Order,
    OrderItem,
    PricingPolicy,
    Quote,
    Reference,
)
from scadbuddy.orders.operations import preview_request
from scadbuddy.orders.problems import PROBLEMS
from scadbuddy.orders.quote import (
    QuoteError,
    QuoteRun,
    SlicedPlate,
    SliceQuoteRequest,
    ensure_output,
    operation_output_id,
    output_matches,
    quote_check,
    quote_run,
    read_plate,
    reference_of,
    support_check,
    unsliced_slots,
)
from scadbuddy.orders.reference import ResolvedReference
from scadbuddy.orders.store import OrderStore
from scadbuddy.render.job_models import Job
from scadbuddy.render.schema import CustomizerSchema, Parameter
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.support.sliced3mf import sliced_3mf

pytestmark = pytest.mark.requires_postgres

BY = Actor(kind="browser")
NOW = datetime(2026, 10, 22, tzinfo=UTC)
INPUTS = {"params": {"width": 10}, "v": 0}
#: Two plates: 3600 s with 12.5 g and 3 g, 1800 s with 10 g and 2 g.
TWO_PLATES = sliced_3mf(
    plates={1: (3600, [12.5, 3.0]), 2: (1800, [10.0, 2.0])},
    gcode={1: b"M620 S0A\n", 2: b"M620 S1A\n"},
)
POLICY = PricingPolicy(machine_rate_per_hour="1.5", setup_fee="0.5")
DIALOG: dict[str, Any] = {
    "printer_id": 1,
    "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}, {"slot_id": 2, "spool_id": 5}]},
    "choices": {"nozzles": [{"size": "0.4"}]},
}


def sliced_plate(plate: int, seconds: int, grams: list[float]) -> bytes:
    return sliced_3mf(plates={plate: (seconds, grams)}, gcode={plate: b"M620 S0A\n"})


class Shop:
    def __init__(self, rows: OrderStore, refs: BlobRefs) -> None:
        self.rows = rows
        self.refs = refs
        self.lifecycle = Lifecycle(rows, InProcessEventBus(), refs)
        customer = rows.insert_customer(
            display_name="Ada",
            company=None,
            notes="",
            tags=[],
            contacts=[],
            addresses=[AddressInput(recipient="Ada", default_shipping=True)],
        )
        self.order = self.lifecycle.create_order(customer.id, BY, currency="USD")
        self.item = self.lifecycle.add_item(
            self.order.id,
            BY,
            ResolvedReference(
                Reference(
                    slug="demo",
                    commit="a" * 40,
                    inputs=INPUTS,
                    template_name="Demo",
                    render_key="c" * 64,
                ),
                [ColourSlot(slot=0, colour="#ff0000"), ColourSlot(slot=1, colour="#00ff00")],
            ),
            output_id=None,
            quantity=1,
        )
        self.pla = rows.insert_filament(
            FilamentInput(material="PLA", colour="#FF0000", cost_per_kg="20")
        ).id
        self.petg = rows.insert_filament(
            FilamentInput(material="PETG", colour="#00FF00", cost_per_kg="30")
        ).id
        self.free = rows.insert_filament(
            FilamentInput(material="PLA", colour="#FFFFFF", cost_per_kg="0")
        ).id

    def get_item(self) -> OrderItem:
        found = self.rows.item(self.item.id)
        assert found is not None
        return found

    def get_order(self) -> Order:
        found = self.rows.order(self.order.id)
        assert found is not None
        return found

    def request(self, **filaments: str) -> SliceQuoteRequest:
        return SliceQuoteRequest.model_validate(
            {
                **DIALOG,
                "filaments": {
                    slot.removeprefix("s"): filament for slot, filament in filaments.items()
                },
            }
        )

    def holders(self) -> set[str]:
        with self.refs.pool.connection() as conn:
            rows = conn.execute(
                "SELECT holder_id FROM blob_refs WHERE holder_kind = %s", (QUOTE_HOLDER,)
            ).fetchall()
        return {row["holder_id"] for row in rows}


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


class FakeSlicer:
    """Every plate sliced at once; each plate's sliced file is ``files[plate]``."""

    def __init__(self, files: Mapping[int, bytes]) -> None:
        self.files = dict(files)
        self.subjects: list[str] = []

    async def start(
        self, output_id: str, request: SliceQuoteRequest, subject: str
    ) -> list[PreviewStarted]:
        self.subjects.append(subject)
        return [PreviewStarted(job_id=plate, plate_id=plate) for plate in sorted(self.files)]

    async def start_library(
        self, file_id: int, request: SliceQuoteRequest, subject: str
    ) -> list[PreviewStarted]:
        self.subjects.append(f"{subject} file {file_id}")
        return [PreviewStarted(job_id=plate, plate_id=plate) for plate in sorted(self.files)]

    async def poll(self, started: PreviewStarted, subject: str) -> SlicedPlate | None:
        return SlicedPlate(plate_id=started.plate_id, slicer="bambuddy", file_id=started.job_id)

    async def fetch(self, sliced: SlicedPlate) -> bytes:
        return self.files[sliced.plate_id]


class FakeRenders:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str | None]] = []

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
        return Job(id="job-1", slug=slug, state="done", created_at=NOW)

    def read(self, job_id: str) -> Job:
        return Job(id=job_id, slug="demo", state="done", created_at=NOW)


class FakeOutputs:
    def __init__(
        self,
        meta: OutputMeta | None = None,
        inputs: dict[str, Any] | None = None,
        arranged: list[str] | None = None,
    ):
        self.meta = meta
        self.saved_inputs = inputs or {}
        self.arranged = arranged or []

    def get(self, output_id: str) -> OutputMeta:
        if self.meta is None or self.meta.id != output_id:
            raise OutputNotFoundError(output_id)
        return self.meta

    def inputs(self, output_id: str) -> dict[str, Any]:
        return self.saved_inputs

    def arranged_from(self, output_id: str) -> list[str]:
        return self.arranged


class Book:
    def __init__(self, policy: PricingPolicy = POLICY) -> None:
        self.current = policy

    def policy(self) -> tuple[str, PricingPolicy, str]:
        return "USD", self.current, "1.15"


def run_of(shop: Shop, tmp_path: Path, files: Mapping[int, bytes], **extra: Any) -> QuoteRun:
    created: list[dict[str, Any]] = []

    async def create_output(body: dict[str, Any]) -> dict[str, Any]:
        created.append(body)
        return {"id": "out-1", "colors": ["#ff0000", "#00ff00"]}

    return QuoteRun(
        lifecycle=shop.lifecycle,
        book=extra.pop("book", Book()),
        outputs=extra.pop("outputs", FakeOutputs()),
        renders=extra.pop("renders", FakeRenders()),
        create_output=create_output,
        slicer=FakeSlicer(files),
        blobs=LocalBlobStore(tmp_path / "blobs"),
        refs=shop.refs,
        follow_every=0.0,
        machine_rate=extra.pop("machine_rate", None),
    )


# -- each plate read (§5.2 step 3) ------------------------------------------------------------


def test_read_plates_prices_each_slot_from_the_file() -> None:
    """Two plates of one file, each read on its own, in the item's slot numbering."""
    first = read_plate(TWO_PLATES, 1, blob_key="quote-a", sha256="a" * 64)
    second = read_plate(TWO_PLATES, 2)
    assert (first.index, first.print_seconds, first.blob_key) == (1, 3600, "quote-a")
    assert [(f.slot, str(f.used_g)) for f in first.filaments] == [(0, "12.5"), (1, "3.0")]
    assert (second.index, second.print_seconds) == (2, 1800)
    assert [(f.slot, str(f.used_g)) for f in second.filaments] == [(0, "10.0"), (1, "2.0")]


def test_an_unknown_plate_fails_the_quote() -> None:
    with pytest.raises(QuoteError) as unreadable:
        read_plate(b"not a zip", 2)
    assert unreadable.value.plate == 2
    assert "plate 2" in str(unreadable.value)
    with pytest.raises(QuoteError) as missing:
        read_plate(TWO_PLATES, 3)
    assert missing.value.plate == 3
    assert "plate 3" in str(missing.value)


def test_a_slot_at_zero_grams_fails_the_quote() -> None:
    """A slot the slicer reports at 0.0 g (0.01 g, read to the tenth as Bambu Studio's
    ``used_g`` is, `filaments3mf.py`) on the second plate."""
    with pytest.raises(QuoteError) as zero:
        read_plate(sliced_plate(2, 600, [4.0, 0.01]), 2)
    assert (zero.value.plate, zero.value.slot) == (2, 1)
    assert "plate 2" in str(zero.value) and "slot 1" in str(zero.value)


def test_a_slot_no_plate_slices_fails_the_quote(shop: Shop) -> None:
    """A slot at 0 g is left out of the read altogether: one no plate uses was not sliced."""
    plates = [read_plate(sliced_plate(1, 600, [4.0]), 1)]
    with pytest.raises(QuoteError) as unsliced:
        unsliced_slots(plates, shop.item)
    assert unsliced.value.slot == 1


# -- the check: the rows alone, nothing written (Ruling 8) --------------------------------------


def test_the_check_refuses_a_settled_item(shop: Shop) -> None:
    shop.lifecycle.cancel_item(shop.order.id, shop.item.id, BY)
    with pytest.raises(ApiError) as refused:
        quote_check(shop.rows, POLICY, shop.order.id, shop.item.id, shop.request())
    assert refused.value.status == 409
    assert refused.value.type.endswith("/item_not_open")
    with pytest.raises(ApiError) as missing:
        quote_check(shop.rows, POLICY, shop.order.id, "nobody", shop.request())
    assert missing.value.status == 404
    with pytest.raises(ApiError) as gone:
        quote_check(shop.rows, POLICY, "nobody", shop.item.id, shop.request())
    assert gone.value.status == 404


def test_the_check_refuses_a_slice_of_a_revision_no_longer_current(shop: Shop) -> None:
    """#4673 PR 3: a slice made for a revision (`expected_revision_id`, the revision its
    new inputs made) is refused once a newer one exists, before anything is written."""
    first = shop.lifecycle.add_revision(shop.order.id, shop.item.id, BY)
    shop.lifecycle.add_revision(shop.order.id, shop.item.id, BY)
    with pytest.raises(ApiError) as refused:
        quote_check(
            shop.rows,
            POLICY,
            shop.order.id,
            shop.item.id,
            shop.request(s0=shop.pla, s1=shop.petg),
            expected_revision_id=first.id,
        )
    assert (refused.value.status, refused.value.type) == (409, PROBLEMS + "revision_superseded")
    current = shop.rows.item(shop.item.id)
    assert current is not None and current.current_revision_id is not None
    quote_check(
        shop.rows,
        POLICY,
        shop.order.id,
        shop.item.id,
        shop.request(s0=shop.pla, s1=shop.petg),
        expected_revision_id=current.current_revision_id,
    )


def test_the_check_refuses_a_request_with_no_design(shop: Shop) -> None:
    request = shop.lifecycle.add_request_item(shop.order.id, BY, title="A dragon", quantity=1)
    with pytest.raises(ApiError) as refused:
        quote_check(shop.rows, POLICY, shop.order.id, request.id, shop.request())
    assert refused.value.status == 409
    assert refused.value.type == PROBLEMS + "item_has_no_design"


def test_the_check_refuses_an_order_that_is_not_open(shop: Shop) -> None:
    quote = shop.lifecycle.record_manual_quote(
        shop.order.id, shop.item.id, ManualQuoteInput(unit_price_minor=100), BY
    )
    shop.lifecycle.accept_quote(shop.order.id, shop.item.id, quote.id, BY)
    shop.lifecycle.accept_order(shop.order.id, BY)
    with pytest.raises(ApiError) as refused:
        quote_check(
            shop.rows, POLICY, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.pla)
        )
    assert (refused.value.status, refused.value.type.rsplit("/", 1)[-1]) == (409, "order_not_open")
    # #5350: revised after acceptance, the item takes a slice quote of its new design, and a
    # filament changed for it keeps the old accepted quote.
    shop.lifecycle.add_revision(shop.order.id, shop.item.id, BY, after_acceptance=True)
    slots = quote_check(
        shop.rows, POLICY, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.petg)
    )
    assert slots == {0: shop.pla, 1: shop.petg}
    shop.lifecycle.update_item(shop.order.id, shop.item.id, BY, filaments={1: shop.petg})
    assert [q.accepted for q in shop.rows.quotes(shop.item.id)] == [True]


def test_the_check_needs_a_filament_per_slot(shop: Shop) -> None:
    def refused(request: SliceQuoteRequest) -> ApiError:
        with pytest.raises(ApiError) as caught:
            quote_check(shop.rows, POLICY, shop.order.id, shop.item.id, request)
        assert caught.value.status == 422
        return caught.value

    assert "slot 1" in refused(shop.request(s0=shop.pla)).detail
    assert "slot 2" in refused(shop.request(s0=shop.pla, s1=shop.pla, s2=shop.pla)).detail
    assert "nobody" in refused(shop.request(s0=shop.pla, s1="nobody")).detail
    shop.rows.archive_filament(shop.petg)
    assert "archived" in refused(shop.request(s0=shop.pla, s1=shop.petg)).detail
    # The item's own filaments cover a slot the body leaves out.
    shop.lifecycle.update_item(shop.order.id, shop.item.id, BY, filaments={1: shop.pla})
    slots = quote_check(shop.rows, POLICY, shop.order.id, shop.item.id, shop.request(s0=shop.pla))
    assert slots == {0: shop.pla, 1: shop.pla}


def test_the_check_refuses_a_book_that_prices_nothing(shop: Shop) -> None:
    nothing = PricingPolicy(markup_percent="20")
    with pytest.raises(ApiError) as refused:
        quote_check(
            shop.rows,
            nothing,
            shop.order.id,
            shop.item.id,
            shop.request(s0=shop.free, s1=shop.free),
        )
    assert refused.value.status == 422
    assert refused.value.type.endswith("/price_book")
    # A zero rate with a priced filament quotes, and so does a rate with free filament.
    quote_check(
        shop.rows,
        PricingPolicy(),
        shop.order.id,
        shop.item.id,
        shop.request(s0=shop.pla, s1=shop.free),
    )
    quote_check(
        shop.rows,
        PricingPolicy(setup_fee="1"),
        shop.order.id,
        shop.item.id,
        shop.request(s0=shop.free, s1=shop.free),
    )


def test_a_refused_check_leaves_the_item_unchanged(shop: Shop) -> None:
    """Review Focus 6: the check writes nothing, even with new filaments in the body."""
    shop.lifecycle.update_item(
        shop.order.id, shop.item.id, BY, filaments={0: shop.pla, 1: shop.pla}
    )
    quote = shop.lifecycle.record_manual_quote(
        shop.order.id, shop.item.id, ManualQuoteInput(unit_price_minor=100), BY
    )
    shop.lifecycle.accept_quote(shop.order.id, shop.item.id, quote.id, BY)
    before = shop.get_item()
    events = shop.rows.events(shop.order.id)
    with pytest.raises(ApiError):
        quote_check(
            shop.rows,
            PricingPolicy(),
            shop.order.id,
            shop.item.id,
            shop.request(s0=shop.free, s1=shop.free),
        )
    after = shop.get_item()
    assert after == before
    assert after.status == "approved"
    assert [q.accepted for q in shop.rows.quotes(after.id)] == [True]
    assert shop.rows.events(shop.order.id) == events


# -- the run's writes, in order ----------------------------------------------------------------


async def test_ensure_output_reuses_a_matching_output(shop: Shop) -> None:
    item = shop.lifecycle.update_item(shop.order.id, shop.item.id, BY, output_id="out-9")
    meta = OutputMeta.model_construct(id="out-9", slug="demo", model_version="a" * 40)
    renders = FakeRenders()

    async def never(body: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("a matching output is reused, never saved again")

    found = await ensure_output(
        lifecycle=shop.lifecycle,
        outputs=FakeOutputs(meta, INPUTS),
        renders=renders,
        create_output=never,
        order=shop.get_order(),
        item=item,
        filaments={},
        by=BY,
        follow_every=0.0,
    )
    assert found == "out-9"
    assert renders.submitted == []

    # Other inputs are another output: rendered at the reference's commit and saved.
    saved: list[dict[str, Any]] = []

    async def create(body: dict[str, Any]) -> dict[str, Any]:
        saved.append(body)
        return {"id": "out-10", "colors": ["#ff0000", "#00ff00"]}

    found = await ensure_output(
        lifecycle=shop.lifecycle,
        outputs=FakeOutputs(meta, {"params": {"width": 11}, "v": 0}),
        renders=renders,
        create_output=create,
        order=shop.get_order(),
        item=item,
        filaments={},
        by=BY,
        follow_every=0.0,
    )
    assert found == "out-10"
    assert renders.submitted == [("demo", "a" * 40)]
    assert saved[0]["job_id"] == "job-1" and saved[0]["inputs"] == INPUTS
    assert saved[0]["name"] == f"{shop.order.number} Demo"
    assert shop.get_item().output_id == "out-10"


#: The commit's schema the quote reads for `output_matches` (#4774).
SCHEMA = CustomizerSchema(
    parameters=[
        Parameter(name="width", type="number", initial=10),
        Parameter(name="label", type="string", initial="hi"),
    ]
)


async def test_an_output_saved_before_defaults_were_filled_still_matches(shop: Shop) -> None:
    """#4774: an output saved before renders filled defaults records only the params it
    was sent; a reference that names their defaults is the same render, both ways, at
    the commit's schema. Quoting it saves nothing and keeps the item's output."""
    meta = OutputMeta.model_construct(id="out-9", slug="demo", model_version="a" * 40)
    sparse: dict[str, Any] = {"params": {"width": 10}, "v": 0}
    full: dict[str, Any] = {"params": {"width": 10, "label": "hi"}, "v": 0}
    reference = reference_of(shop.item)

    async def never(body: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("a matching output is reused, never saved again")

    for stored, recorded in ((full, sparse), (sparse, full)):
        resolved = ResolvedReference(
            reference.model_copy(update={"inputs": stored}), shop.item.colours
        )
        item = shop.lifecycle.update_item(
            shop.order.id, shop.item.id, BY, reference=resolved, output_id="out-9"
        )
        renders = FakeRenders()
        found = await ensure_output(
            lifecycle=shop.lifecycle,
            outputs=FakeOutputs(meta, recorded),
            renders=renders,
            create_output=never,
            order=shop.get_order(),
            item=item,
            filaments={},
            by=BY,
            follow_every=0.0,
            schema=SCHEMA,
        )
        assert found == "out-9"
        assert renders.submitted == []
        assert shop.get_item().output_id == "out-9"
    # Another value of a default is another render, filled or not.
    assert not output_matches(
        FakeOutputs(meta, {"params": {"width": 10, "label": "bye"}, "v": 0}),
        "out-9",
        shop.get_item(),
        SCHEMA,
    )


async def test_an_output_with_malformed_params_is_compared_exactly(shop: Shop) -> None:
    """A malformed output row's params are not read as none at all: filled, they would
    look like an all-defaults render and match an item that names the defaults."""
    meta = OutputMeta.model_construct(id="out-9", slug="demo", model_version="a" * 40)
    full = {"params": {"width": 10, "label": "hi"}, "v": 0}
    resolved = ResolvedReference(
        reference_of(shop.item).model_copy(update={"inputs": full}), shop.item.colours
    )
    item = shop.lifecycle.update_item(
        shop.order.id, shop.item.id, BY, reference=resolved, output_id="out-9"
    )
    malformed = {"params": "width=10", "v": 0}
    assert not output_matches(FakeOutputs(meta, malformed), "out-9", item, SCHEMA)


async def test_ensure_output_reuses_an_arranged_output(shop: Shop) -> None:
    """#4565: an arranged output records a content hash, not the commit the item pins,
    and no render of the reference remakes the arrangement: it is the one sliced."""
    item = shop.lifecycle.update_item(shop.order.id, shop.item.id, BY, output_id="out-9")
    meta = OutputMeta.model_construct(id="out-9", slug="demo", model_version="sha256:" + "c" * 64)
    renders = FakeRenders()

    async def never(body: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("an arranged output is reused, never rendered again")

    async def ensure(outputs: FakeOutputs) -> str:
        return await ensure_output(
            lifecycle=shop.lifecycle,
            outputs=outputs,
            renders=renders,
            create_output=never,
            order=shop.get_order(),
            item=item,
            filaments={},
            by=BY,
            follow_every=0.0,
        )

    assert await ensure(FakeOutputs(meta, INPUTS, ["library:3410"])) == "out-9"
    assert renders.submitted == []
    # The same hash on an output that was not arranged matches nothing.
    with pytest.raises(AssertionError, match="never rendered again"):
        await ensure(FakeOutputs(meta, INPUTS))


async def test_the_run_writes_the_filaments_first(shop: Shop, tmp_path: Path) -> None:
    files = {1: sliced_plate(1, 3600, [12.5, 3.0]), 2: sliced_plate(2, 1800, [10.0, 2.0])}
    run = run_of(shop, tmp_path, files)
    quote = await quote_run(
        run, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.petg), BY
    )
    kinds = [(e.kind, e.detail.get("repinned")) for e in shop.rows.events(shop.order.id)]
    assert kinds[-3:] == [("item.updated", True), ("item.updated", False), ("quote.recorded", None)]
    item = shop.get_item()
    assert [slot.filament_id for slot in item.colours] == [shop.pla, shop.petg]
    assert item.output_id == "out-1" and item.status == "quoted"
    # Both plates: their grams per slot, their seconds and a setup line each.
    assert [plate["index"] for plate in quote.plates] == [1, 2]
    lines = [(line.kind, line.amount_minor) for line in quote.lines]
    assert lines == [
        ("filament", 45),  # 22.5 g x 20 / 1000 = 0.45
        ("filament", 15),  # 5.0 g x 30 / 1000 = 0.15
        ("machine_time", 225),  # 1.5 h x 1.5
        ("setup", 50),
        ("setup", 50),
    ]
    assert quote.source == "slice"
    assert quote.sliced_blob_key == quote.plates[0]["blob_key"]
    assert quote.sliced_blob_key != quote.plates[1]["blob_key"]
    assert run.blobs.exists(quote.sliced_blob_key or "")
    assert shop.holders() == {quote.id}
    assert isinstance(run.slicer, FakeSlicer)
    assert run.slicer.subjects == [f"order_item:{shop.item.id}"]


#: The print setup a quote records beyond the dialog's choices (#4673, order operations
#: spec §5.1): `PrintRunRequest`'s own tower, supports and sequence.
SETUP: dict[str, Any] = {
    "prime_tower": {"enabled": True, "no_sparse_layers": True},
    "supports": {"enabled": False},
    "print_sequence": "by object",
}


def test_a_slice_quote_takes_the_tower_supports_and_sequence() -> None:
    """#4673: in `PrintRunRequest`'s own shapes, so a bad value is refused as the print
    refuses it."""
    request = SliceQuoteRequest.model_validate({**DIALOG, **SETUP})
    assert request.prime_tower is not None and request.prime_tower.no_sparse_layers is True
    assert request.supports is not None and request.supports.enabled is False
    assert request.print_sequence == "by object"
    for bad in (
        {"print_sequence": "by plate"},
        {"supports": {"enabled": True, "threshold_angle": 91}},
        {"prime_tower": {"no_sparse_layers": True}},
    ):
        with pytest.raises(ValueError):
            SliceQuoteRequest.model_validate({**DIALOG, **bad})


def test_the_quotes_slice_takes_the_tower_supports_and_sequence() -> None:
    """#4673: the background slice's run request (`PreviewSlicer`) carries all three, so
    the quote slices what the print will."""
    request = SliceQuoteRequest.model_validate({**DIALOG, **SETUP})
    body = preview_request(request)
    assert body.prime_tower == request.prime_tower
    assert body.supports == request.supports
    assert body.print_sequence == "by object"
    assert preview_request(SliceQuoteRequest.model_validate(DIALOG)).supports is None


async def test_the_quote_records_its_whole_print_setup(shop: Shop, tmp_path: Path) -> None:
    """#4673 (spec §5.1): the quote keeps the printer, path, choices, tower, supports,
    sequence and spools it sliced with, so `order_get` shows them and the print can
    send them."""
    run = run_of(shop, tmp_path, {1: sliced_plate(1, 3600, [12.5, 3.0])})
    request = SliceQuoteRequest.model_validate(
        {
            **DIALOG,
            **SETUP,
            "printer": "11111111-1111-1111-1111-111111111111",
            "path": "native",
            "filaments": {"0": shop.pla, "1": shop.petg},
        }
    )
    quote = await quote_run(run, shop.order.id, shop.item.id, request, BY)
    stored = shop.rows.quote(quote.id)
    assert stored is not None
    for recorded in (quote, stored):
        assert recorded.choices == {
            "printer_id": 1,
            "printer": "11111111-1111-1111-1111-111111111111",
            "path": "native",
            "choices": request.choices.model_dump(mode="json"),
            "prime_tower": {"enabled": True, "no_sparse_layers": True},
            "supports": {
                "enabled": False,
                "type": None,
                "threshold_angle": None,
                "on_build_plate_only": None,
            },
            "print_sequence": "by object",
        }
        assert recorded.filament_plan == request.filament_plan.model_dump(mode="json")


async def test_a_quote_sliced_without_a_setup_records_none_of_it(
    shop: Shop, tmp_path: Path
) -> None:
    """#4673: the three left out are recorded as left out (the template's), and a manual
    quote, which sliced nothing, records no setup."""
    run = run_of(shop, tmp_path, {1: sliced_plate(1, 3600, [12.5, 3.0])})
    quote = await quote_run(
        run, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.petg), BY
    )
    assert quote.choices is not None
    assert (
        quote.choices["prime_tower"],
        quote.choices["supports"],
        quote.choices["print_sequence"],
    ) == (None, None, None)
    manual = shop.lifecycle.record_manual_quote(
        shop.order.id, shop.item.id, ManualQuoteInput(unit_price_minor=500), BY
    )
    assert (manual.choices, manual.filament_plan) == (None, None)


async def test_the_runs_new_filaments_are_a_revision_the_quote_names(
    shop: Shop, tmp_path: Path
) -> None:
    run = run_of(shop, tmp_path, {1: sliced_plate(1, 3600, [12.5, 3.0])})
    quote = await quote_run(
        run, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.petg), BY
    )
    revisions = shop.rows.revisions(shop.item.id)
    assert [r.number for r in revisions] == [1, 2]
    assert quote.revision_id == revisions[1].id


async def test_a_revision_made_during_the_run_refuses_its_quote(shop: Shop, tmp_path: Path) -> None:
    """Review Focus 5: the slice is of a design a person has since replaced."""
    run = run_of(shop, tmp_path, {1: sliced_plate(1, 3600, [12.5, 3.0])})
    assert isinstance(run.slicer, FakeSlicer)
    fetch = run.slicer.fetch

    async def revising(sliced: SlicedPlate) -> bytes:
        shop.lifecycle.add_revision(shop.order.id, shop.item.id, BY, note="new photos")
        return await fetch(sliced)

    run.slicer.fetch = revising  # type: ignore[method-assign]
    with pytest.raises(ApiError) as refused:
        await quote_run(
            run, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.petg), BY
        )
    assert refused.value.status == 409
    assert refused.value.type.endswith("/revision_not_newest")
    assert shop.rows.quotes(shop.item.id) == []
    assert shop.holders() == set()


async def test_a_revision_made_during_the_render_refuses_its_quote(
    shop: Shop, tmp_path: Path
) -> None:
    """A revision made while the item's render runs: the render is of the old design,
    so the run writes nothing past it and records no quote."""

    class Revising(FakeRenders):
        async def submit(self, *args: Any, **kwargs: Any) -> Job:
            shop.lifecycle.add_revision(shop.order.id, shop.item.id, BY, note="new photos")
            return await super().submit(*args, **kwargs)

    run = run_of(shop, tmp_path, {1: sliced_plate(1, 3600, [12.5, 3.0])}, renders=Revising())
    with pytest.raises(ApiError) as refused:
        await quote_run(
            run, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.petg), BY
        )
    assert refused.value.status == 409
    assert refused.value.type.endswith("/revision_not_newest")
    assert shop.rows.quotes(shop.item.id) == []
    assert shop.holders() == set()
    assert shop.get_item().output_id is None


async def test_an_item_with_no_revision_is_repaired_and_quoted(shop: Shop, tmp_path: Path) -> None:
    """An item a build before revisions wrote has none: the run makes its revision 1
    from the item's copy and quotes it, never a 500."""
    shop.lifecycle.update_item(
        shop.order.id, shop.item.id, BY, filaments={0: shop.pla, 1: shop.petg}
    )
    with shop.rows.transaction() as conn:
        conn.execute(
            "UPDATE order_items SET current_revision_id = NULL WHERE id = %s", (shop.item.id,)
        )
        conn.execute("DELETE FROM order_item_revisions WHERE item_id = %s", (shop.item.id,))
    run = run_of(shop, tmp_path, {1: sliced_plate(1, 3600, [12.5, 3.0])})
    quote = await quote_run(
        run, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.petg), BY
    )
    item = shop.get_item()
    assert item.current_revision_id is not None
    assert quote.revision_id == item.current_revision_id
    kinds = [e.kind for e in shop.rows.events(shop.order.id)]
    assert "revision.repaired" in kinds


async def test_a_recorded_quote_keeps_its_rates(shop: Shop, tmp_path: Path) -> None:
    """Review Focus 2: the book changing afterwards never moves a recorded quote."""
    book = Book()
    run = run_of(shop, tmp_path, {1: sliced_plate(1, 3600, [10.0, 5.0])}, book=book)
    quote = await quote_run(
        run, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.petg), BY
    )
    book.current = PricingPolicy(machine_rate_per_hour="99")
    shop.rows.update_filament(shop.pla, FilamentPatch(cost_per_kg="99"))
    again = shop.rows.quote(quote.id)
    assert again is not None
    assert again.lines == quote.lines and again.unit_price_minor == quote.unit_price_minor
    assert "at 20 per kg" in again.lines[0].label


async def test_the_ref_is_added_before_the_row(
    shop: Shop, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Focus 10: when the row's insert fails, the ref was already there, and the
    run drops it before it raises."""
    seen: list[set[str]] = []

    def failing(*args: Any, **kwargs: Any) -> Any:
        seen.append(shop.holders())
        raise RuntimeError("the insert failed")

    monkeypatch.setattr(shop.rows, "insert_quote", failing)
    run = run_of(shop, tmp_path, {1: sliced_plate(1, 600, [1.0, 1.0])})
    with pytest.raises(RuntimeError):
        await quote_run(
            run, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.petg), BY
        )
    assert len(seen) == 1 and len(seen[0]) == 1
    assert shop.holders() == set()
    assert shop.rows.quotes(shop.item.id) == []


async def test_a_failed_run_drops_its_own_holder(shop: Shop, tmp_path: Path) -> None:
    """A second plate at 0 g fails the quote after the first plate was kept: its holder
    goes, and no quote is recorded."""
    files = {1: sliced_plate(1, 600, [1.0, 1.0]), 2: sliced_plate(2, 600, [1.0, 0.01])}
    run = run_of(shop, tmp_path, files)
    with pytest.raises(ApiError) as failed:
        await quote_run(
            run, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.petg), BY
        )
    assert failed.value.status == 422
    assert failed.value.extensions["plate"] == 2
    assert "plate 2" in failed.value.detail
    assert shop.holders() == set()
    assert shop.rows.quotes(shop.item.id) == []


# -- attempts of one operation that overlap (#5353) -------------------------------------------

#: The quote id every attempt of one operation records under (`operation_quote_id`).
ONE_OPERATION = "5353" * 8


class PausedSlicer(FakeSlicer):
    """Holds the second plate's fetch until ``go``: by then the first plate's file is
    kept and held, so the attempt is between its holds and its record. With ``fail``
    the attempt fails there, as one whose worker stopped does."""

    def __init__(self, files: Mapping[int, bytes]) -> None:
        super().__init__(files)
        self.paused = asyncio.Event()
        self.go = asyncio.Event()
        self.fail = False

    async def fetch(self, sliced: SlicedPlate) -> bytes:
        if sliced.plate_id == 2:
            self.paused.set()
            await self.go.wait()
            if self.fail:
                raise RuntimeError("this attempt's worker stopped")
        return await super().fetch(sliced)


async def paused_attempt(
    shop: Shop, tmp_path: Path, seconds: int = 600, **extra: Any
) -> tuple[PausedSlicer, asyncio.Task[Quote]]:
    """An attempt of the operation, started and run to its pause. Another ``seconds``
    is a re-slice that gave other bytes, so other keys; ``extra`` goes to `run_of`."""
    files = {
        1: sliced_plate(1, seconds, [1.0, 1.0]),
        2: sliced_plate(2, seconds, [1.0, 1.0]),
    }
    run = run_of(shop, tmp_path, files, **extra)
    slicer = PausedSlicer(files)
    run.slicer = slicer
    task = asyncio.create_task(
        quote_run(
            run,
            shop.order.id,
            shop.item.id,
            shop.request(s0=shop.pla, s1=shop.petg),
            BY,
            quote_id=ONE_OPERATION,
        )
    )
    await asyncio.wait_for(slicer.paused.wait(), 30)
    return slicer, task


def held_keys(shop: Shop, quote_id: str) -> set[str]:
    with shop.refs.pool.connection() as conn:
        rows = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (QUOTE_HOLDER, quote_id),
        ).fetchall()
    return {row["key"] for row in rows}


def plate_keys(quote: Quote) -> set[str]:
    return {plate["blob_key"] for plate in quote.plates} | {quote.sliced_blob_key or ""}


async def test_an_attempt_that_fails_before_another_records_leaves_its_quote_held(
    shop: Shop, tmp_path: Path
) -> None:
    """#5353: a heartbeat timeout starts the next attempt while the first still runs.
    One fails after both took their holds and drops them, since nothing is recorded
    yet; the other then records, and its quote's holds commit with its row."""
    first, recording = await paused_attempt(shop, tmp_path)
    second, failing = await paused_attempt(shop, tmp_path)
    assert held_keys(shop, ONE_OPERATION)
    second.fail = True
    second.go.set()
    with pytest.raises(RuntimeError):
        await failing
    assert held_keys(shop, ONE_OPERATION) == set()
    first.go.set()
    quote = await recording
    assert quote.id == ONE_OPERATION
    assert held_keys(shop, ONE_OPERATION) == plate_keys(quote)
    assert [q.id for q in shop.rows.quotes(shop.item.id)] == [ONE_OPERATION]


async def test_attempts_that_end_after_another_recorded_keep_its_holds(
    shop: Shop, tmp_path: Path
) -> None:
    """#5353: three overlapping attempts. One records; one that reaches its record
    afterwards answers with that quote (no second row, no primary-key failure); one that
    fails afterwards leaves the recorded quote's holds where they are."""
    first, recording = await paused_attempt(shop, tmp_path)
    second, late = await paused_attempt(shop, tmp_path)
    third, failing = await paused_attempt(shop, tmp_path)
    first.go.set()
    quote = await recording
    second.go.set()
    assert (await late).id == quote.id == ONE_OPERATION
    third.fail = True
    third.go.set()
    with pytest.raises(RuntimeError):
        await failing
    assert held_keys(shop, ONE_OPERATION) == plate_keys(quote)
    assert [q.id for q in shop.rows.quotes(shop.item.id)] == [ONE_OPERATION]


async def test_an_attempt_whose_reslice_differs_keeps_only_the_recorded_quotes_holds(
    shop: Shop, tmp_path: Path
) -> None:
    """#5415: overlapping attempts whose re-slices gave other bytes hold keys the quote
    another attempt recorded does not name. One that reaches its record afterwards, and
    one that fails afterwards, drop those holds; the recorded quote keeps its own."""

    class Broken(Book):
        def policy(self) -> tuple[str, PricingPolicy, str]:
            raise RuntimeError("this attempt's worker stopped")

    first, recording = await paused_attempt(shop, tmp_path)
    second, late = await paused_attempt(shop, tmp_path, seconds=601)
    # Fails after it kept both plates' files: at the price.
    third, failing = await paused_attempt(shop, tmp_path, seconds=602, book=Broken())
    first.go.set()
    quote = await recording
    second.go.set()
    assert (await late).id == quote.id
    assert held_keys(shop, ONE_OPERATION) == plate_keys(quote)
    third.go.set()
    with pytest.raises(RuntimeError):
        await failing
    assert held_keys(shop, ONE_OPERATION) == plate_keys(quote)
    assert [q.id for q in shop.rows.quotes(shop.item.id)] == [ONE_OPERATION]


# -- an output saved once per operation (#5390) -----------------------------------------------


class SavedOutputs:
    """``OutputsRead`` over the outputs ``create`` saved, each the reference's render."""

    def __init__(self) -> None:
        self.metas: dict[str, OutputMeta] = {}
        self.created: list[str] = []

    def get(self, output_id: str) -> OutputMeta:
        if output_id not in self.metas:
            raise OutputNotFoundError(output_id)
        return self.metas[output_id]

    def inputs(self, output_id: str) -> dict[str, Any]:
        return INPUTS

    def arranged_from(self, output_id: str) -> list[str]:
        return []

    async def create(self, body: dict[str, Any]) -> dict[str, Any]:
        # As `output_create`'s run: an id already there is another save's claim.
        output_id = body.get("output_id") or ""
        if not output_id or output_id in self.metas:
            output_id = f"out-{len(self.created)}"
        colours = ["#ff0000", "#00ff00"]
        self.metas[output_id] = OutputMeta.model_construct(
            id=output_id, slug="demo", model_version="a" * 40, colors=colours
        )
        self.created.append(output_id)
        return {"id": output_id, "colors": colours}


async def test_an_attempt_that_stopped_after_saving_the_output_is_not_saved_twice(
    shop: Shop, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5390: an attempt saves the item's output and stops before the item's
    ``output_id`` write. The next attempt of the operation finds that output by the id
    the operation saves under and sets it on the item, with no second render or save."""
    outputs = SavedOutputs()
    run = run_of(shop, tmp_path, {1: sliced_plate(1, 600, [1.0, 1.0])}, outputs=outputs)
    run.create_output = outputs.create
    renders = run.renders
    assert isinstance(renders, FakeRenders)
    real = shop.lifecycle.update_item

    def stopped(*args: Any, **kwargs: Any) -> OrderItem:
        if kwargs.get("output_id") is not None:
            raise RuntimeError("this attempt's worker stopped")
        return real(*args, **kwargs)

    monkeypatch.setattr(shop.lifecycle, "update_item", stopped)
    request = shop.request(s0=shop.pla, s1=shop.petg)
    with pytest.raises(RuntimeError):
        await quote_run(run, shop.order.id, shop.item.id, request, BY, quote_id=ONE_OPERATION)
    assert shop.get_item().output_id is None
    monkeypatch.setattr(shop.lifecycle, "update_item", real)

    quote = await quote_run(run, shop.order.id, shop.item.id, request, BY, quote_id=ONE_OPERATION)
    saved = operation_output_id(ONE_OPERATION)
    assert outputs.created == [saved]
    assert len(renders.submitted) == 1
    assert shop.get_item().output_id == saved
    assert quote.id == ONE_OPERATION


async def test_an_output_of_the_operations_id_that_does_not_match_is_left_alone(
    shop: Shop,
) -> None:
    """#5390: an output already saved under the operation's id that is not the item's
    render (another reference's) is never overwritten: the new one gets an id of its
    own."""
    outputs = SavedOutputs()
    keyed = operation_output_id(ONE_OPERATION)
    outputs.metas[keyed] = OutputMeta.model_construct(
        id=keyed, slug="other", model_version="a" * 40, colors=[]
    )
    found = await ensure_output(
        lifecycle=shop.lifecycle,
        outputs=outputs,
        renders=FakeRenders(),
        create_output=outputs.create,
        order=shop.get_order(),
        item=shop.get_item(),
        filaments={},
        by=BY,
        follow_every=0.0,
        saved_as=keyed,
    )
    assert found != keyed and outputs.created == [found]
    assert outputs.metas[keyed].slug == "other"
    assert shop.get_item().output_id == found


# -- the machine rate of the printer it sliced for (#4625, ops spec §6.2) ----------------------


class FakeMachineRate:
    def __init__(self, rate: MachineRateBreakdown | None) -> None:
        self.rate = rate
        self.asked: list[tuple[str | None, int | None]] = []

    async def __call__(
        self, printer: str | None, printer_id: int | None
    ) -> MachineRateBreakdown | None:
        self.asked.append((printer, printer_id))
        return self.rate


async def test_a_quote_is_priced_at_its_printers_machine_rate(shop: Shop, tmp_path: Path) -> None:
    rate = MachineRateBreakdown(
        printer="p-1",
        printer_name="H2C",
        source="profile",
        rate_per_hour="0.89",
        currency="USD",
        depreciation_per_hour="0.89",
        intervals="typed",
    )
    machine = FakeMachineRate(rate)
    run = run_of(shop, tmp_path, {1: sliced_plate(1, 7200, [10.0, 5.0])}, machine_rate=machine)
    quote = await quote_run(
        run, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.petg), BY
    )
    assert machine.asked == [(None, 1)]
    line = next(line for line in quote.lines if line.kind == "machine_time")
    assert (line.label, line.amount_minor) == ("Machine time, 2 h at 0.89 per hour", 178)
    assert quote.printer == "p-1"
    assert quote.machine_rate_breakdown == rate
    again = shop.rows.quote(quote.id)
    assert again is not None
    assert (again.printer, again.machine_rate_breakdown) == ("p-1", rate)


async def test_a_quote_for_no_printer_keeps_the_books_rate(shop: Shop, tmp_path: Path) -> None:
    run = run_of(
        shop, tmp_path, {1: sliced_plate(1, 3600, [10.0, 5.0])}, machine_rate=FakeMachineRate(None)
    )
    quote = await quote_run(
        run, shop.order.id, shop.item.id, shop.request(s0=shop.pla, s1=shop.petg), BY
    )
    line = next(line for line in quote.lines if line.kind == "machine_time")
    assert line.label == "Machine time, 1 h at 1.5 per hour"
    assert (quote.printer, quote.machine_rate_breakdown) == (None, None)


# -- support material (ops spec §6.3) ----------------------------------------------------------


async def test_a_support_slot_is_priced_as_its_own_filament_line(
    shop: Shop, tmp_path: Path
) -> None:
    """The slice's third slot is the support interface, not a colour slot of the item:
    priced with the price-book filament the dialog mapped it to, from its grams."""
    files = {1: sliced_plate(1, 3600, [10.0, 5.0, 2.0])}
    request = shop.request(s0=shop.pla, s1=shop.pla).model_copy(
        update={"support_filaments": {2: shop.petg}}
    )
    quote = await quote_run(run_of(shop, tmp_path, files), shop.order.id, shop.item.id, request, BY)
    filament = [(line.label, line.amount_minor) for line in quote.lines if line.kind == "filament"]
    assert filament == [
        ("PLA, 10 g at 20 per kg", 20),
        ("PLA, 5 g at 20 per kg", 10),
        ("Support, PETG, 2 g at 30 per kg", 6),
    ]
    # The support slot is no colour slot of the item.
    assert [slot.slot for slot in shop.get_item().colours] == [0, 1]


async def test_an_unmapped_support_slot_refuses_the_quote(shop: Shop, tmp_path: Path) -> None:
    files = {1: sliced_plate(1, 3600, [10.0, 5.0, 2.0])}
    with pytest.raises(ApiError) as refused:
        await quote_run(
            run_of(shop, tmp_path, files),
            shop.order.id,
            shop.item.id,
            shop.request(s0=shop.pla, s1=shop.pla),
            BY,
        )
    assert refused.value.status == 422
    assert "slot 2" in refused.value.detail


def test_the_support_check_refuses_a_colour_slot_or_an_unknown_filament(shop: Shop) -> None:
    def refused(support: dict[int, str]) -> str:
        request = shop.request(s0=shop.pla, s1=shop.pla).model_copy(
            update={"support_filaments": support}
        )
        with pytest.raises(ApiError) as caught:
            support_check(shop.rows, shop.item.id, request)
        assert caught.value.status == 422
        return caught.value.detail

    assert "colour slot 1" in refused({1: shop.petg})
    assert "nobody" in refused({2: "nobody"})
    shop.rows.archive_filament(shop.petg)
    assert "archived" in refused({2: shop.petg})
    shop.rows.unarchive_filament(shop.petg)
    request = shop.request(s0=shop.pla, s1=shop.pla).model_copy(
        update={"support_filaments": {2: shop.petg}}
    )
    support_check(shop.rows, shop.item.id, request)


async def test_a_support_slot_no_plate_uses_refuses_the_quote(shop: Shop, tmp_path: Path) -> None:
    """A support filament for a slot the slice never used is a mistake in the request,
    never silently left out of the price."""
    files = {1: sliced_plate(1, 3600, [10.0, 5.0])}
    request = shop.request(s0=shop.pla, s1=shop.pla).model_copy(
        update={"support_filaments": {4: shop.petg}}
    )
    with pytest.raises(ApiError) as refused:
        await quote_run(run_of(shop, tmp_path, files), shop.order.id, shop.item.id, request, BY)
    assert refused.value.status == 422
    assert "slot 4" in refused.value.detail


async def test_a_support_filament_archived_since_the_check_refuses_the_quote(
    shop: Shop, tmp_path: Path
) -> None:
    """Archived between the check and the run, as a colour slot's would be refused: never
    priced from a filament the book hides."""
    files = {1: sliced_plate(1, 3600, [10.0, 5.0, 2.0])}
    request = shop.request(s0=shop.pla, s1=shop.pla).model_copy(
        update={"support_filaments": {2: shop.petg}}
    )
    shop.rows.archive_filament(shop.petg)
    with pytest.raises(ApiError) as refused:
        await quote_run(run_of(shop, tmp_path, files), shop.order.id, shop.item.id, request, BY)
    assert refused.value.status == 422
    assert "slot 2" in refused.value.detail


async def test_a_revision_made_after_the_check_refuses_the_slice(
    shop: Shop, tmp_path: Path
) -> None:
    """#4673 PR 3: a slice for one revision (its new inputs') records nothing once another
    is current, whether that one came before the run read the item or while it sliced:
    the record's own check, under the order's lock, catches the second. Both are
    `revision_superseded`."""
    files = {1: sliced_plate(1, 3600, [12.5, 3.0]), 2: sliced_plate(2, 1800, [10.0, 2.0])}
    request = shop.request(s0=shop.pla, s1=shop.petg)
    meant = shop.lifecycle.add_revision(shop.order.id, shop.item.id, BY)

    # Before the run: the revision it reads is not the one it was made for.
    shop.lifecycle.add_revision(shop.order.id, shop.item.id, BY)
    with pytest.raises(ApiError) as before:
        await quote_run(
            run_of(shop, tmp_path, files),
            shop.order.id,
            shop.item.id,
            request,
            BY,
            expected_revision_id=meant.id,
        )
    assert before.value.type == PROBLEMS + "revision_superseded"

    # While it slices: the record refuses it under the order's lock.
    current = shop.get_item().current_revision_id
    assert current is not None
    run = run_of(shop, tmp_path, files)
    record = shop.lifecycle.record_slice_quote

    def revised_first(*args: Any, **kwargs: Any) -> Any:
        shop.lifecycle.add_revision(shop.order.id, shop.item.id, BY)
        return record(*args, **kwargs)

    shop.lifecycle.record_slice_quote = revised_first  # type: ignore[method-assign]
    with pytest.raises(ApiError) as during:
        await quote_run(run, shop.order.id, shop.item.id, request, BY, expected_revision_id=current)
    assert during.value.type == PROBLEMS + "revision_superseded"
    assert shop.rows.quotes(shop.item.id) == []
