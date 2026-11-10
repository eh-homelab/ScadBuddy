"""The storefront's bot check, on the CRUD side (#5283, spec 2026-11-02 §5.8): Cloudflare
Turnstile's Siteverify, called with the secret this process keeps sealed under its KEK
(the setting ``storefront_turnstile_secret``, a `SECRET_FIELDS` member). The edge never
holds the secret: it forwards the browser's token here (`storefront/turnstile.py`) and acts
on the verdict.

The check is on while ``storefront_turnstile_site_key`` is set, and off, passing
everything, while it is not. On, it fails closed: a secret this process cannot read (none
saved, no KEK, another KEK: `settings_store._opened` drops it) or a Siteverify that does not
answer as documented is ``unavailable``, never a pass.

Cloudflare's facts this rests on
(https://developers.cloudflare.com/turnstile/get-started/server-side-validation/, read
2026-11-09):

- Siteverify is ``POST https://challenges.cloudflare.com/turnstile/v0/siteverify`` and takes
  ``application/x-www-form-urlencoded`` with ``secret``, ``response`` (the widget's token)
  and the optional ``remoteip``; it always answers JSON.
- A token is at most 2048 characters, valid for 300 seconds, and can be validated only
  once (a second validation is ``timeout-or-duplicate``).
- The answer carries ``success``, ``error-codes``, ``hostname`` and ``action``; the page
  advises validating the action and hostname, and setting a timeout on the call.
- The error codes ``missing-input-secret``, ``invalid-input-secret``, ``bad-request`` and
  ``internal-error`` are the shop's or Cloudflare's fault, not the visitor's: they read as
  ``unavailable``. Every other refusal is ``fail``.

Siteverify's optional ``idempotency_key`` is not sent. With one key per token, a token
that passed once would pass again on every replay within its five minutes; without it a
retry after a lost answer reads ``timeout-or-duplicate``, and the visitor solves the
widget again.
"""

from __future__ import annotations

import logging
from typing import Final, Literal, NamedTuple

import httpx

from scadbuddy.storefront.wire import (
    MAX_TURNSTILE_TOKEN_CHARS,
    SITEVERIFY_SECONDS,
    TurnstileAction,
)

logger = logging.getLogger(__name__)

SITEVERIFY_URL: Final = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
#: Cloudflare's bound on a token's length.
MAX_TOKEN_CHARS: Final = MAX_TURNSTILE_TOKEN_CHARS
#: How long one Siteverify call may take, connect to answer.
SITEVERIFY_TIMEOUT: Final = httpx.Timeout(SITEVERIFY_SECONDS)
#: Refusals that are not the visitor's doing: the check cannot judge the token.
_NOT_THE_VISITOR: Final = frozenset(
    {"missing-input-secret", "invalid-input-secret", "bad-request", "internal-error"}
)

Verdict = Literal["off", "pass", "fail", "unavailable"]


class TurnstileConfig(NamedTuple):
    """What a check reads: the site key (None is off), the opened secret, and the host
    the page is served on (the storefront's public URL's), or None to skip that check."""

    site_key: str | None
    secret: str | None
    hostname: str | None


async def verify(
    config: TurnstileConfig,
    *,
    token: str | None,
    action: TurnstileAction,
    remoteip: str,
) -> Verdict:
    """The verdict on ``token``, which the page's widget made for ``action``."""
    if not config.site_key:
        return "off"
    if not config.secret:
        logger.error("Turnstile has a site key but no secret this process can read")
        return "unavailable"
    if not token or len(token) > MAX_TOKEN_CHARS:
        return "fail"
    form = {"secret": config.secret, "response": token, "remoteip": remoteip}
    try:
        async with httpx.AsyncClient(timeout=SITEVERIFY_TIMEOUT) as client:
            response = await client.post(SITEVERIFY_URL, data=form)
        answer = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        logger.warning("Turnstile's Siteverify did not answer")
        return "unavailable"
    if not isinstance(answer, dict):
        logger.warning("Turnstile's Siteverify answered %s", response.status_code)
        return "unavailable"
    codes = answer.get("error-codes")
    codes = set(codes) if isinstance(codes, list) else set()
    if answer.get("success") is not True:
        if codes & _NOT_THE_VISITOR:
            logger.error("Turnstile's Siteverify refused the shop: %s", sorted(codes))
            return "unavailable"
        return "fail"
    if answer.get("action") != action:
        return "fail"
    if config.hostname is not None and answer.get("hostname") != config.hostname:
        return "fail"
    return "pass"
