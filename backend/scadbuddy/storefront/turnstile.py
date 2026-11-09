"""The edge's bot check (#5283, spec 2026-11-02 §5.8): Cloudflare Turnstile on sign-up,
submit and design requests.

The edge holds no Turnstile secret: the CRUD service keeps it sealed under its KEK and
calls Cloudflare's Siteverify itself (`orders/storefront/turnstile.py`). A route that
takes the check awaits `require_turnstile` before it reads its body or calls the CRUD
route that acts: it reads the widget's token from `TURNSTILE_HEADER` and sends it, with
the form's ``action``, to ``POST …/storefront/turnstile/verify``, which answers a verdict.

- ``off`` (no site key set) and ``pass`` let the request on.
- ``fail`` (no token, a refused, used or expired token, or one made for another form or
  host) is 403 ``…/storefront/bot_check_failed``: the page solves the widget again.
- ``unavailable`` (a site key with no secret the CRUD side can read, or Cloudflare not
  answering) is 503 ``…/storefront/bot_check_unavailable``. The check fails closed.

A token longer than Cloudflare's bound fails here, before any call. `SiteKey` is the
CRUD side's site key, read at most once per `SITE_KEY_TTL` per replica: the page's
widget (`GET /api/store/v1/turnstile`) and the page's CSP (`static.py`) both read it, so
the CSP names Cloudflare only while the check is on.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Final

from starlette.requests import Request

from scadbuddy.core.problems import ApiError
from scadbuddy.core.proxies import client_address
from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.storefront import StorefrontCrud
from scadbuddy.storefront.wire import (
    MAX_TURNSTILE_TOKEN_CHARS,
    STOREFRONT_PROBLEMS,
    TURNSTILE_HEADER,
    TurnstileAction,
)

logger = logging.getLogger(__name__)

BOT_CHECK_FAILED: Final = f"{STOREFRONT_PROBLEMS}bot_check_failed"
BOT_CHECK_UNAVAILABLE: Final = f"{STOREFRONT_PROBLEMS}bot_check_unavailable"
#: How long one replica trusts its read of the site key.
SITE_KEY_TTL: Final = 30.0


def bot_check_failed() -> ApiError:
    return ApiError(
        403,
        "The check that you are not a robot did not pass. Try it again.",
        type_=BOT_CHECK_FAILED,
    )


def bot_check_unavailable() -> ApiError:
    return ApiError(
        503,
        "The check that you are not a robot is not working now. Try again later.",
        type_=BOT_CHECK_UNAVAILABLE,
    )


def _client(request: Request) -> str:
    """The browser's address, as `routes.py` finds it for every visitor route."""
    trusted = request.app.state.trusted_proxies
    peer = request.client.host if request.client else None
    address = client_address(peer, request.headers.get("x-forwarded-for"), trusted)
    if address is None:
        raise ApiError(400, "The request has no client address.")
    return address


async def require_turnstile(request: Request, action: TurnstileAction) -> None:
    """Passes when the check is off or the browser's token passes; raises 403
    `BOT_CHECK_FAILED` or 503 `BOT_CHECK_UNAVAILABLE` otherwise (module docstring)."""
    token = request.headers.get(TURNSTILE_HEADER) or None
    if token is not None and len(token) > MAX_TURNSTILE_TOKEN_CHARS:
        raise bot_check_failed()
    crud: StorefrontCrud = request.app.state.crud
    verdict = await crud.turnstile_verify(
        token, action, visitor_id=request.state.visitor_id, client=_client(request)
    )
    if verdict in ("off", "pass"):
        return
    if verdict == "fail":
        raise bot_check_failed()
    if verdict != "unavailable":
        logger.error("the CRUD service gave an unknown bot check verdict")
    raise bot_check_unavailable()


class SiteKey:
    """The CRUD side's Turnstile site key, or None while the check is off, read at most
    once per `SITE_KEY_TTL`. A read that fails keeps the last value (None before the
    first), so a page still loads while the CRUD service is away; with the check on and
    the key not yet read, the page cannot make a token, and the check then refuses."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._read: tuple[float, str | None] | None = None

    async def get(self, crud: StorefrontCrud) -> str | None:
        now = self._clock()
        if self._read is not None and now - self._read[0] < SITE_KEY_TTL:
            return self._read[1]
        try:
            key = await crud.turnstile_site_key()
        except (CrudUnavailableError, CrudRefusedError) as error:
            logger.warning("could not read the bot check's site key (%s)", type(error).__name__)
            return None if self._read is None else self._read[1]
        self._read = (now, key)
        return key
