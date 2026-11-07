"""The storefront edge's client of its CRUD routes (#5282, plan "Trust model").

The edge holds no database: it calls ``/api/v1/internal/storefront/…`` with its
``storefront`` service credential, which it exchanges for its proof (`exchange_source`).
`CrudClient` retries a missing route, a 5xx and a transport failure for 30 s and raises
`CrudRefusedError` for a CRUD route's 4xx; the edge answers a public one with its status,
problem and ``Retry-After``, and any other with a bare 502 (`storefront/app.py`). Each call
sends the visitor id the edge verified (`VISITOR_HEADER`) and the browser's address
(`CLIENT_HEADER`); a write also sends its ``Idempotency-Key``, the same on each retry.

It imports no database code: the edge's import test (`tests/storefront/test_edge.py`)
checks it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from scadbuddy.auth.service_auth import ExchangeSettings, ServiceAuth, exchange_source
from scadbuddy.crud.client import CrudClient, wait_for_areas
from scadbuddy.crud.wire import CRUD_PREFIXES
from scadbuddy.storefront.wire import CLIENT_HEADER, VISITOR_HEADER

#: The CRUD areas the edge calls (`api/crud_areas.py` `CRUD_AREAS["storefront"]`): each
#: task that adds an area to both sides adds it here.
EDGE_AREAS: Final[frozenset[str]] = frozenset()
BASE: Final = f"/api/v1{CRUD_PREFIXES['storefront']}"


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
        json: Any = None,
        key: str | None = None,
    ) -> Any:
        response = await self._crud.request(
            method,
            f"{BASE}{path}",
            json=json,
            idempotency_key=key,
            headers={VISITOR_HEADER: visitor_id, CLIENT_HEADER: client},
        )
        return response.json()

    async def listings(self, *, visitor_id: str, client: str) -> Any:
        """``GET …/storefront/listings``: the published listings (Task 6 adds the route)."""
        return await self._call("GET", "/listings", visitor_id=visitor_id, client=client)
