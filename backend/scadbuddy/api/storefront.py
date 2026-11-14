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
import dataclasses
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field

from scadbuddy.api.deps import StateDep
from scadbuddy.core.authorship import by_a_person
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.storefront.listings import listing_not_found, publish, unpublish
from scadbuddy.orders.storefront.mail import MailConnectionUnsetError
from scadbuddy.orders.storefront.wire import (
    Listing,
    ListingInput,
    ListingMove,
    ListingOrder,
    ListingPage,
    ListingQuery,
    ListingUpdate,
)
from scadbuddy.payments.credentials import KekMissingError

router = APIRouter(prefix="/storefront", tags=["storefront"])

PROBLEMS = "https://scadbuddy.dev/problems/storefront/"


class MailPasswordStatus(BaseModel):
    """Whether the storefront's SMTP password is set; never the password."""

    set: bool


class MailPassword(BaseModel):
    password: str = Field(min_length=1, max_length=1000, repr=False)


class MailStatusView(BaseModel):
    """The storefront mail as the shop sees it (#5283): whether a password is set and for
    which connection (``user@host:port``), and the last failure of the server or its
    setup, which no mail was charged for. Never the password."""

    password_set: bool
    password_for: str | None
    server_error: str | None
    server_error_at: datetime | None


def _by_a_person() -> None:
    # Ruling R2: the password is a person's to set or forget, whatever the auth mode.
    if not by_a_person():
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "Only a person sets or forgets the storefront mail password.",
            type_=PROBLEMS + "person_only",
        )


@router.get("/listings", response_model=ListingPage, summary="The storefront's listings")
async def list_listings(orders: OrdersDep, query: Annotated[ListingQuery, Query()]) -> ListingPage:
    """A page of the listings, published or not (#6157): those whose title or slug
    holds ``q``, of ``status``, of any ``slug`` and with a colour of any ``material``,
    sorted by ``sort`` (``position`` is the order the public site shows them), and how
    many match in all. A row holds only what the list shows; ``GET
    …/listings/{listing_id}`` reads one listing whole."""
    return await asyncio.to_thread(orders.listings.page, query)


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


@router.get("/listings/{listing_id}", response_model=Listing, summary="A storefront listing")
async def get_listing(listing_id: str, orders: OrdersDep) -> Listing:
    """One listing whole, for its editor (#6157); 404 ``listing_not_found``."""
    found = await asyncio.to_thread(orders.listings.get, listing_id)
    if found is None:
        raise listing_not_found(listing_id)
    return found


@router.post(
    "/listings/{listing_id}/move", response_model=Listing, summary="Move a storefront listing"
)
async def move_listing(listing_id: str, body: ListingMove, orders: OrdersDep) -> Listing:
    """The listing put at ``place`` (from 1; a place past the end puts it last) in the
    order the public site shows them, the others keeping their order around it (#6157).
    Unlike ``PUT …/listings/order`` it names one listing, so it suits a long list. 404
    ``listing_not_found``."""
    return await asyncio.to_thread(orders.listings.move, listing_id, body.place)


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
    ``listing_fixed_invalid``, ``listing_default_invalid``, ``listing_colour_unknown``,
    ``listing_media_unknown``,
    ``listing_reference``, ``listing_pipeline_unsupported``,
    ``listing_material_unsliceable``: the shop slice profile has no spool for a material
    of the listing's filaments, #6153); 409 ``listing_publishing``
    while another publish of it runs; 503 ``listing_publish_busy`` while this process
    runs as many publishes as it allows."""
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


@router.get("/mail", response_model=MailStatusView, summary="The storefront mail's state")
async def get_mail(orders: OrdersDep) -> MailStatusView:
    status_ = await asyncio.to_thread(orders.mail.status)
    return MailStatusView(**dataclasses.asdict(status_))


@router.get(
    "/mail/password",
    response_model=MailPasswordStatus,
    summary="Whether the storefront's SMTP password is set",
)
async def get_mail_password(orders: OrdersDep) -> MailPasswordStatus:
    """The password itself is never read back (#5283)."""
    status_ = await asyncio.to_thread(orders.mail.status)
    return MailPasswordStatus(set=status_.password_set)


@router.put(
    "/mail/password",
    response_model=MailPasswordStatus,
    summary="Set the storefront's SMTP password",
    description=(
        "Sealed under the CRUD service's KEK (#5283, spec 2026-11-02 §5.7), bound to the"
        " SMTP host, port and user set now, and never read back. A person's only (403"
        " `person_only`). 409 `kek_missing` without SCADBUDDY_SECRET_KEY_FILE; 409"
        " `mail_connection_unset` before the host and the user are set."
    ),
)
async def put_mail_password(body: MailPassword, orders: OrdersDep) -> MailPasswordStatus:
    _by_a_person()
    try:
        await asyncio.to_thread(orders.mail.set_password, body.password)
    except KekMissingError:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "the storefront mail password needs SCADBUDDY_SECRET_KEY_FILE",
            type_=PROBLEMS + "kek_missing",
        ) from None
    except MailConnectionUnsetError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT, str(error), type_=PROBLEMS + "mail_connection_unset"
        ) from None
    # Mail that waited for the login goes now.
    orders.mail.kick()
    return MailPasswordStatus(set=True)


@router.delete(
    "/mail/password",
    response_model=MailPasswordStatus,
    summary="Forget the storefront's SMTP password",
    description="A person's only (403 `person_only`).",
)
async def delete_mail_password(orders: OrdersDep) -> MailPasswordStatus:
    _by_a_person()
    await asyncio.to_thread(orders.mail.clear_password)
    return MailPasswordStatus(set=False)
