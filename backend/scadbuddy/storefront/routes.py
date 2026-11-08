"""The edge's routes: ``/healthz`` and the visitor routes under `STOREFRONT_PREFIX`, each
of which forwards to the CRUD client with the verified visitor id and the browser's
address. `app.py` answers a CRUD refusal."""

from __future__ import annotations

import re
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request

from scadbuddy.core.problems import ApiError
from scadbuddy.core.proxies import client_address
from scadbuddy.crud.storefront import StorefrontCrud
from scadbuddy.storefront.settings import STOREFRONT_PREFIX
from scadbuddy.storefront.wire import STOREFRONT_PROBLEMS

#: A listing id (``gen_random_uuid()::text``): any other never reaches the CRUD side.
LISTING_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

router = APIRouter()
visitor_router = APIRouter(prefix=STOREFRONT_PREFIX)


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _crud(request: Request) -> StorefrontCrud:
    crud: StorefrontCrud = request.app.state.crud
    return crud


def _visitor(request: Request) -> str:
    """The id `VisitorCookie` verified or minted for this request."""
    visitor_id: str = request.state.visitor_id
    return visitor_id


def _client(request: Request) -> str:
    """The browser's address: a trusted proxy's ``X-Forwarded-For``, else the peer."""
    trusted = request.app.state.trusted_proxies
    peer = request.client.host if request.client else None
    address = client_address(peer, request.headers.get("x-forwarded-for"), trusted)
    if address is None:
        raise ApiError(400, "The request has no client address.")
    return address


Crud = Annotated[StorefrontCrud, Depends(_crud)]
Visitor = Annotated[str, Depends(_visitor)]
Client = Annotated[str, Depends(_client)]


@visitor_router.get("/listings")
async def listings(crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    return await crud.listings(visitor_id=visitor_id, client=client)


@visitor_router.get("/listings/{listing_id}")
async def listing(listing_id: str, crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    if LISTING_ID.fullmatch(listing_id) is None:
        raise ApiError(
            404, "There is no such listing.", type_=f"{STOREFRONT_PROBLEMS}listing_not_found"
        )
    return await crud.listing(listing_id, visitor_id=visitor_id, client=client)
