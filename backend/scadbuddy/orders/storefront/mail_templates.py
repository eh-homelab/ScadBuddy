"""The storefront's mail templates (#5283, spec 2026-11-02 §5.7): a subject and a short
plain-text body per event. Customer text (an item's title, a carrier) goes in as it was
typed and is never HTML: the message is ``text/plain``. A subject is one line, whatever
its values hold. Every order mail carries the order link, ``{public_url}/o#{token}``
(ruling R1), which the sender builds from the env-only ``storefront_public_url`` and the
link's sealed token; a sign-in code mail carries the code and no link.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from scadbuddy.orders.currency import minor_exponent

MailEvent = Literal[
    "sign_in_code", "order_link", "revision_ready", "price_ready", "payment_requested", "shipped"
]

#: The events the outbox sends only for an order the customer follows by its link
#: (`mail.storefront_mail_covers`); the rest are #5660's plugin's job.
ORDER_EVENTS: frozenset[MailEvent] = frozenset(
    {"revision_ready", "price_ready", "payment_requested", "shipped"}
)


@dataclass(frozen=True)
class Rendered:
    subject: str
    body: str


def _line(text: str) -> str:
    """``text`` on one line: a header never takes a break from a value."""
    return " ".join(text.split())


def _text(context: Mapping[str, Any], name: str, default: str = "") -> str:
    value = context.get(name)
    return default if value is None else str(value)


def _amount(context: Mapping[str, Any]) -> str:
    currency = _text(context, "currency")
    minor = context.get("amount_minor")
    if not isinstance(minor, int) or not currency:
        return ""
    exponent = minor_exponent(currency)
    return f"{Decimal(minor).scaleb(-exponent):.{exponent}f} {currency}"


def render(event: MailEvent, context: Mapping[str, Any], link: str | None) -> Rendered:
    """The mail for ``event`` with ``context``. ``link`` is the order link, which every
    order mail needs (ValueError without one); a sign-in code takes none."""
    order = _text(context, "order")
    if event == "sign_in_code":
        code = _text(context, "code")
        return Rendered(
            "Your sign-in code",
            f"Your sign-in code is {code}.\n\n"
            "It works for 10 minutes. If you did not ask for it, you can ignore this mail.\n",
        )
    if not link:
        raise ValueError(f"the {event} mail needs the order link")
    follow = f"Follow your order here:\n{link}\n"
    if event == "order_link":
        return Rendered(
            _line(f"Your order {order}"),
            f"Thank you for your order {order}.\n\n{follow}\n"
            "Keep this link to yourself: anyone with it can see and answer for your order.\n",
        )
    if event == "revision_ready":
        item = _text(context, "item", "your item")
        return Rendered(
            _line(f"A design is ready for order {order}"),
            f"A design of {item} is ready for you to look at, on order {order}.\n\n{follow}",
        )
    if event == "price_ready":
        item = _text(context, "item", "your item")
        return Rendered(
            _line(f"Your price is ready for order {order}"),
            f"The price of {item} is ready for you to accept, on order {order}.\n\n{follow}",
        )
    if event == "payment_requested":
        amount = _amount(context)
        due = f" of {amount}" if amount else ""
        return Rendered(
            _line(f"Payment requested for order {order}"),
            f"We asked for a payment{due} for order {order}.\n\n{follow}",
        )
    carrier = _text(context, "carrier")
    tracking = _text(context, "tracking")
    lines = [f"Your order {order} has shipped."]
    if carrier:
        lines.append(f"Carrier: {carrier}")
    if tracking:
        lines.append(f"Tracking: {tracking}")
    return Rendered(_line(f"Order {order} has shipped"), "\n".join(lines) + f"\n\n{follow}")
