"""`/api/v1/orders/{order_id}/messages` (#5660, spec
2026-11-08-order-notify-customer-design.md §7.1): the record of the messages the agent sends
to an order's customer through a messaging plugin (``orders/messages.py``). The agent claims
a message before it calls the plugin and posts the outcome after; a person marks an
``unknown`` one sent (``auth/routes.py`` ``PERSON_ENDPOINTS``). Nothing here sends.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status
from pydantic import BaseModel

from scadbuddy.api.orders import check_path
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersComponent, OrdersDep
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.message_models import (
    STALE_SENDING_MINUTES,
    MessageClaim,
    MessageOutcome,
    NotifyTemplate,
    OrderMessage,
)
from scadbuddy.orders.messages import OrderMessages
from scadbuddy.orders.problems import refused

router = APIRouter(prefix="/orders", tags=["orders"])

ClaimKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


class MessageCheck(BaseModel):
    """Whether a send of the template would be refused now, and why."""

    #: The message that already told the customer since the order's last status change.
    already_notified: OrderMessage | None
    #: The storefront mails this template's news to the order's customer itself (spec §8),
    #: so a claim without `also_when_storefront_mails` would be recorded `skipped`.
    storefront_mail: bool


def _messages(orders: OrdersComponent) -> OrderMessages:
    """The record, asking the storefront overlap with the lifecycle's running mail host."""
    return OrderMessages(orders.store, orders.lifecycle.mail_host)


async def _run[T](call: Callable[[], T]) -> T:
    try:
        return await asyncio.to_thread(call)
    except LifecycleError as error:
        raise refused(error) from None


@router.get(
    "/{order_id}/messages",
    response_model=list[OrderMessage],
    summary="The messages sent to an order's customer",
    description=(
        "Newest first: each message the agent sent, or tried to send, through the bound "
        "messaging plugin, with exactly what went to whom and how it ended. A message "
        f"still `sending` {STALE_SENDING_MINUTES} minutes after its claim reads `unknown`: "
        "it may have reached the customer, and it is never sent again by itself."
    ),
)
async def list_order_messages(order_id: str, orders: OrdersDep) -> list[OrderMessage]:
    check_path(order_id)
    return await _run(lambda: _messages(orders).list(order_id))


@router.get(
    "/{order_id}/messages/check",
    response_model=MessageCheck,
    summary="Whether a message of a template may be sent now",
    description=(
        "Writes nothing. `already_notified` is the message that already told the customer "
        "this template since the order's last status change (`sent`, `sending` or "
        "`unknown`), which a claim without `resend_of` is refused for. `storefront_mail` is "
        "true when the storefront mails this template's news to the order's customer itself "
        "(`quoted` and `shipped`, on an order with a live order link while a mail host is "
        "set), so a claim without `also_when_storefront_mails` would be recorded `skipped`. "
        "409 `order_not_submitted` for a cart."
    ),
)
async def check_order_message(
    order_id: str, orders: OrdersDep, template: Annotated[NotifyTemplate, Query()]
) -> MessageCheck:
    check_path(order_id)
    told, mails = await _run(lambda: _messages(orders).check(order_id, template))
    return MessageCheck(already_notified=told, storefront_mail=mails)


@router.post(
    "/{order_id}/messages",
    response_model=OrderMessage,
    status_code=status.HTTP_201_CREATED,
    summary="Claim a message before it is sent",
    description=(
        "Records a `sending` message before the agent calls the plugin, under the order's "
        "lock, with its audit row `message.sending`. The `Idempotency-Key` makes a retry "
        "safe: a key seen before answers its message as it is (200) and writes nothing; a "
        "key of another order is 409 `idempotency_key_reused`. 409 `already_notified` when "
        "the template was already `sent`, `sending` or `unknown` since the order's last "
        "status change and the body names no `resend_of`; 409 `message_in_flight` when "
        "`resend_of` is still being sent (a stale one reads `unknown` and may be sent "
        "again); 422 on `resend_of` for a message not of this order or of another "
        "template; 409 `order_not_submitted` for a cart. When the storefront mails this "
        "template's news for the order itself and the body has no "
        "`also_when_storefront_mails`, the message is recorded `skipped` (reason "
        "`storefront_mail`, audit row `message.skipped`) instead of `sending`: the agent "
        "sends nothing."
    ),
    responses={status.HTTP_200_OK: {"model": OrderMessage, "description": "Already claimed"}},
)
async def claim_order_message(
    order_id: str,
    body: MessageClaim,
    orders: OrdersDep,
    response: Response,
    idempotency_key: ClaimKey,
) -> OrderMessage:
    check_path(order_id, idempotency_key)
    by = current_actor()
    message, written = await _run(
        lambda: _messages(orders).claim(order_id, idempotency_key, body, by)
    )
    if not written:
        response.status_code = status.HTTP_200_OK
    return message


@router.post(
    "/{order_id}/messages/{message_id}/outcome",
    response_model=OrderMessage,
    summary="Record how a message's send ended",
    description=(
        "`sent`, `failed` or `unknown`, with a fixed `reason` (never the plugin's text) "
        "for the last two. Taken while the message is `sending` (stale or not) or "
        "`unknown`, so a slow plugin's late answer is recorded; 409 `message_final` once "
        "it is `sent`, `failed` or `skipped`. Audit row `message.<status>`."
    ),
)
async def record_order_message_outcome(
    order_id: str, message_id: str, body: MessageOutcome, orders: OrdersDep
) -> OrderMessage:
    check_path(order_id, message_id)
    by = current_actor()
    return await _run(lambda: _messages(orders).outcome(order_id, message_id, body, by))


@router.post(
    "/{order_id}/messages/{message_id}/mark-sent",
    response_model=OrderMessage,
    summary="Mark an unknown message as sent",
    description=(
        "A person's word that a message whose outcome is `unknown` did reach the customer. "
        "409 `message_not_unknown` for any other status. Audit row `message.marked_sent`."
    ),
)
async def mark_order_message_sent(
    order_id: str, message_id: str, orders: OrdersDep
) -> OrderMessage:
    check_path(order_id, message_id)
    by = current_actor()
    return await _run(lambda: _messages(orders).mark_sent(order_id, message_id, by))
