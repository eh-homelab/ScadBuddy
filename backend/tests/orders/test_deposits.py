"""Deposits (#5281, storefront spec §9): a deposit set on a `draft` or `quoted` order, its
`deposit` payment request, the shop's deposit policy, "Proceed without deposit", and the
`deposit_unpaid` rule that holds a revision's send and a test print until it is paid."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from scadbuddy.orders.lifecycle import LifecycleError, ManualQuoteInput
from scadbuddy.orders.models import Order, OrderItem, Revision
from scadbuddy.orders.money import DepositRule, OrderRules
from scadbuddy.orders.revisions import RevisionAnswerInput
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.visitors import VisitorStore, visitor_actor
from scadbuddy.payments.models import HostedRequest, ProviderEvent
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import (
    BY,
    RulesShop,
    Shop,
    connect,
    requests_for,
    resolved,
    set_request_status,
)
from tests.orders.test_revisions import attach_render

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def _slug(call: Callable[[], object]) -> LifecycleError:
    with pytest.raises(LifecycleError) as e:
        call()
    return e.value


def requested(shop: Shop) -> tuple[Order, OrderItem, Revision]:
    """A draft order with a request item, its first revision and a render on it."""
    order = shop.order()
    item = shop.lifecycle.add_request_item(order.id, BY, title="A bracket", quantity=1)
    rev = shop.lifecycle.add_revision(order.id, item.id, BY, resolved())
    attach_render(shop, order, shop.get_item(item), rev)
    return order, shop.get_item(item), rev


def test_a_set_deposit_opens_a_deposit_request(shop: Shop) -> None:
    order = shop.lifecycle.set_deposit(shop.order().id, BY, 2000)
    [req] = requests_for(shop, order)
    assert (order.deposit_minor, req.kind, req.amount_minor, req.status) == (
        2000,
        "deposit",
        2000,
        "open",
    )
    assert req.provider is None
    [event] = [e for e in shop.rows.events(order.id) if e.kind == "deposit.set"]
    assert event.detail == {"amount_minor": 2000}


def test_the_deposit_request_is_on_the_default_provider(shop: Shop) -> None:
    shop.lifecycle.offered_providers = frozenset({"fake"})
    connect(shop.rows, default=True)
    order = shop.lifecycle.set_deposit(shop.order().id, BY, 2000)
    assert [r.provider for r in requests_for(shop, order)] == ["fake"]


def test_changing_the_deposit_cancels_the_old_request(shop: Shop) -> None:
    order = shop.order()
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    shop.lifecycle.set_deposit(order.id, BY, 3000)
    found = requests_for(shop, order)
    assert [(r.amount_minor, r.status) for r in found] == [(2000, "cancelled"), (3000, "open")]
    # The audit line names the cancelled request, as a request's own cancel does.
    events = shop.rows.events(order.id)
    cancels = [e.detail for e in events if e.kind == "payment_request.cancelled"]
    assert cancels == [{"request_id": found[0].id}]


def test_the_request_asks_only_what_is_not_paid(shop: Shop) -> None:
    order = shop.order()
    shop.lifecycle.add_payment(order.id, BY, amount_minor=500, method="cash")
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    assert [(r.amount_minor, r.status) for r in requests_for(shop, order)] == [(1500, "open")]
    shop.lifecycle.set_deposit(order.id, BY, 500)
    assert [r.status for r in requests_for(shop, order)] == ["cancelled"]


def test_clearing_the_deposit_cancels_its_request(shop: Shop) -> None:
    order = shop.order()
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    shop.lifecycle.waive_deposit(order.id, BY, "known customer")
    order = shop.lifecycle.set_deposit(order.id, BY, None)
    assert (order.deposit_minor, order.deposit_waived_reason) == (None, None)
    assert [r.status for r in requests_for(shop, order)] == ["cancelled"]


def test_a_deposit_below_one_is_422(shop: Shop) -> None:
    order = shop.order()
    error = _slug(lambda: shop.lifecycle.set_deposit(order.id, BY, 0))
    assert (error.slug, error.unprocessable) == ("amount_minor", True)


def test_the_deposit_is_fixed_once_accepted(shop: Shop) -> None:
    order, _ = shop.accepted()
    assert _slug(lambda: shop.lifecycle.set_deposit(order.id, BY, 2000)).slug == (
        "order_not_editable"
    )
    assert _slug(lambda: shop.lifecycle.apply_deposit_policy(order.id, BY)).slug == (
        "order_not_editable"
    )
    cancelled = shop.lifecycle.cancel_order(shop.order().id, BY)
    assert _slug(lambda: shop.lifecycle.set_deposit(cancelled.id, BY, 2000)).slug == (
        "order_cancelled"
    )


def test_an_unpaid_deposit_refuses_a_send(shop: Shop) -> None:
    order, item, rev = requested(shop)
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    error = _slug(lambda: shop.lifecycle.send_revision(order.id, item.id, rev.id, BY))
    assert error.slug == "deposit_unpaid"
    assert "revision.sent" not in shop.kinds(order)


def test_a_sent_revision_is_returned_whatever_the_deposit(shop: Shop) -> None:
    order, item, rev = requested(shop)
    sent = shop.lifecycle.send_revision(order.id, item.id, rev.id, BY)
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    assert shop.lifecycle.send_revision(order.id, item.id, rev.id, BY).sent_at == sent.sent_at


def _test_print(shop: Shop, order: Order, item: OrderItem, rev: Revision, key: str) -> object:
    return shop.lifecycle.claim_print(
        order.id,
        item.id,
        BY,
        request_id=key,
        sliced_sha256=None,
        sliced_blob_key=None,
        revision_id=rev.id,
    )


def test_an_unpaid_deposit_refuses_a_test_print(shop: Shop) -> None:
    order, item, rev = requested(shop)
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    error = _slug(lambda: _test_print(shop, order, item, rev, "trial"))
    assert error.slug == "deposit_unpaid"
    assert shop.rows.fulfilments(item.id) == []


def test_a_retried_test_print_claim_is_not_refused(shop: Shop) -> None:
    order, item, rev = requested(shop)
    claim = _test_print(shop, order, item, rev, "trial")
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    assert _test_print(shop, order, item, rev, "trial") == claim


# Review Focus 3
def test_a_cash_payment_for_the_deposit_closes_its_request_and_lifts_the_rule(
    shop: Shop,
) -> None:
    order, item, rev = requested(shop)
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=1500, method="cash")
    assert requests_for(shop, order)[0].status == "open"
    payment = shop.lifecycle.add_payment(order.id, BY, amount_minor=500, method="cash")
    [req] = requests_for(shop, order)
    assert (req.status, req.payment_id) == ("paid", payment.id)
    assert shop.lifecycle.send_revision(order.id, item.id, rev.id, BY).sent_at is not None


def test_a_payment_closes_only_an_open_deposit_request(shop: Shop) -> None:
    order = shop.order()
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    [deposit] = requests_for(shop, order)
    shop.lifecycle.cancel_payment_request(order.id, deposit.id, BY)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=2000, method="cash")
    assert [r.status for r in requests_for(shop, order)] == ["cancelled"]


def test_proceed_without_deposit_needs_a_reason_and_lifts_the_rule(shop: Shop) -> None:
    order, item, rev = requested(shop)
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    error = _slug(lambda: shop.lifecycle.waive_deposit(order.id, BY, "  "))
    assert (error.slug, error.unprocessable) == ("reason", True)
    error = _slug(lambda: shop.lifecycle.waive_deposit(order.id, BY, "x" * 501))
    assert (error.slug, error.unprocessable) == ("reason", True)
    order = shop.lifecycle.waive_deposit(order.id, BY, "  regular customer ")
    assert order.deposit_waived_reason == "regular customer"
    last = shop.rows.events(order.id)[-1]
    assert (last.kind, last.detail) == ("deposit.waived", {"reason": "regular customer"})
    assert [r.status for r in requests_for(shop, order)] == ["open"]
    assert shop.lifecycle.send_revision(order.id, item.id, rev.id, BY).sent_at is not None
    _test_print(shop, order, item, rev, "trial")


def test_a_closed_order_takes_no_waiver(shop: Shop) -> None:
    order = shop.lifecycle.cancel_order(shop.order().id, BY)
    assert _slug(lambda: shop.lifecycle.waive_deposit(order.id, BY, "why")).slug == (
        "order_cancelled"
    )


# -- the policy -------------------------------------------------------------------------------

HAS_REQUEST_2000 = DepositRule(criterion="has_request", amount_minor=2000)
TOTAL_ABOVE_10000_50PCT = DepositRule(criterion="total_above", above_minor=10000, percent="50")
FIRST_ORDER_25PCT = DepositRule(criterion="first_order", percent="25")


def _policy(*rules: DepositRule) -> OrderRules:
    return OrderRules("USD", Decimal(0), False, rules, ())


def _setup(shop: RulesShop, setup: str) -> Order:
    if setup == "request_item":
        order = shop.order()
        shop.lifecycle.add_request_item(order.id, BY, title="A bracket", quantity=1)
        return order
    if setup == "item_quoted_20000":
        order = shop.order()
        shop.quoted(order, shop.item(order), price=20000)
        return order
    if setup == "first_order_quoted_1000":
        order = shop.order()
        shop.quoted(order, shop.item(order), price=1000)
        return order
    assert setup == "second_order_after_an_accepted_one"
    shop.accepted()
    order = shop.order()
    shop.quoted(order, shop.item(order), price=1000)
    return order


@pytest.mark.parametrize(
    ("policy", "setup", "want"),
    [
        ([HAS_REQUEST_2000], "request_item", 2000),
        ([TOTAL_ABOVE_10000_50PCT], "item_quoted_20000", 10000),
        ([FIRST_ORDER_25PCT], "first_order_quoted_1000", 250),
        ([FIRST_ORDER_25PCT], "second_order_after_an_accepted_one", None),
    ],
)
def test_the_policy_sets_the_largest_deposit_that_applies(
    rows: OrderStore, pool: PgPool, policy: list[DepositRule], setup: str, want: int | None
) -> None:
    shop = RulesShop(rows, BlobRefs(pool), _policy(*policy))
    order = _setup(shop, setup)
    order = shop.lifecycle.apply_deposit_policy(order.id, BY)
    assert order.deposit_minor == want
    # No deposit asked of an order with none is no change: nothing is written.
    sets = [e.detail for e in shop.rows.events(order.id) if e.kind == "deposit.set"]
    assert sets == ([{"amount_minor": want, "policy": True}] if want else [])
    assert [r.amount_minor for r in requests_for(shop, order)] == ([want] if want else [])


def test_the_policy_ignores_a_cancelled_request_item(rows: OrderStore, pool: PgPool) -> None:
    shop = RulesShop(rows, BlobRefs(pool), _policy(HAS_REQUEST_2000))
    order = shop.order()
    shop.quoted(order, shop.item(order))
    request = shop.lifecycle.add_request_item(order.id, BY, title="A bracket", quantity=1)
    shop.lifecycle.cancel_item(order.id, request.id, BY)
    assert shop.lifecycle.apply_deposit_policy(order.id, BY).deposit_minor is None


# -- an unchanged deposit writes nothing ------------------------------------------------------


def _writes(shop: Shop, order: Order) -> tuple[int, list[tuple[str, str]]]:
    return len(shop.rows.events(order.id)), [(r.id, r.status) for r in requests_for(shop, order)]


def test_the_same_deposit_again_writes_nothing(shop: Shop) -> None:
    order = shop.order()
    shop.lifecycle.add_payment(order.id, BY, amount_minor=500, method="cash")
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    before, told = _writes(shop, order), len(shop.events)
    again = shop.lifecycle.set_deposit(order.id, BY, 2000)
    assert again.deposit_minor == 2000 and _writes(shop, order) == before
    assert len(shop.events) == told  # no OrderChanged: it would fire flow triggers
    shop.lifecycle.set_deposit(order.id, BY, 2500)
    assert [(r.amount_minor, r.status) for r in requests_for(shop, order)] == [
        (1500, "cancelled"),
        (2000, "open"),
    ]


def test_the_same_deposit_after_a_payment_asks_again_for_the_rest(shop: Shop) -> None:
    order = shop.order()
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=500, method="cash")
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    assert [(r.amount_minor, r.status) for r in requests_for(shop, order)] == [
        (2000, "cancelled"),
        (1500, "open"),
    ]


def test_the_same_policy_deposit_again_writes_nothing(rows: OrderStore, pool: PgPool) -> None:
    shop = RulesShop(rows, BlobRefs(pool), _policy(FIRST_ORDER_25PCT))
    order = _setup(shop, "first_order_quoted_1000")
    shop.lifecycle.apply_deposit_policy(order.id, BY)
    before, told = _writes(shop, order), len(shop.events)
    assert shop.lifecycle.apply_deposit_policy(order.id, BY).deposit_minor == 250
    assert _writes(shop, order) == before and len(shop.events) == told


def test_no_deposit_cleared_or_waived_writes_nothing(shop: Shop) -> None:
    order = shop.order()
    before, told = _writes(shop, order), len(shop.events)
    assert shop.lifecycle.set_deposit(order.id, BY, None).deposit_minor is None
    waived = shop.lifecycle.waive_deposit(order.id, BY, "no need")
    assert waived.deposit_waived_reason is None and _writes(shop, order) == before
    assert len(shop.events) == told


def test_the_same_deposit_waiver_twice_writes_one_row(shop: Shop) -> None:
    order = shop.lifecycle.set_deposit(shop.order().id, BY, 2000)
    shop.lifecycle.waive_deposit(order.id, BY, "known customer")
    before, told = _writes(shop, order), len(shop.events)
    again = shop.lifecycle.waive_deposit(order.id, BY, " known customer ")
    assert again.deposit_waived_reason == "known customer"
    assert _writes(shop, order) == before and len(shop.events) == told
    shop.lifecycle.waive_deposit(order.id, BY, "pays later")
    waivers = [e.detail for e in shop.rows.events(order.id) if e.kind == "deposit.waived"]
    assert waivers == [{"reason": "known customer"}, {"reason": "pays later"}]


def test_a_refund_after_acceptance_brings_the_rule_back_until_waived(shop: Shop) -> None:
    order, item, rev = requested(shop)
    shop.lifecycle.send_revision(order.id, item.id, rev.id, BY)
    shop.lifecycle.answer_revision(
        order.id, item.id, rev.id, RevisionAnswerInput(answer="accepted"), BY
    )
    quote = shop.lifecycle.record_manual_quote(
        order.id, item.id, ManualQuoteInput(unit_price_minor=5000, label="typed"), BY
    )
    shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY)
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=2000, method="cash")
    shop.lifecycle.accept_order(order.id, BY)
    shop.lifecycle.add_payment(order.id, BY, amount_minor=-500, method="cash")

    assert _slug(lambda: _test_print(shop, order, item, rev, "trial")).slug == "deposit_unpaid"
    assert _slug(lambda: shop.lifecycle.set_deposit(order.id, BY, 1500)).slug == (
        "order_not_editable"
    )
    shop.lifecycle.waive_deposit(order.id, BY, "refunded a part by mistake")
    _test_print(shop, order, item, rev, "trial")


def test_the_same_deposit_after_a_waiver_lifts_the_waiver(shop: Shop) -> None:
    order, item, rev = requested(shop)
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    shop.lifecycle.waive_deposit(order.id, BY, "regular customer")
    order = shop.lifecycle.set_deposit(order.id, BY, 2000)
    assert order.deposit_waived_reason is None
    error = _slug(lambda: shop.lifecycle.send_revision(order.id, item.id, rev.id, BY))
    assert error.slug == "deposit_unpaid"


def _cart(shop: Shop, pool: PgPool) -> Order:
    visitor = VisitorStore(pool).ensure("v" * 43)
    return shop.lifecycle.ensure_draft(visitor.id, visitor_actor(visitor), currency="USD")


CART_ACTS: dict[str, Callable[[Shop, Order], object]] = {
    "set_deposit": lambda shop, cart: shop.lifecycle.set_deposit(cart.id, BY, 2000),
    "apply_deposit_policy": lambda shop, cart: shop.lifecycle.apply_deposit_policy(cart.id, BY),
    "waive_deposit": lambda shop, cart: shop.lifecycle.waive_deposit(cart.id, BY, "why"),
    "waive_balance": lambda shop, cart: shop.lifecycle.waive_balance(cart.id, BY, "why"),
}


@pytest.mark.parametrize("act", list(CART_ACTS))
def test_a_cart_takes_no_deposit_or_waiver(shop: Shop, pool: PgPool, act: str) -> None:
    cart = _cart(shop, pool)
    before, told = _writes(shop, cart), len(shop.events)
    assert _slug(lambda: CART_ACTS[act](shop, cart)).slug == "order_not_submitted"
    assert _writes(shop, cart) == before and len(shop.events) == told
    after = shop.get(cart)
    assert (after.deposit_minor, after.deposit_waived_reason, after.balance_waived_reason) == (
        None,
        None,
        None,
    )


DEPOSIT_ACTS: dict[str, Callable[[Shop, Order], object]] = {
    "set_deposit": lambda shop, order: shop.lifecycle.set_deposit(order.id, BY, 3000),
    "apply_deposit_policy": lambda shop, order: shop.lifecycle.apply_deposit_policy(order.id, BY),
}


@pytest.mark.parametrize("act", list(DEPOSIT_ACTS))
def test_a_pending_deposit_request_holds_the_deposit(shop: Shop, act: str) -> None:
    order = shop.lifecycle.set_deposit(shop.order().id, BY, 2000)
    [req] = requests_for(shop, order)
    set_request_status(shop, req.id, "pending")
    before, told = _writes(shop, order), len(shop.events)
    error = _slug(lambda: DEPOSIT_ACTS[act](shop, order))
    assert (error.slug, error.unprocessable, error.not_found) == ("payment_in_flight", False, False)
    assert _writes(shop, order) == before and len(shop.events) == told


def test_a_failed_deposit_request_is_cancelled_and_replaced(shop: Shop) -> None:
    order = shop.lifecycle.set_deposit(shop.order().id, BY, 2000)
    [req] = requests_for(shop, order)
    set_request_status(shop, req.id, "failed")
    shop.lifecycle.set_deposit(order.id, BY, 2000)
    assert [(r.kind, r.amount_minor, r.status) for r in requests_for(shop, order)] == [
        ("deposit", 2000, "cancelled"),
        ("deposit", 2000, "open"),
    ]


def test_a_payment_that_reaches_the_deposit_pays_a_failed_deposit_request(shop: Shop) -> None:
    """#4959: a customer can approve a failed request's page again, so it is paid too."""
    order = shop.lifecycle.set_deposit(shop.order().id, BY, 2000)
    [req] = requests_for(shop, order)
    set_request_status(shop, req.id, "failed")
    shop.lifecycle.add_payment(order.id, BY, amount_minor=2000, method="cash")
    assert [r.status for r in requests_for(shop, order)] == ["paid"]


def test_a_provider_payment_that_reaches_the_deposit_pays_its_request(
    rows: OrderStore, pool: PgPool
) -> None:
    """Review of #5726: a provider's payment on another request that covers the deposit
    ends the deposit request `paid`, as a manual payment does, so its page closes."""
    shop = Shop(rows, BlobRefs(pool))
    shop.lifecycle.offered_providers = frozenset({"fake"})
    connect(rows, default=True)
    order = shop.order()
    item = shop.item(order)
    shop.quoted(order, item)
    whole = shop.lifecycle.open_payment_request(order.id, BY, provider="fake")
    shop.lifecycle.set_request_link(
        whole.id,
        hosted=HostedRequest.model_validate(
            {"provider_ref": "fake_whole", "hosted_url": "https://payments.fake.invalid/w"}
        ),
        error=None,
    )
    shop.lifecycle.set_deposit(order.id, BY, 500)
    deposit = next(r for r in requests_for(shop, order) if r.kind == "deposit")
    shop.lifecycle.set_request_link(
        deposit.id,
        hosted=HostedRequest.model_validate(
            {"provider_ref": "fake_dep", "hosted_url": "https://payments.fake.invalid/d"}
        ),
        error=None,
    )
    paid = ProviderEvent(
        event_id="e-whole",
        kind="paid",
        provider_ref="fake_whole",
        amount_minor=1000,
        currency="USD",
    )
    assert shop.lifecycle.record_provider_event("fake", paid) == "recorded"
    statuses = {(r.kind, r.status) for r in requests_for(shop, order)}
    assert statuses == {("balance", "paid"), ("deposit", "paid")}
    # Its page is marked, so the webhook closes it after the commit (#5700).
    closing = next(r for r in requests_for(shop, order) if r.kind == "deposit")
    assert closing.close_pending_at is not None


# -- a policy rule in another currency (review of #5726) ----------------------------------

FIRST_ORDER_500 = DepositRule(criterion="first_order", amount_minor=500)


def _eur(*rules: DepositRule) -> OrderRules:
    """The shop's currency is EUR; the orders here are USD."""
    return OrderRules("EUR", Decimal(0), False, rules, ())


def test_a_policy_with_only_rules_in_another_currency_is_refused(
    rows: OrderStore, pool: PgPool
) -> None:
    shop = RulesShop(rows, BlobRefs(pool), _eur(FIRST_ORDER_500, TOTAL_ABOVE_10000_50PCT))
    order = shop.order()
    shop.quoted(order, shop.item(order), price=20000)
    before = _writes(shop, order)
    error = _slug(lambda: shop.lifecycle.apply_deposit_policy(order.id, BY))
    assert error.slug == "deposit_policy_currency"
    assert "EUR" in str(error) and "USD" in str(error)
    assert _writes(shop, order) == before


def test_a_policy_deposit_names_the_rules_it_skipped(rows: OrderStore, pool: PgPool) -> None:
    shop = RulesShop(rows, BlobRefs(pool), _eur(FIRST_ORDER_500, FIRST_ORDER_25PCT))
    order = shop.order()
    shop.quoted(order, shop.item(order), price=1000)
    order = shop.lifecycle.apply_deposit_policy(order.id, BY)
    assert order.deposit_minor == 250
    sets = [e.detail for e in shop.rows.events(order.id) if e.kind == "deposit.set"]
    assert sets == [{"amount_minor": 250, "policy": True, "skipped": [0]}]
