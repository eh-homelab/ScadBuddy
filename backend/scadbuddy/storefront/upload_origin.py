"""The origin the browser PUTs a customer's model files to (#6133, spec 2026-11-13 §6.4).

The files go straight to the store's bucket by presigned PUTs, so the page's CSP must name
the bucket's origin in ``connect-src`` (`static.py`). The edge does not know the store; it
reads the origin from the CRUD side (``GET …/storefront/uploads/config``, which takes it
from a presigned URL itself), at most once per `ORIGIN_TTL`, and keeps the last value
through a failed read. A value that is not a bare ``http``/``https`` origin is never put
in a CSP. It imports no database code: the edge imports it."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Final
from urllib.parse import urlsplit

from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.storefront import SITE_KEY_BUDGET, StorefrontCrud

logger = logging.getLogger(__name__)

#: How long a read origin is used before it is read again.
ORIGIN_TTL: Final = 300.0
#: How long a failed read waits before the next.
ORIGIN_RETRY: Final = 30.0


def bare_origin(value: str | None) -> str | None:
    """``value`` when it is ``scheme://host[:port]`` with scheme ``http`` or ``https`` and
    nothing else, else None."""
    if not value:
        return None
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018  (reading it checks it)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.path or parts.query or parts.fragment or "@" in parts.netloc:
        return None
    if any(c.isspace() or c in ";,'\"" for c in value):
        return None
    return f"{parts.scheme}://{parts.netloc}"


class UploadOrigin:
    """The CRUD side's upload origin, cached; None until one read succeeds or while the
    store is not S3 (the CRUD side refuses, and uploads are off)."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._read: tuple[float, str | None] | None = None
        self._failed_at: float | None = None
        self._reading = asyncio.Lock()

    def _cached(self, now: float) -> tuple[bool, str | None]:
        last = None if self._read is None else self._read[1]
        if self._read is not None and now - self._read[0] < ORIGIN_TTL:
            return True, last
        if self._failed_at is not None and now - self._failed_at < ORIGIN_RETRY:
            return True, last
        return False, None

    async def get(self, crud: StorefrontCrud) -> str | None:
        hit, origin = self._cached(self._clock())
        if hit:
            return origin
        async with self._reading:
            now = self._clock()
            hit, origin = self._cached(now)
            if hit:
                return origin
            try:
                read = await asyncio.wait_for(crud.upload_origin(), SITE_KEY_BUDGET)
            except CrudRefusedError:
                # The store is not S3: no upload can be made, so the CSP names none.
                self._read = (now, None)
                self._failed_at = None
                return None
            except (CrudUnavailableError, TimeoutError) as error:
                logger.warning("could not read the upload origin (%s)", type(error).__name__)
                self._failed_at = now
                return None if self._read is None else self._read[1]
            self._read = (now, bare_origin(read))
            self._failed_at = None
            return self._read[1]
