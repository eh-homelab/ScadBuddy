"""`/api/v1/storefront` (#5282, spec 2026-11-02 §5.3): the back office's Storefront page.
The shop lists, makes, edits, orders, publishes and deletes the listings the public site
offers (`orders/storefront/listings.py`). These routes are the back office's only: the
public site reads the published listings through `api/crud_storefront.py`, never these.

An edit names the ``updated_at`` the editor read (409 ``listing_changed`` when the
listing moved on), and unpublishes the listing. A delete is refused while the listing is
published. Every refusal is a problem under ``https://scadbuddy.dev/problems/storefront/``.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio

from fastapi import APIRouter, Response, status

from scadbuddy.api.deps import StateDep
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.storefront.listings import publish, unpublish
from scadbuddy.orders.storefront.wire import Listing, ListingInput, ListingOrder, ListingUpdate

router = APIRouter(prefix="/storefront", tags=["storefront"])


@router.get("/listings", response_model=list[Listing], summary="The storefront's listings")
async def list_listings(orders: OrdersDep) -> list[Listing]:
    """Every listing, published or not, in the order the public site shows them."""
    return await asyncio.to_thread(orders.listings.all)


@router.post(
    "/listings",
    response_model=Listing,
    status_code=status.HTTP_201_CREATED,
    summary="Make a storefront listing",
)
async def create_listing(body: ListingInput, orders: OrdersDep) -> Listing:
    """A new listing, unpublished, after every other one. Nothing is checked against
    the template until it is published."""
    return await asyncio.to_thread(orders.listings.create, body)


# Before `/listings/{listing_id}`, which would match `order` too.
@router.put("/listings/order", response_model=list[Listing], summary="Order the listings")
async def order_listings(body: ListingOrder, orders: OrdersDep) -> list[Listing]:
    """The listings in the order of ``ids``, which must name each one once (409
    ``listing_changed`` otherwise: the list was read before one was made or deleted)."""
    return await asyncio.to_thread(orders.listings.reorder, body.ids)


@router.put("/listings/{listing_id}", response_model=Listing, summary="Edit a storefront listing")
async def update_listing(listing_id: str, body: ListingUpdate, orders: OrdersDep) -> Listing:
    """The listing replaced whole, and unpublished: a change is not live until the shop
    publishes it again. 409 ``listing_changed`` unless ``updated_at`` is the listing's."""
    return await asyncio.to_thread(
        orders.listings.update, listing_id, body.listing, expected_updated_at=body.updated_at
    )


@router.post(
    "/listings/{listing_id}/publish",
    response_model=Listing,
    summary="Publish a storefront listing",
)
async def publish_listing(listing_id: str, orders: OrdersDep, state: StateDep) -> Listing:
    """The listing checked against its commit's schema and the price book, then put on
    the public site with what a visitor's render needs (the schema, the source
    snapshot, the media). 409 ``storefront_needs_s3`` unless the store is S3; 422 names
    the fault (``listing_param_unknown``, ``listing_bounds_outside_schema``,
    ``listing_fixed_invalid``, ``listing_colour_unknown``, ``listing_media_unknown``,
    ``listing_reference``, ``listing_pipeline_unsupported``)."""
    return await publish(state, orders.listings, listing_id)


@router.post(
    "/listings/{listing_id}/unpublish",
    response_model=Listing,
    summary="Unpublish a storefront listing",
)
async def unpublish_listing(listing_id: str, orders: OrdersDep) -> Listing:
    """The listing taken off the public site; what publish captured is let go."""
    return await unpublish(orders.listings, listing_id)


@router.delete(
    "/listings/{listing_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a storefront listing",
)
async def delete_listing(listing_id: str, orders: OrdersDep) -> Response:
    """Remove an unpublished listing (409 ``listing_published`` while it is published)."""
    await asyncio.to_thread(orders.listings.delete, listing_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
