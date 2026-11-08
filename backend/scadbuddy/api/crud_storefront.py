"""The storefront edge's CRUD routes (#5282, spec 2026-11-02 §5.1): under
``/api/v1/internal/crud/storefront/``, for the ``storefront`` credential only (`crud_router`).
Its ``GET …/areas`` is `api/crud_areas.py`'s; each area's task adds its routes here.

The edge sends the visitor id it verified in the cookie (`VISITOR_HEADER`) and the
browser's address (`CLIENT_HEADER`). This side cannot check the cookie's mac: it takes
the id only as a 256-bit secret to look a visitor up by, and the address only to key the
per-client render limit (plan "Trust model"). `visitor_id` and `store_client` are the
dependencies that read them, and every route takes both: only a ``storefront`` credential
reaches these routes (`crud_router`), so no other caller can send either header here.

Area ``listings``: ``GET …/listings`` and ``GET …/listings/{id}``, the published listings
as the public site shows them (`orders/storefront/listings.py` `public_listing`), with
each media file as a presigned GET of an hour. An unpublished or unknown listing is 404
``…/storefront/listing_not_found``; any store but S3 is 409
``…/storefront/storefront_needs_s3``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Annotated, Final

from fastapi import Depends, Request

from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.orders.component import OrdersComponent, OrdersDep
from scadbuddy.orders.models import Filament
from scadbuddy.orders.storefront.listings import public_content, public_listing, published_listing
from scadbuddy.orders.storefront.wire import PublicListing, PublicListings
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


Visitor = Annotated[str, Depends(visitor_id)]
StoreClient = Annotated[str, Depends(store_client)]


def _filaments(orders: OrdersComponent) -> dict[str, Filament]:
    """The price book's filaments not archived, by id."""
    return {f.id: f for f in orders.store.filaments()}


@router.get("/listings", name="crud_storefront_listings")
async def listings(
    state: StateDep, orders: OrdersDep, _visitor: Visitor, _client: StoreClient
) -> PublicListings:
    """The published listings, in the shop's order, and whether "Ask for a design" is on."""
    content = public_content(state)
    published = await asyncio.to_thread(orders.listings.published)
    filaments = await asyncio.to_thread(_filaments, orders)
    snapshot = await asyncio.to_thread(state.settings_store.snapshot)
    return PublicListings(
        listings=[await public_listing(content, item, filaments) for item in published],
        requests_enabled=snapshot.runtime.storefront_requests_enabled,
    )


@router.get("/listings/{listing_id}", name="crud_storefront_listing")
async def listing(
    listing_id: str, state: StateDep, orders: OrdersDep, _visitor: Visitor, _client: StoreClient
) -> PublicListing:
    """One published listing; 404 ``listing_not_found`` for an unpublished or unknown one."""
    content = public_content(state)
    found = await asyncio.to_thread(published_listing, orders.listings, listing_id)
    filaments = await asyncio.to_thread(_filaments, orders)
    return await public_listing(content, found, filaments)
