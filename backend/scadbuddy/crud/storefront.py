"""The storefront edge's client of its CRUD routes (#5282, plan "Trust model").

The edge holds no database: it calls ``/api/v1/internal/crud/storefront/…`` with its
``storefront`` service credential, which it exchanges for its proof (`exchange_source`).
`CrudClient` retries a missing route, a 5xx and a transport failure for 30 s and raises
`CrudRefusedError` for a CRUD route's 4xx; the edge answers a public one with its status,
problem and ``Retry-After``, and any other with a bare 502 (`storefront/app.py`). Each call
sends the visitor id the edge verified (`VISITOR_HEADER`) and the browser's address
(`CLIENT_HEADER`); a write also sends its ``Idempotency-Key``, the same on each retry. A
render's body, and a draft item's, goes as the visitor sent it, unread; a render's preview
comes back as a stream (`CrudStream`), which the edge relays without holding it whole. A
design request goes as multipart from the files the edge spooled, read a chunk at a time
and from their start again on each retry.

It imports no database code: the edge's import test (`tests/storefront/test_edge.py`)
checks it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Final

from starlette.datastructures import UploadFile

from scadbuddy.auth.service_auth import ExchangeSettings, ServiceAuth, exchange_source
from scadbuddy.crud.client import CrudClient, CrudStream, wait_for_areas
from scadbuddy.crud.wire import PREFIX
from scadbuddy.storefront.wire import CLIENT_HEADER, VISITOR_HEADER

#: The CRUD areas the edge calls (`api/crud_areas.py` `CRUD_AREAS["storefront"]`): each
#: task that adds an area to both sides adds it here.
EDGE_AREAS: Final[frozenset[str]] = frozenset({"listings", "renders", "drafts", "requests"})
BASE: Final = f"/api/v1{PREFIX}/storefront"


class StorefrontCrud:
    """One async method per CRUD route, each for one visitor and client address."""

    def __init__(self, crud: CrudClient) -> None:
        self._crud = crud

    @classmethod
    def from_settings(cls, settings: ExchangeSettings) -> StorefrontCrud:
        """The edge's client, with the credential it exchanges its proof for."""
        if settings.api_internal_url is None:
            raise ValueError("SCADBUDDY_API_INTERNAL_URL is required for the storefront")
        auth = ServiceAuth(exchange_source("storefront", settings))
        return cls(CrudClient(settings.api_internal_url, "storefront", auth))

    async def aclose(self) -> None:
        await self._crud.aclose()

    async def wait_for_areas(self, stop: asyncio.Event) -> bool:
        """Waits until the API serves every one of `EDGE_AREAS`; False when ``stop`` came
        first."""
        return await wait_for_areas(self._crud, "storefront", EDGE_AREAS, stop)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        visitor_id: str,
        client: str,
        content: bytes | None = None,
        key: str | None = None,
    ) -> Any:
        response = await self._crud.request(
            method,
            f"{BASE}{path}",
            content=content,
            idempotency_key=key,
            headers={VISITOR_HEADER: visitor_id, CLIENT_HEADER: client},
        )
        return response.json()

    async def listings(self, *, visitor_id: str, client: str) -> Any:
        """``GET …/storefront/listings``: the published listings."""
        return await self._call("GET", "/listings", visitor_id=visitor_id, client=client)

    async def listing(self, listing_id: str, *, visitor_id: str, client: str) -> Any:
        """``GET …/storefront/listings/{id}``: one published listing. ``listing_id`` must
        be a listing id (`storefront/routes.py` checks it), so it cannot leave the path."""
        return await self._call(
            "GET", f"/listings/{listing_id}", visitor_id=visitor_id, client=client
        )

    async def render(self, body: bytes, *, key: str, visitor_id: str, client: str) -> Any:
        """``POST …/storefront/renders``: the visitor's JSON body as it came, unread, and
        the request's ``Idempotency-Key``."""
        return await self._call(
            "POST", "/renders", visitor_id=visitor_id, client=client, content=body, key=key
        )

    async def render_status(self, job_id: str, *, visitor_id: str, client: str) -> Any:
        """``GET …/storefront/renders/{id}``. ``job_id`` must be a job id
        (`storefront/routes.py` checks it), so it cannot leave the path."""
        return await self._call("GET", f"/renders/{job_id}", visitor_id=visitor_id, client=client)

    async def render_estimate(self, job_id: str, *, visitor_id: str, client: str) -> Any:
        """``GET …/storefront/renders/{id}/estimate``: the render's rough price."""
        return await self._call(
            "GET", f"/renders/{job_id}/estimate", visitor_id=visitor_id, client=client
        )

    async def draft(self, *, visitor_id: str, client: str) -> Any:
        """``GET …/storefront/draft``: the visitor's draft order, or None."""
        return await self._call("GET", "/draft", visitor_id=visitor_id, client=client)

    async def add_item(self, body: bytes, *, key: str, visitor_id: str, client: str) -> Any:
        """``POST …/storefront/draft/items``: the visitor's JSON body as it came, unread,
        and the request's ``Idempotency-Key``."""
        return await self._call(
            "POST", "/draft/items", visitor_id=visitor_id, client=client, content=body, key=key
        )

    async def remove_item(self, item_id: str, *, key: str, visitor_id: str, client: str) -> Any:
        """``DELETE …/storefront/draft/items/{id}``. ``item_id`` must be an item id
        (`storefront/routes.py` checks it), so it cannot leave the path."""
        return await self._call(
            "DELETE", f"/draft/items/{item_id}", visitor_id=visitor_id, client=client, key=key
        )

    async def add_request(
        self,
        text: str,
        files: Sequence[UploadFile],
        *,
        key: str,
        visitor_id: str,
        client: str,
    ) -> Any:
        """``POST …/storefront/draft/requests``: ``text`` and each spooled file, as
        multipart, with the request's ``Idempotency-Key``."""
        response = await self._crud.request(
            "POST",
            f"{BASE}/draft/requests",
            idempotency_key=key,
            headers={VISITOR_HEADER: visitor_id, CLIENT_HEADER: client},
            data={"text": text},
            files=[
                ("files", (f.filename or "file", f.file, "application/octet-stream")) for f in files
            ],
        )
        return response.json()

    async def render_preview(
        self, job_id: str, *, plate: int | None, visitor_id: str, client: str
    ) -> CrudStream:
        """``GET …/storefront/renders/{id}/preview.glb``, its body unread: the caller
        reads it as a stream, then closes it."""
        return await self._crud.stream(
            "GET",
            f"{BASE}/renders/{job_id}/preview.glb",
            params=None if plate is None else {"plate": str(plate)},
            headers={VISITOR_HEADER: visitor_id, CLIENT_HEADER: client},
        )
