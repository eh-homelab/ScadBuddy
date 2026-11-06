"""The grant a one-off fetch off the asset allowlist must carry (#959).

A fetch's ``allow_host`` lets one host off the allowlist through, so it must come from a
person's approval, not from whoever can reach the route. The agent's ``fetch_asset`` is
the one way to ask for it: that call is always parked for a person (agent
`tools/pinnedOutward.ts`), and only once it runs, after its approval, does the agent
mint this grant (`tools/fetchGrant.ts`) and send it in :data:`FETCH_GRANT_HEADER`.

A grant is MACed under a key derived from the KEK, as `auth/run_tokens.py` derives its
own, and names the host, a SHA-256 of the URL exactly as sent, an expiry and a nonce.
The route checks all of them against what it will fetch and spends the nonce
(`AssetStore.spend_fetch_grant`), so one grant serves one fetch. The format, fixed on
both sides:
``b64url("asset-fetch-once\\n<host>\\n<sha256 hex>\\n<expiry seconds>\\n<nonce hex>")
+ "." + b64url(HMAC-SHA256)``, unpadded. A grant is a bearer secret: never log one.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from datetime import datetime
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from scadbuddy.core.secrets import Kek

FETCH_GRANT_HEADER: Final = "x-scadbuddy-fetch-grant"
#: The HKDF label of :func:`fetch_grant_key`.
FETCH_GRANT_KEY_INFO: Final = b"scadbuddy:assets:fetch-once:v1"
#: The agent's (`fetchGrant.ts` `FETCH_GRANT_TTL_SECONDS`), for :func:`mint`.
FETCH_GRANT_TTL_SECONDS: Final = 600
_PURPOSE = "asset-fetch-once"
_NONCE = re.compile(r"^[0-9a-f]{32}$")


class FetchGrantError(ValueError):
    """The grant is missing, malformed, MACed under another key, for another host or
    URL, or expired. Its message never carries the grant."""


def fetch_grant_key(kek: Kek) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=FETCH_GRANT_KEY_INFO).derive(
        kek.key
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    """Only the one spelling :func:`_b64` writes, so no two strings verify as one grant."""
    data = base64.b64decode(
        (text + "=" * (-len(text) % 4)).encode("ascii"), altchars=b"-_", validate=True
    )
    if _b64(data) != text:
        raise ValueError("not canonical base64")
    return data


def _mac(key: bytes, body: bytes) -> bytes:
    return hmac.new(key, body, hashlib.sha256).digest()


def _digest(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def mint(key: bytes, host: str, url: str, *, now: datetime) -> str:
    """A grant as the agent mints one: for tests, and the format's one Python spelling."""
    expires = int(now.timestamp()) + FETCH_GRANT_TTL_SECONDS
    body = f"{_PURPOSE}\n{host}\n{_digest(url)}\n{expires}\n{secrets.token_hex(16)}".encode()
    return f"{_b64(body)}.{_b64(_mac(key, body))}"


def verify(key: bytes, grant: str, *, host: str, url: str, now: datetime) -> str:
    """The nonce of ``grant``, if its MAC is ``key``'s and it names ``host`` and ``url``
    and has not expired at ``now``; else :class:`FetchGrantError`."""
    try:
        encoded_body, encoded_mac = grant.split(".")
        body, mac = _unb64(encoded_body), _unb64(encoded_mac)
    except (ValueError, binascii.Error, UnicodeEncodeError):
        raise FetchGrantError("the fetch grant is malformed") from None
    if not hmac.compare_digest(mac, _mac(key, body)):
        raise FetchGrantError("the fetch grant was not minted by this deployment's agent")
    try:
        purpose, named_host, digest, expires, nonce = body.decode("utf-8").split("\n")
        expiry = int(expires)
    except ValueError:
        raise FetchGrantError("the fetch grant is malformed") from None
    if purpose != _PURPOSE or not _NONCE.match(nonce):
        raise FetchGrantError("the fetch grant is malformed")
    if named_host != host or not hmac.compare_digest(digest, _digest(url)):
        raise FetchGrantError("the fetch grant was approved for another host or URL")
    if now.timestamp() >= expiry:
        raise FetchGrantError("the fetch grant has expired; ask for the fetch again")
    return nonce
