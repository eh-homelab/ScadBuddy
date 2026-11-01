"""Printing an order item (#4719, spec 2026-10-18 §5.4): the attempt a print run starts,
the item following the run from what the print path recorded (``print_runs``,
``print_outcomes``), and ``order_item_print``'s check."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scadbuddy.bambuddy.runs import PrintRun
from scadbuddy.core.events import BusResync, PrintEvent, PrintOutcome, PrintRunEvent
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.orders.lifecycle import (
    FULFILMENT_HOLDER,
    QUOTE_HOLDER,
    FulfilmentInput,
    LifecycleError,
)
from scadbuddy.orders.models import Fulfilment, Order, OrderItem
from scadbuddy.orders.printing import (
    listen_for_prints,
    print_check,
    print_item,
    print_request_id,
)
from scadbuddy.orders.quote import OutputsRead
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop, _slice_quote

pytestmark = pytest.mark.requires_postgres

SUBJECT = "out-1"


@pytest.fixture(name="shop")
def _shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def run_row(pool: PgPool, *, status: str = "running", detail: str | None = None) -> str:
    """A ``print_runs`` row as the print route's run writes it."""
    run_id = uuid.uuid4().hex
    error = None if detail is None else f'{{"status": 422, "detail": "{detail}"}}'
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_runs (id, output_id, idempotency_key, status, error)"
            " VALUES (%s, %s, %s, %s, %s::jsonb)",
            (run_id, SUBJECT, run_id, status, error),
        )
    return run_id


def set_run(pool: PgPool, run_id: str, status: str, detail: str | None = None) -> None:
    error = None if detail is None else f'{{"status": 422, "detail": "{detail}"}}'
    with pool.connection() as conn:
        conn.execute(
            "UPDATE print_runs SET status = %s, error = %s::jsonb WHERE id = %s",
            (status, error, run_id),
        )


def settle(pool: PgPool, run_id: str, outcome: str) -> None:
    """``print_outcomes``' row, as the follow of every path records it (#2665)."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO print_outcomes (run_id, subject, slug, outcome) VALUES (%s, %s, %s, %s)",
            (run_id, SUBJECT, "demo", outcome),
        )


def claimed(shop: Shop, order: Order, item: OrderItem, key: str) -> Fulfilment:
    return shop.lifecycle.claim_print(
        order.id,
        item.id,
        BY,
        request_id=key,
        sliced_sha256="a" * 64,
        sliced_blob_key="quote-a",
    )


def started(shop: Shop, order: Order, item: OrderItem, run_id: str) -> Fulfilment:
    """Claimed, then its run attached, as ``print_item`` does."""
    claim = claimed(shop, order, item, f"req-{run_id}")
    return shop.lifecycle.attach_print(claim, run_id, None)


def held(pool: PgPool, kind: str) -> dict[str, str]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT holder_id, key FROM blob_refs WHERE holder_kind = %s", (kind,)
        ).fetchall()
    return {row["holder_id"]: row["key"] for row in rows}


def test_a_print_records_its_attempt_and_queues_the_item(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted(quantity=2)
    run_id = run_row(pool)
    attempt = started(shop, order, item, run_id)
    assert (attempt.attempt, attempt.quantity, attempt.outcome) == (1, 0, "unknown")
    assert (attempt.print_run_id, attempt.copies) == (run_id, 2)
    assert attempt.sliced_blob_key == "quote-a" and attempt.started_at is not None
    assert shop.get_item(item).status == "queued"
    # §3.5's "any item past `approved`".
    assert shop.get(order).status == "in_production"
    assert held(pool, FULFILMENT_HOLDER) == {attempt.id: "quote-a"}
    assert [k for k in shop.kinds(order) if k.startswith("print.")] == [
        "print.claimed",
        "print.started",
    ]
    # A re-sent operation repeats its run, and records nothing twice.
    assert started(shop, order, item, run_id) == attempt
    assert len(shop.rows.fulfilments(item.id)) == 1


def test_a_print_needs_an_approved_item_on_an_accepted_order(shop: Shop, pool: PgPool) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    with pytest.raises(LifecycleError) as refused:
        started(shop, order, item, run_row(pool))
    assert refused.value.slug == "order_not_accepted"
    order, item = shop.accepted()
    started(shop, order, item, run_row(pool))
    with pytest.raises(LifecycleError) as again:
        started(shop, order, item, run_row(pool))
    assert again.value.slug == "item_not_approved"


def test_the_item_follows_its_print_to_printed(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted(quantity=2)
    run_id = run_row(pool)
    started(shop, order, item, run_id)
    assert shop.lifecycle.follow_print(run_id) is None
    # A print moving before its run queued it is not this run's print yet.
    assert shop.lifecycle.follow_print(run_id, progressed=True) is None
    set_run(pool, run_id, "succeeded")
    assert shop.lifecycle.follow_print(run_id) is None
    assert shop.get_item(item).status == "queued"
    shop.lifecycle.follow_print(run_id, progressed=True)
    assert shop.get_item(item).status == "printing"
    settle(pool, run_id, "finished")
    settled = shop.lifecycle.follow_print(run_id)
    assert settled is not None
    assert (settled.outcome, settled.quantity) == ("succeeded", 2)
    assert settled.finished_at is not None
    assert shop.get_item(item).status == "printed"
    # Heard again (another replica, a resync): nothing moves.
    assert shop.lifecycle.follow_print(run_id) is None
    shop.lifecycle.accept_fulfilment(order.id, item.id, settled.id, BY)
    assert shop.get_item(item).status == "fulfilled"
    assert shop.get(order).status == "ready"


def test_a_failed_run_or_print_fails_the_attempt(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted()
    run_id = run_row(pool)
    started(shop, order, item, run_id)
    set_run(pool, run_id, "failed", "no spool for slot 2")
    failed = shop.lifecycle.follow_print(run_id)
    assert failed is not None
    assert (failed.outcome, failed.failure_reason) == ("failed", "no spool for slot 2")
    assert shop.get_item(item).status == "failed"
    # A reprint is a new attempt, from `failed`.
    second = run_row(pool, status="succeeded")
    started(shop, order, item, second)
    assert shop.get_item(item).status == "queued"
    settle(pool, second, "cancelled")
    cancelled = shop.lifecycle.follow_print(second)
    assert cancelled is not None and cancelled.outcome == "failed"
    assert cancelled.failure_reason == "the print was cancelled"
    assert shop.get_item(item).status == "failed"


def test_an_unread_end_waits_for_a_person(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted()
    run_id = run_row(pool, status="succeeded")
    attempt = started(shop, order, item, run_id)
    settle(pool, run_id, "unknown")
    ended = shop.lifecycle.follow_print(run_id)
    assert ended is not None and ended.outcome == "unknown" and ended.finished_at is not None
    assert shop.get_item(item).status == "queued"
    # Settled by hand (§5.4: PATCH the attempt).
    shop.lifecycle.update_fulfilment(
        order.id, item.id, attempt.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    assert shop.get_item(item).status == "printed"


def test_a_persons_edit_stands_over_the_follow(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted()
    run_id = run_row(pool, status="succeeded")
    attempt = started(shop, order, item, run_id)
    shop.lifecycle.update_fulfilment(
        order.id, item.id, attempt.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    settle(pool, run_id, "failed")
    assert shop.lifecycle.follow_print(run_id) is None
    assert shop.get_item(item).status == "printed"


def test_a_cancelled_item_keeps_its_print_record(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted()
    run_id = run_row(pool, status="succeeded")
    started(shop, order, item, run_id)
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    settle(pool, run_id, "finished")
    settled = shop.lifecycle.follow_print(run_id)
    assert settled is not None and settled.outcome == "succeeded"
    assert shop.get_item(item).status == "cancelled"


def test_an_accepted_part_leaves_the_rest_to_print(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted(quantity=3)
    run_id = run_row(pool, status="succeeded")
    attempt = started(shop, order, item, run_id)
    shop.lifecycle.update_fulfilment(
        order.id, item.id, attempt.id, FulfilmentInput(quantity=2, outcome="partial"), BY
    )
    assert shop.get_item(item).status == "printed"
    shop.lifecycle.accept_fulfilment(order.id, item.id, attempt.id, BY)
    assert shop.get_item(item).status == "approved"


def test_the_listener_follows_every_print_event(shop: Shop, pool: PgPool) -> None:
    async def scenario() -> None:
        order, item = shop.accepted()
        run_id = run_row(pool, status="succeeded")
        started(shop, order, item, run_id)
        remove = listen_for_prints(shop.bus, shop.lifecycle)
        try:
            shop.bus.publish(PrintEvent(kind="print.progress", output_id=SUBJECT, slug="demo"))
            await _until(lambda: shop.get_item(item).status == "printing")
            settle(pool, run_id, "finished")
            shop.bus.publish(
                PrintOutcome(output_id=SUBJECT, slug="demo", run_id=run_id, outcome="finished")
            )
            await _until(lambda: shop.get_item(item).status == "printed")

            # A run that fails is heard on `print.run`; a lost event on the resync.
            order2, item2 = shop.accepted()
            failed = run_row(pool)
            started(shop, order2, item2, failed)
            set_run(pool, failed, "failed", "refused")
            shop.bus.publish(PrintRunEvent(output_id=SUBJECT, slug="demo", run_id=failed))
            await _until(lambda: shop.get_item(item2).status == "failed")
            order3, item3 = shop.accepted()
            lost = run_row(pool, status="succeeded")
            started(shop, order3, item3, lost)
            settle(pool, lost, "finished")
            shop.bus.publish(BusResync())
            await _until(lambda: shop.get_item(item3).status == "printed")
        finally:
            await remove()

    asyncio.run(scenario())


async def _until(done: Any, timeout: float = 60.0) -> None:
    """Until ``done`` holds; ``timeout`` only catches a follow that never moves."""
    deadline = time.monotonic() + timeout
    while not await asyncio.to_thread(done):
        assert time.monotonic() < deadline, "the follow never moved the item"
        await asyncio.sleep(0.02)


# -- the check -------------------------------------------------------------------------------


class Outputs:
    """``OutputsRead`` over one output, the reference's render unless told otherwise."""

    def __init__(self, item: OrderItem, *, gone: bool = False) -> None:
        assert item.reference is not None
        self.reference = item.reference
        self.gone = gone

    def get(self, output_id: str) -> OutputMeta:
        if self.gone:
            raise OSError(output_id)
        meta = SimpleNamespace(slug=self.reference.slug, model_version=self.reference.commit)
        return cast(OutputMeta, meta)

    def inputs(self, output_id: str) -> dict[str, Any]:
        return dict(self.reference.inputs)

    def arranged_from(self, output_id: str) -> list[str]:
        return []


def _check(
    shop: Shop, order: Order, item: OrderItem, blobs: LocalBlobStore, outputs: OutputsRead
) -> dict[str, Any]:
    return asyncio.run(print_check(shop.rows, outputs, blobs, order.id, item.id))


def _with_output(shop: Shop, item: OrderItem) -> OrderItem:
    with shop.rows.transaction() as conn:
        return shop.rows.update_item(conn, item.id, output_id=uuid.uuid4().hex)


def _slice_accepted(shop: Shop, blobs: LocalBlobStore, payload: bytes) -> tuple[Order, OrderItem]:
    order = shop.order()
    item = _with_output(shop, shop.item(order, 2))
    quote = _slice_quote(shop, order, item, key="quote-a")
    shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY)
    (blobs.dir_for("quote-a") / "sliced.3mf").write_bytes(payload)
    return shop.lifecycle.accept_order(order.id, BY), shop.get_item(item)


def _refusal(call: Any) -> str:
    with pytest.raises(ApiError) as refused:
        call()
    return str(refused.value.type).rsplit("/", 1)[-1]


def test_the_check_reads_the_quotes_file(shop: Shop, pool: PgPool, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    # `_slice_quote` records the digest "a" * 64: no file reads to it.
    order, item = _slice_accepted(shop, blobs, b"sliced")
    outputs = Outputs(item)
    assert _refusal(lambda: _check(shop, order, item, blobs, outputs)) == "requote_needed"
    with shop.rows.transaction() as conn:
        conn.execute(
            "UPDATE order_item_quotes SET sliced_sha256 = %s WHERE order_item_id = %s",
            (hashlib.sha256(b"sliced").hexdigest(), item.id),
        )
    checked = _check(shop, order, item, blobs, outputs)
    assert (checked["copies"], checked["output_id"]) == (2, item.output_id)
    assert checked["sliced_blob_key"] == "quote-a"
    # The output the item prints is gone: the quote no longer stands for what would print.
    gone = Outputs(item, gone=True)
    assert _refusal(lambda: _check(shop, order, item, blobs, gone)) == "requote_needed"
    # The file the sweep took while the quote was released.
    blobs.remove("quote-a")
    assert _refusal(lambda: _check(shop, order, item, blobs, outputs)) == "requote_needed"
    assert held(pool, QUOTE_HOLDER)


def test_the_check_prints_what_is_left(shop: Shop, pool: PgPool, tmp_path: Path) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    order = shop.order()
    item = _with_output(shop, shop.item(order, 3))
    shop.quoted(order, item)
    outputs = Outputs(item)
    assert _refusal(lambda: _check(shop, order, item, blobs, outputs)) == "order_not_accepted"
    order = shop.lifecycle.accept_order(order.id, BY)
    # A manual quote names no file: the print slices at its start.
    assert _check(shop, order, item, blobs, outputs)["sliced_sha256"] is None
    shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="partial"), BY
    )
    assert _check(shop, order, item, blobs, outputs)["copies"] == 2
    shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=2, outcome="succeeded"), BY
    )
    assert _refusal(lambda: _check(shop, order, item, blobs, outputs)) == "item_made"
    shop.lifecycle.hold(order.id, BY)
    assert _refusal(lambda: _check(shop, order, item, blobs, outputs)) == "order_on_hold"


def test_a_print_request_id_is_the_operations() -> None:
    assert print_request_id("i", "k") == print_request_id("i", "k") != print_request_id("i", "j")
    # One key on two items is two runs.
    assert print_request_id("i", "k") != print_request_id("h", "k")
    assert len(print_request_id("i" * 64, "k" * 500)) <= 128


# -- the run: claimed before it prints (#5149 review) -----------------------------------------


class Starts:
    """The print route's start, counted: each call is a print the printer would make."""

    def __init__(self, pool: PgPool, *, refuse: ApiError | None = None, made: bool = False) -> None:
        self.pool = pool
        self.refuse = refuse
        #: With ``refuse``: the run was made all the same (a 5xx after the start).
        self.made = made
        self.calls: list[int] = []
        self.run: PrintRun | None = None

    async def __call__(self, copies: int) -> PrintRun:
        self.calls.append(copies)
        if self.refuse is None or self.made:
            run_id = run_row(self.pool, status="succeeded")
            self.run = PrintRun.model_validate(
                {
                    "id": run_id,
                    "subject": f"output:{SUBJECT}",
                    "status": "succeeded",
                    "created_at": "2026-11-01T00:00:00Z",
                }
            )
        if self.refuse is not None:
            raise self.refuse
        assert self.run is not None
        return self.run

    async def existing(self, copies: int) -> PrintRun | None:
        """The run the request's start made, as the print route's record finds it."""
        return self.run


def _print(shop: Shop, order: Order, item: OrderItem, start: Starts, key: str) -> Fulfilment:
    return asyncio.run(
        print_item(
            shop.lifecycle,
            start,
            start.existing,
            order.id,
            item.id,
            BY,
            request_id=key,
            sliced_sha256="a" * 64,
            sliced_blob_key="quote-a",
        )
    )


def test_two_prints_of_one_item_start_one_print(shop: Shop, pool: PgPool) -> None:
    """Two tabs, or the browser and the agent: two keys, one print."""
    order, item = shop.accepted(quantity=2)
    start = Starts(pool)
    barrier = threading.Barrier(2)
    answers: list[Fulfilment | ApiError] = []

    def press(key: str) -> None:
        barrier.wait()
        try:
            answers.append(_print(shop, order, item, start, key))
        except ApiError as error:
            answers.append(error)

    threads = [threading.Thread(target=press, args=(key,)) for key in ("tab-1", "tab-2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert start.calls == [2]
    [attempt] = [a for a in answers if isinstance(a, Fulfilment)]
    [refusal] = [a for a in answers if isinstance(a, ApiError)]
    assert str(refusal.type).endswith("/item_not_approved")
    assert attempt.print_run_id is not None
    assert [f.id for f in shop.rows.fulfilments(item.id)] == [attempt.id]
    # The winning key again repeats its claim and its run.
    assert attempt.print_request_id is not None
    again = _print(shop, order, item, start, attempt.print_request_id)
    assert again.id == attempt.id and again.print_run_id == attempt.print_run_id
    assert start.calls == [2]


def test_a_hold_between_the_check_and_the_run_prints_nothing(
    shop: Shop, pool: PgPool, tmp_path: Path
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    order = shop.order()
    item = _with_output(shop, shop.item(order, 2))
    shop.quoted(order, item)
    order = shop.lifecycle.accept_order(order.id, BY)
    _check(shop, order, item, blobs, Outputs(item))
    shop.lifecycle.hold(order.id, BY)
    start = Starts(pool)
    assert _refusal(lambda: _print(shop, order, item, start, "k")) == "order_on_hold"
    assert start.calls == []
    assert shop.rows.fulfilments(item.id) == []
    assert shop.get_item(item).status == "approved"


def test_a_refused_start_releases_the_claim(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted()
    refusal = ApiError(422, "no spool for slot 2")
    with pytest.raises(ApiError):
        _print(shop, order, item, Starts(pool, refuse=refusal), "k")
    assert shop.rows.fulfilments(item.id) == []
    assert held(pool, FULFILMENT_HOLDER) == {}
    assert shop.get_item(item).status == "approved"
    assert "print.refused" in shop.kinds(order)
    # Released from a failed item, it is failed again.
    run_id = run_row(pool, status="failed", detail="jammed")
    started(shop, order, item, run_id)
    shop.lifecycle.follow_print(run_id)
    assert shop.get_item(item).status == "failed"
    with pytest.raises(ApiError):
        _print(shop, order, item, Starts(pool, refuse=refusal), "k2")
    assert shop.get_item(item).status == "failed"


def test_a_start_that_may_still_run_keeps_its_claim_for_the_retry(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted()
    with pytest.raises(ApiError):
        _print(shop, order, item, Starts(pool, refuse=ApiError(503, "Temporal is busy")), "k")
    assert shop.get_item(item).status == "queued"
    retried = _print(shop, order, item, Starts(pool), "k")
    assert retried.print_run_id is not None
    assert len(shop.rows.fulfilments(item.id)) == 1


def test_progress_of_a_newer_print_of_the_output_is_not_the_items(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted()
    run_id = run_row(pool, status="succeeded")
    started(shop, order, item, run_id)
    # The dialog printed the same output since.
    with pool.connection() as conn:
        conn.execute(
            "UPDATE print_runs SET created_at = now() - interval '1 minute' WHERE id = %s",
            (run_id,),
        )
    run_row(pool, status="succeeded")
    assert shop.lifecycle.follow_print(run_id, progressed=True) is None
    assert shop.get_item(item).status == "queued"


def test_a_claims_key_on_another_item_is_refused(shop: Shop, pool: PgPool) -> None:
    order = shop.order()
    item, other = shop.item(order), shop.item(order)
    shop.quoted(order, item)
    shop.quoted(order, other)
    order = shop.lifecycle.accept_order(order.id, BY)
    claim = claimed(shop, order, item, "k")
    with pytest.raises(LifecycleError) as refused:
        claimed(shop, order, other, "k")
    assert refused.value.slug == "print_request_conflict"
    assert shop.rows.fulfilments(other.id) == []
    # Nor does another run take a claim's run over.
    run_id = run_row(pool)
    shop.lifecycle.attach_print(claim, run_id, None)
    with pytest.raises(LifecycleError) as taken:
        shop.lifecycle.attach_print(claim, run_row(pool), None)
    assert taken.value.slug == "print_request_conflict"
    assert shop.rows.fulfilments(item.id)[0].print_run_id == run_id


def test_a_hold_between_the_claim_and_its_retry_prints_nothing(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted()
    with pytest.raises(ApiError):
        _print(shop, order, item, Starts(pool, refuse=ApiError(503, "Temporal is busy")), "k")
    shop.lifecycle.hold(order.id, BY)
    start = Starts(pool)
    assert _refusal(lambda: _print(shop, order, item, start, "k")) == "order_on_hold"
    assert start.calls == []
    assert shop.rows.fulfilments(item.id) == []
    assert held(pool, FULFILMENT_HOLDER) == {}
    assert shop.get_item(item).status == "approved"


def test_a_cancel_between_the_claim_and_its_retry_prints_nothing(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted(quantity=2)
    with pytest.raises(ApiError):
        _print(shop, order, item, Starts(pool, refuse=ApiError(503, "Temporal is busy")), "k")
    shop.lifecycle.cancel_item(order.id, item.id, BY)
    start = Starts(pool)
    assert _refusal(lambda: _print(shop, order, item, start, "k")) == "item_not_open"
    assert start.calls == []
    assert shop.rows.fulfilments(item.id) == []
    assert shop.get_item(item).status == "cancelled"


def running_print(pool: PgPool, item: OrderItem) -> None:
    """``order_item_print``'s operation, still running for the item."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO operations (id, kind, subject, operation_key, status, request,"
            " workflow_id, workflow_run_id)"
            " VALUES (%s, 'order_item_print', %s, %s, 'running', '{}'::jsonb, %s, 'r')",
            (uuid.uuid4().hex, f"order_item:{item.id}", uuid.uuid4().hex, uuid.uuid4().hex),
        )


def test_a_pending_claim_keeps_the_item_queued(shop: Shop, pool: PgPool) -> None:
    order, item = shop.accepted(quantity=2)
    claim = claimed(shop, order, item, "k")
    running_print(pool, item)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.update_fulfilment(
            order.id, item.id, claim.id, FulfilmentInput(quantity=0, outcome="unknown"), BY
        )
    assert refused.value.slug == "print_starting"
    assert shop.get_item(item).status == "queued"
    with pytest.raises(LifecycleError) as again:
        claimed(shop, order, item, "k2")
    assert again.value.slug == "item_not_approved"


def test_a_claim_whose_start_ended_is_settled_by_hand(shop: Shop, pool: PgPool) -> None:
    """The start never answered and its operation gave up: a person settles it."""
    order, item = shop.accepted()
    claim = claimed(shop, order, item, "k")
    shop.lifecycle.update_fulfilment(
        order.id, item.id, claim.id, FulfilmentInput(quantity=0, outcome="unknown"), BY
    )
    assert shop.get_item(item).status == "queued"
    shop.lifecycle.update_fulfilment(
        order.id, item.id, claim.id, FulfilmentInput(quantity=0, outcome="failed"), BY
    )
    assert shop.get_item(item).status == "failed"


def test_a_retry_after_a_start_that_made_its_run_attaches_it(shop: Shop, pool: PgPool) -> None:
    """The start answered 5xx but its run was made; a hold lands before the retry. The
    print is real: the retry attaches it, and the hold is for what comes next."""
    order, item = shop.accepted()
    first = Starts(pool, refuse=ApiError(503, "Temporal is busy"), made=True)
    with pytest.raises(ApiError):
        _print(shop, order, item, first, "k")
    assert first.run is not None
    shop.lifecycle.hold(order.id, BY)
    retry = Starts(pool)
    retry.run = first.run
    attempt = _print(shop, order, item, retry, "k")
    assert retry.calls == []
    assert attempt.print_run_id == first.run.id
    assert [f.id for f in shop.rows.fulfilments(item.id)] == [attempt.id]
    assert held(pool, FULFILMENT_HOLDER) == {attempt.id: "quote-a"}
    assert shop.get_item(item).status == "queued"
    assert "print.refused" not in shop.kinds(order)
