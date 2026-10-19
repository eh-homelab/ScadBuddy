"""The routes the slicer worker downloads its input and uploads its output through (plan
2026-10-16-native-slicer-rust-worker Task 10): it holds no store credentials, only the
`TransferUrl`s `slicing/transfer.py` mints. Not a client API: hidden from the schema.

Each request carries a token for its one method and key (`slicing/transfer_token.py`),
checked before the key or the store is looked at, so a token never probes a key it was
not minted for and a caller without one cannot tell which `store_backend` is in use. A
missing, malformed, tampered or expired one is 403 (nothing to challenge, so not 401),
which the worker does not retry. Without a content store (`store_backend` local) a valid
token's request is 404. Neither ever redirects: the worker follows none. Never log the
header."""

from __future__ import annotations

import asyncio
import re
import tempfile
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import StreamingResponse

from scadbuddy.api.deps import StateDep
from scadbuddy.api.limits import RouteLimit
from scadbuddy.bambuddy.preview import MAX_SLICED_BYTES
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import settings_kek
from scadbuddy.slicing.transfer import (
    INTERNAL_BLOBS,
    PENDING_PREFIX,
    TRANSFER_HEADER,
    TransferPuts,
)
from scadbuddy.slicing.transfer_token import Method, TokenError, transfer_key, verify
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobScope

router = APIRouter(prefix=INTERNAL_BLOBS.removeprefix("/api/v1"), include_in_schema=False)

#: For ``main.py``'s `BodySizeGate`: every PUT here, whatever its content type, refused
#: on its Content-Length or, without one, as the body streams, so a worker's token
#: cannot fill the spool before the store's own room check, which runs on the whole body.
PUT_ROUTE_LIMIT = RouteLimit(
    "PUT",
    re.compile(rf"^{re.escape(INTERNAL_BLOBS)}/"),
    MAX_SLICED_BYTES,
    "a sliced 3MF",
    "fixed, bambuddy/preview.py MAX_SLICED_BYTES",
)

TokenHeader = Annotated[str | None, Header(alias=TRANSFER_HEADER)]


def _content(state: StateDep, key: str) -> ContentStore:
    content = state.store.content
    if content is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"blob {key} not found", key=key)
    return content


def _check(state: StateDep, token: str | None, *, method: Method, key: str) -> None:
    kek = settings_kek(state.settings.secret_key_file)
    try:
        if token is None or kek is None:
            raise TokenError("no transfer token")
        verify(transfer_key(kek), token, method=method, blob_key=key, now=datetime.now(UTC))
    except TokenError:
        raise ApiError(status.HTTP_403_FORBIDDEN, "transfer refused") from None


@router.get("/{key:path}", response_class=StreamingResponse)
async def get_blob(key: str, state: StateDep, token: TokenHeader = None) -> StreamingResponse:
    _check(state, token, method="GET", key=key)
    content = _content(state, key)
    stat = await content.stat(key)
    if stat is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"blob {key} not found", key=key)
    return StreamingResponse(
        content.get(stat.ref),
        media_type="application/octet-stream",
        headers={"Content-Length": str(stat.ref.size)},
    )


@router.put("/{key:path}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def put_blob(
    key: str, request: Request, state: StateDep, token: TokenHeader = None
) -> Response:
    _check(state, token, method="PUT", key=key)
    content = _content(state, key)
    if not key.startswith(PENDING_PREFIX):
        raise ApiError(status.HTTP_404_NOT_FOUND, f"blob {key} not found", key=key)
    await asyncio.to_thread(content.spool.mkdir, parents=True, exist_ok=True)
    # Streamed to a file under the spool, never held whole: a sliced 3MF is large.
    with tempfile.TemporaryFile(dir=content.spool) as body:
        async for chunk in request.stream():
            await asyncio.to_thread(body.write, chunk)
        await asyncio.to_thread(body.seek, 0)
        ref = await content.put("slice", body, name="sliced.3mf", scope=BlobScope(folder="work"))
    await asyncio.to_thread(
        TransferPuts(state.projection.pool).record,
        key,
        key=f"slice-{ref.sha256}",
        sha256=ref.sha256,
        size=ref.size,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
