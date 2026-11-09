"""The storefront edge's CRUD routes (#5282, spec 2026-11-02 §5.1): under
``/api/v1/internal/crud/storefront/``, for the ``storefront`` credential only (`crud_router`).
Its ``GET …/areas`` is `api/crud_areas.py`'s; each area's task adds its routes here.

The edge sends the visitor id it verified in the cookie (`VISITOR_HEADER`) and the
browser's address (`CLIENT_HEADER`). This side cannot check the cookie's mac: it takes
the id only as a 256-bit secret to look a visitor up by, and the address only to key the
per-client render limit (plan "Trust model"). `visitor_id` and `store_client` are the
dependencies that read them.

``POST …/pay/checkout`` (#4960, the interface agreed on #5283) answers the edge's
``/pay/{token}`` page: what it needs to render PayPal's buttons for one request, or a
bare 404 (no reason) for a token that reads nothing (`payments.venmo.venmo_checkout`).
The token travels in the body, never the path, so no access log holds it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Final

from fastapi import Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.orders.component import OrdersDep
from scadbuddy.payments.venmo import VenmoCheckout, venmo_checkout
from scadbuddy.storefront.wire import (
    CLIENT_HEADER,
    STORE_CLIENT_PROBLEM,
    VISITOR_HEADER,
    VISITOR_ID_PROBLEM,
)

router = crud_router("storefront")

#: 32 random bytes as URL-safe base64, unpadded (`secrets.token_urlsafe(32)`).
VISITOR_ID: Final = re.compile(r"[A-Za-z0-9_-]{43}")


def visitor_id(request: Request) -> str:
    """The visitor id the edge sends; 400 ``…/storefront/visitor_id`` unless it is one."""
    value = request.headers.get(VISITOR_HEADER, "")
    if VISITOR_ID.fullmatch(value) is None:
        raise ApiError(
            400,
            f"{VISITOR_HEADER} must be a visitor id of 43 URL-safe characters.",
            type_=VISITOR_ID_PROBLEM,
        )
    return value


def store_client(request: Request) -> str:
    """The browser's address the edge sends; 400 ``…/storefront/store_client`` unless it
    is one IP address."""
    value = request.headers.get(CLIENT_HEADER, "")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise ApiError(
            400,
            f"{CLIENT_HEADER} must be one IP address.",
            type_=STORE_CLIENT_PROBLEM,
        ) from None
    return value


class PayCheckoutRead(BaseModel):
    """The pay page's token, as the edge took it from ``/pay/{token}``."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(max_length=100)


@router.post(
    "/pay/checkout",
    name="crud_storefront_pay_checkout",
    response_model=VenmoCheckout,
    responses={status.HTTP_404_NOT_FOUND: {"description": "No open Venmo request has it"}},
)
async def pay_checkout(body: PayCheckoutRead, orders: OrdersDep) -> VenmoCheckout | Response:
    """#4960: the request a pay token names, for the edge's pay page; 404 with no body
    for any token that reads nothing, so the edge learns no reason."""

    def read() -> VenmoCheckout | None:
        with orders.store.transaction() as conn:
            return venmo_checkout(conn, body.token)

    found = await asyncio.to_thread(read)
    if found is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return found
