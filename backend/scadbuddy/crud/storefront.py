"""The storefront edge's client of its CRUD routes (#5282, plan "Trust model").

The edge holds no database: it calls ``/api/v1/internal/crud/storefront/…`` with its
``storefront`` service credential, which it exchanges for its proof (`exchange_source`).
`CrudClient` retries a missing route, a 5xx and a transport failure for 30 s and raises
`CrudRefusedError` for a CRUD route's 4xx; the edge answers a public one with its status,
problem and ``Retry-After``, and any other with a bare 502 (`storefront/app.py`). Each call
sends the visitor id the edge verified (`VISITOR_HEADER`) and the browser's address
(`CLIENT_HEADER`); a write also sends its ``Idempotency-Key``, the same on each retry. A
render's body, a draft item's and a submit's go as the visitor sent them, unread; a render's preview
comes back as a stream (`CrudStream`), which the edge relays without holding it whole. A
design request goes as multipart made from the files the edge spooled, read a chunk at a
time in a thread, in one call that is never retried (its body cannot be sent twice); each
file's name is escaped (`escape_filename`), and the CRUD side decodes it.
An account call and a submit (#5283) also send the session token of the edge's verified
``sb_session`` cookie (`SESSION_HEADER`), when there is one. A sign-in is sent once, never
retried (a code is single use), and its new session's token comes back in that header,
never in the body.

It imports no database code: the edge's import test (`tests/storefront/test_edge.py`)
checks it.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Sequence
from typing import Any, Final

import anyio
import httpx
from starlette.datastructures import UploadFile

from scadbuddy.auth.service_auth import ExchangeSettings, ServiceAuth, exchange_source
from scadbuddy.crud.client import CrudClient, CrudStream, Upload, wait_for_areas
from scadbuddy.crud.wire import PREFIX
from scadbuddy.storefront.wire import (
    CLIENT_HEADER,
    SESSION_HEADER,
    TURNSTILE_VERIFY_SECONDS,
    VISITOR_HEADER,
    escape_filename,
)

#: The CRUD areas the edge calls (`api/crud_areas.py` `CRUD_AREAS["storefront"]`): each
#: task that adds an area to both sides adds it here. ``pay`` (#4960) is read by the
#: ``/pay/{token}`` page that #5283 phase 4 builds; the API serves it whole already.
#: ``turnstile`` is the bot check (#5283, `storefront/turnstile.py`).
EDGE_AREAS: Final[frozenset[str]] = frozenset(
    {"listings", "renders", "drafts", "requests", "pay", "turnstile", "accounts"}
)
#: The site key's read: the page's CSP waits on it, so it gives up fast and the edge
#: keeps its last value (`storefront/turnstile.py` `SiteKey`).
SITE_KEY_BUDGET: Final = 2.0
BASE: Final = f"/api/v1{PREFIX}/storefront"
#: A design request's one call: the CRUD side answers only once every file is checked
#: and stored.
UPLOAD_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=300.0, write=300.0, pool=5.0)
_CHUNK: Final = 1 << 20


async def _multipart(boundary: str, text: str, files: Sequence[UploadFile]) -> AsyncIterator[bytes]:
    """The request as multipart, each file read from its start in a thread."""
    dash = f"--{boundary}\r\n".encode()
    yield dash + b'Content-Disposition: form-data; name="text"\r\n\r\n'
    yield text.encode() + b"\r\n"
    for upload in files:
        name = escape_filename(upload.filename or "file")
        yield (
            dash
            + (
                f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
        )
        await anyio.to_thread.run_sync(upload.file.seek, 0)
        while chunk := await anyio.to_thread.run_sync(upload.file.read, _CHUNK):
            yield chunk
        yield b"\r\n"
    yield f"--{boundary}--\r\n".encode()


def _headers(visitor_id: str, client: str, session: str | None = None) -> dict[str, str]:
    headers = {VISITOR_HEADER: visitor_id, CLIENT_HEADER: client}
    if session is not None:
        headers[SESSION_HEADER] = session
    return headers


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
        session: str | None = None,
    ) -> Any:
        response = await self._crud.request(
            method,
            f"{BASE}{path}",
            content=content,
            idempotency_key=key,
            headers=_headers(visitor_id, client, session),
        )
        return response.json()

    async def account_code(self, body: bytes, *, visitor_id: str, client: str) -> None:
        """``POST …/storefront/account/code``: the visitor's JSON body as it came. Sent
        once, never retried (``budget=0``): a replay would make and mail a second code and
        count against the client's limits."""
        await self._crud.request(
            "POST",
            f"{BASE}/account/code",
            content=body,
            budget=0.0,
            headers=_headers(visitor_id, client),
        )

    async def account_verify(
        self, body: bytes, *, visitor_id: str, client: str, session: str | None
    ) -> tuple[Any, str | None]:
        """``POST …/storefront/account/verify``: the visitor's JSON body as it came, and
        the session the browser held (which the sign-in ends). The account and the new
        session's token, from `SESSION_HEADER` of the answer. Sent once, never retried
        (``budget=0``): a code is single use, so a replay of a sign-in that committed
        would only fail."""
        response = await self._crud.request(
            "POST",
            f"{BASE}/account/verify",
            content=body,
            budget=0.0,
            headers=_headers(visitor_id, client, session),
        )
        return response.json(), response.headers.get(SESSION_HEADER)

    async def account_sign_out(self, *, visitor_id: str, client: str, session: str) -> None:
        """``POST …/storefront/account/sign-out``: the session's row is deleted."""
        await self._crud.request(
            "POST",
            f"{BASE}/account/sign-out",
            headers=_headers(visitor_id, client, session),
        )

    async def account(self, *, visitor_id: str, client: str, session: str) -> Any:
        """``GET …/storefront/account``: the session's account, or None."""
        return await self._call(
            "GET", "/account", visitor_id=visitor_id, client=client, session=session
        )

    async def account_orders(self, *, visitor_id: str, client: str, session: str) -> Any:
        """``GET …/storefront/account/orders``: what the session's account sees."""
        return await self._call(
            "GET", "/account/orders", visitor_id=visitor_id, client=client, session=session
        )

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

    async def submit(
        self, body: bytes, *, key: str, visitor_id: str, client: str, session: str | None
    ) -> Any:
        """``POST …/storefront/draft/submit``: the visitor's JSON body as it came, unread,
        the request's ``Idempotency-Key`` and the session, if any, whose account submits."""
        return await self._call(
            "POST",
            "/draft/submit",
            visitor_id=visitor_id,
            client=client,
            content=body,
            key=key,
            session=session,
        )

    async def requests_enabled(
        self, *, visitor_id: str, client: str, key: str | None = None
    ) -> bool:
        """``GET …/storefront/requests-enabled``: whether "Ask for a design" is on; with a
        request's ``key``, also whether that key holds a committed request."""
        answer = await self._call(
            "GET", "/requests-enabled", visitor_id=visitor_id, client=client, key=key
        )
        return bool(answer.get("enabled")) if isinstance(answer, dict) else False

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
        multipart made as it is sent, each file read a chunk at a time in a thread. Its
        body cannot be sent twice, so it is sent once (`UPLOAD_TIMEOUT`), never retried:
        a failure raises `CrudUnavailableError` at once."""
        boundary = secrets.token_hex(16)
        response = await self._crud.request(
            "POST",
            f"{BASE}/draft/requests",
            idempotency_key=key,
            headers={VISITOR_HEADER: visitor_id, CLIENT_HEADER: client},
            timeout=UPLOAD_TIMEOUT,
            upload=Upload(
                f"multipart/form-data; boundary={boundary}",
                _multipart(boundary, text, files),
            ),
        )
        return response.json()

    async def turnstile_site_key(self) -> str | None:
        """``GET …/storefront/turnstile``: the bot check's site key, or None while it is
        off. Read outside any visitor, so it sends no visitor or client header."""
        response = await self._crud.request(
            "GET", f"{BASE}/turnstile", budget=SITE_KEY_BUDGET, timeout=SITE_KEY_BUDGET
        )
        answer = response.json()
        key = answer.get("site_key") if isinstance(answer, dict) else None
        return key if isinstance(key, str) and key else None

    async def turnstile_verify(
        self, token: str | None, action: str, *, visitor_id: str, client: str
    ) -> str:
        """``POST …/storefront/turnstile/verify``: the CRUD side's verdict on the
        browser's token for ``action`` (``off``, ``pass``, ``fail``, ``unavailable``).
        Sent once, never retried: a token passes one Siteverify call only, so a retry
        after a lost answer would fail a good token. A transport failure or a timeout
        (`TURNSTILE_VERIFY_SECONDS`) raises `CrudUnavailableError`."""
        response = await self._crud.request(
            "POST",
            f"{BASE}/turnstile/verify",
            json={"token": token, "action": action},
            headers={VISITOR_HEADER: visitor_id, CLIENT_HEADER: client},
            timeout=TURNSTILE_VERIFY_SECONDS,
            budget=0.0,
        )
        answer = response.json()
        verdict = answer.get("verdict") if isinstance(answer, dict) else None
        return verdict if isinstance(verdict, str) else "unavailable"

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
