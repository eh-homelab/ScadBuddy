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

from fastapi import APIRouter, File, Path, Query, Response, UploadFile, status
from pydantic import BaseModel, Field

from scadbuddy.api.deps import StateDep
from scadbuddy.core.authorship import by_a_person
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.storefront.file_listings import pinned_plate_image, upload_picture
from scadbuddy.orders.storefront.listings import (
    change_media,
    listing_not_found,
    publish,
    unpublish,
)
from scadbuddy.orders.storefront.mail import MailConnectionUnsetError
from scadbuddy.orders.storefront.prepare import (
    PrepareProgress,
    PrepareStarted,
    progress,
    start_run,
    warm_default,
)
from scadbuddy.orders.storefront.wire import (
    Listing,
    ListingInput,
    ListingMediaUpdate,
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


@router.put(
    "/listings/{listing_id}/media",
    response_model=Listing,
    summary="Set a storefront listing's pictures",
)
async def set_listing_media(
    listing_id: str, body: ListingMediaUpdate, orders: OrdersDep, state: StateDep
) -> Listing:
    """The listing's pictures (#6213): ``media_ids``, images of its template (and any
    video the listing already shows, #6290) in the order to show them, and nothing else
    changed. A draft stays a draft; a published listing
    is published again at once and stays live. 409 ``listing_changed`` unless
    ``updated_at`` is the listing's; 422 ``listing_media_unknown``,
    ``listing_media_not_image`` or ``listing_media_duplicate`` (``name`` is the id); a
    republish refused answers as ``POST …/publish`` does, with ``pictures_saved: true``:
    the pictures are kept and the public site shows what was last published."""
    listing = await change_media(
        state, orders.listings, listing_id, body.media_ids, expected_updated_at=body.updated_at
    )
    # A republish is a new version: its variants have new keys (#6922).
    await warm_default(state, listing)
    return listing


class UploadedPicture(BaseModel):
    """A picture the shop uploaded to a file listing: its id for ``media_ids``."""

    id: str


@router.post(
    "/listings/{listing_id}/pictures",
    response_model=UploadedPicture,
    summary="Upload a picture to a file listing",
)
async def post_listing_picture(
    listing_id: str, orders: OrdersDep, state: StateDep, file: Annotated[UploadFile, File()]
) -> UploadedPicture:
    """A picture for a file listing (#6710 §5.4), which has no template media to upload
    into: an image (PNG, JPEG or WebP by its bytes; 415 otherwise) of at most 10 MB (413),
    kept for the listing and answered as its id ``upload:<sha256>``, which the editor adds
    to ``media_ids``. A save or a pictures change that no longer names it lets it go, once
    an hour has passed since its upload. 404 ``listing_not_found``; 422
    ``listing_source_invalid`` for a template listing."""
    return UploadedPicture(id=await upload_picture(state, orders.listings, listing_id, file))


@router.get(
    "/listings/{listing_id}/plates/{plate}.png",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
    summary="A plate picture of a file listing's published version",
)
async def get_listing_plate(
    listing_id: str, plate: Annotated[int, Path(ge=1, le=9999)], orders: OrdersDep, state: StateDep
) -> Response:
    """Plate ``plate``'s picture in the copy of the file the listing pins (#6710 §9.3):
    the picture publish copies for ``plate:<n>``, also once the file is gone from the
    library. 404 for a listing with no pin, or a plate with no picture."""
    image = await pinned_plate_image(state, orders.listings, listing_id, plate)
    if image is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no picture of plate {plate}")
    return Response(content=image, media_type="image/png")


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
    ``listing_media_unknown``, ``listing_material_not_allowed``,
    ``listing_locked_colours_unprintable``: no allowed material offers every locked colour
    and a choice of each offered colour,
    ``listing_reference``, ``listing_pipeline_unsupported``,
    ``listing_material_unsliceable``: the shop slice profile has no filament type for a material
    of the listing's filaments, #6153); 409 ``listing_publishing``
    while another publish of it runs; 503 ``listing_publish_busy`` while this process
    runs as many publishes as it allows. Once published, the shop renders the default
    variant and starts its storefront slice price (#6922, `prepare.py` `warm_default`),
    so its price is there before a visitor asks."""
    listing = await publish(state, orders.listings, listing_id)
    await warm_default(state, listing)
    return listing


@router.get(
    "/listings/{listing_id}/prepare",
    response_model=PrepareProgress,
    summary="A storefront listing's colour combinations and how far they are prepared",
)
async def get_prepare(listing_id: str, orders: OrdersDep, state: StateDep) -> PrepareProgress:
    """How many colour combinations the published listing has (``count``: the render keys of
    its combinations, each of one allowed material, #6258; 0 with no colour parameter), the cap
    (``storefront_prepare_cap``), and how many have a done (``ready``), an open
    (``rendering``) and a failed render (#6237). ``running`` is whether the listing's
    prepare run is open, and ``failed_to_start`` how many combinations its last run could
    not submit and that have no done render since (#6299 review). Both are read only for
    a listing that can be prepared; when Temporal does not answer, ``running`` is false
    and ``failed_to_start`` 0. Over the cap only the count is computed. 404
    ``listing_not_found``, 409 ``listing_not_published``."""
    return await progress(state, orders, listing_id)


@router.post(
    "/listings/{listing_id}/prepare",
    response_model=PrepareStarted,
    summary="Prepare every colour combination of a storefront listing",
)
async def post_prepare(listing_id: str, orders: OrdersDep, state: StateDep) -> PrepareStarted:
    """Start the listing's prepare run (#6237): a render of each colour combination of the
    published listing, at the values a visitor's page sends for it, so a visitor's render
    of it reuses the job. Every other parameter stays at its default. The run renders a few
    at a time (`PREPARE_IN_FLIGHT`), at the default priority (#6299 review), so it never
    holds up a visitor's render or the shop's own. Once they settle, it starts each
    combination's storefront slice price, one at a time under
    ``storefront_slices_concurrent`` (#6922); a combination priced before is not sliced
    again. It answers at once: ``started`` is false when a run was open already, and this
    POST joined it. 404 ``listing_not_found``, 409
    ``listing_not_published``, 422 ``listing_no_colours``, 422
    ``listing_combinations_over_cap`` (with ``count`` and ``cap``), 503
    ``render_unavailable``.

    Prepared renders follow ``job_ttl`` as any other render does: with ``job_ttl`` 0
    (#6244) they are kept, else the prune takes each one a ``job_ttl`` after its finish or
    its last reuse. They carry no keep mark."""
    return await start_run(state, orders, listing_id)


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
