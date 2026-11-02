"""The OIDC relying party (#4975 phase 2, spec 2026-11-01 §5.1): discovery, the JWKS, the
code redemption and the ID token's check.

Fetched as the agent fetches for ``/mcp`` (AI spec §8.3 "Fetching",
``agent/src/auth/oidc.ts``): https only (http for a loopback host), no redirects, a
bounded body and time, the metadata and keys cached ten minutes, an unknown ``kid``
refetching the keys at most every 30 s. An IdP on the LAN is normal here, so no
public-address rule applies. Every error's message is safe to show the person signing
in: it names what failed, never a token or the client secret.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from joserfc import errors as jose_errors
from joserfc import jwt
from joserfc.jwk import KeySet

from scadbuddy.library.url_import import ImportRefusedError, ResolverUnavailableError

#: The agent's ``SUPPORTED_ALGORITHMS``: asymmetric only, so never ``none`` or HMAC.
SUPPORTED_ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)
CACHE_SECONDS = 600.0
#: An unknown ``kid`` refetches the keys at most this often.
REFETCH_SECONDS = 30.0
FETCH_TIMEOUT = 10.0
MAX_BODY_BYTES = 512 * 1024
#: ``exp`` and ``iat`` are checked with this much skew allowed.
LEEWAY_SECONDS = 30


class OidcError(Exception):
    """A step of the login failed; the message says which, safely."""


@dataclass(frozen=True)
class Provider:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


#: What the public-only transport (``library/url_import.py`` ``PublicOnlyBackend``, the
#: login's unless ``SCADBUDDY_AUTH_OIDC_PRIVATE_NETWORK``) refused, said once.
NOT_PUBLIC = (
    "did not answer, or is not at a public address (an identity provider on a private"
    " network needs SCADBUDDY_AUTH_OIDC_PRIVATE_NETWORK=true)"
)


def check_url(url: str, what: str) -> str:
    """``url`` when it is https, or http on a loopback host; else :class:`OidcError`."""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise OidcError(f"the {what} is not a URL") from None
    host = parts.hostname or ""
    if not host or parts.username or parts.password:
        raise OidcError(f"the {what} is not an absolute URL without credentials")
    if parts.scheme == "https" or (parts.scheme == "http" and _loopback(host)):
        return url
    raise OidcError(f"the {what} must be https (http only for a loopback host)")


class OidcClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._providers: dict[str, tuple[float, Provider]] = {}
        self._keys: dict[str, tuple[float, KeySet]] = {}
        self._lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, follow_redirects=False, timeout=FETCH_TIMEOUT
        )

    async def _json(self, response: httpx.Response, what: str) -> dict[str, Any]:
        if response.is_redirect:
            raise OidcError(f"the {what} answered a redirect, which is not followed")
        if response.status_code != 200:
            raise OidcError(f"the {what} answered HTTP {response.status_code}")
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                raise OidcError(f"the {what} answered more than {MAX_BODY_BYTES} bytes")
        try:
            data = json.loads(body)
        except ValueError:
            raise OidcError(f"the {what} did not answer JSON") from None
        if not isinstance(data, dict):
            raise OidcError(f"the {what} did not answer a JSON object")
        return data

    async def _get(self, url: str, what: str) -> dict[str, Any]:
        check_url(url, what)
        try:
            async with self._client() as client, client.stream("GET", url) as response:
                return await self._json(response, what)
        except httpx.HTTPError as error:
            raise OidcError(f"the {what} could not be read ({type(error).__name__})") from None
        except (ImportRefusedError, ResolverUnavailableError):
            raise OidcError(f"the {what} {NOT_PUBLIC}") from None

    async def provider(self, issuer: str, *, fresh: bool = False) -> Provider:
        """The issuer's metadata (OIDC Discovery 1.0 §4), whose ``issuer`` must be
        ``issuer`` exactly (§4.3)."""
        cached = self._providers.get(issuer)
        if not fresh and cached is not None and self._clock() - cached[0] < CACHE_SECONDS:
            return cached[1]
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        data = await self._get(url, "issuer's discovery document")
        if data.get("issuer") != issuer:
            raise OidcError(
                f"the discovery document names the issuer {str(data.get('issuer'))[:200]!r},"
                f" not {issuer!r}"
            )
        fields = {}
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = data.get(name)
            if not isinstance(value, str):
                raise OidcError(f"the discovery document has no {name}")
            fields[name] = check_url(value, name.replace("_", " "))
        end = data.get("end_session_endpoint")
        provider = Provider(
            issuer=issuer,
            end_session_endpoint=check_url(end, "end session endpoint")
            if isinstance(end, str)
            else None,
            **fields,
        )
        self._providers[issuer] = (self._clock(), provider)
        return provider

    async def keys(self, provider: Provider, *, refetch: bool = False) -> KeySet:
        """The issuer's signing keys; ``refetch`` (an unknown ``kid``) fetches them
        again unless that was done within ``REFETCH_SECONDS``."""
        async with self._lock:
            cached = self._keys.get(provider.jwks_uri)
            if cached is not None:
                age = self._clock() - cached[0]
                if age < (REFETCH_SECONDS if refetch else CACHE_SECONDS):
                    return cached[1]
            data = await self._get(provider.jwks_uri, "issuer's JWKS")
            try:
                keys = KeySet.import_key_set(data)  # type: ignore[arg-type]
            except (jose_errors.JoseError, ValueError, TypeError, KeyError):
                raise OidcError("the issuer's JWKS holds no usable key") from None
            self._keys[provider.jwks_uri] = (self._clock(), keys)
            return keys

    async def redeem(
        self,
        provider: Provider,
        *,
        code: str,
        verifier: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
    ) -> str:
        """The ID token for ``code``, redeemed with ``client_secret_basic`` and the PKCE
        verifier (RFC 7636 §4.5)."""
        try:
            async with (
                self._client() as client,
                client.stream(
                    "POST",
                    provider.token_endpoint,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "code_verifier": verifier,
                    },
                    auth=httpx.BasicAuth(client_id, client_secret),
                    headers={"Accept": "application/json"},
                ) as response,
            ):
                data = await self._json(response, "token endpoint")
        except httpx.HTTPError as error:
            raise OidcError(
                f"the token endpoint could not be reached ({type(error).__name__})"
            ) from None
        except (ImportRefusedError, ResolverUnavailableError):
            raise OidcError(f"the token endpoint {NOT_PUBLIC}") from None
        token = data.get("id_token")
        if not isinstance(token, str):
            raise OidcError("the token endpoint answered no ID token")
        return token

    async def verify(
        self,
        provider: Provider,
        id_token: str,
        *,
        client_id: str,
        nonce: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """The ID token's claims once OIDC Core §3.1.3.7 holds: signed by the issuer's
        keys with an allowed algorithm, ``iss``, ``aud`` (and ``azp``), ``exp`` and
        ``iat``, and ``nonce``."""
        keys = await self.keys(provider)
        try:
            token = jwt.decode(id_token, keys, algorithms=SUPPORTED_ALGORITHMS)
        except jose_errors.InvalidKeyIdError:
            keys = await self.keys(provider, refetch=True)
            token = self._decode(id_token, keys)
        except jose_errors.JoseError as error:
            raise OidcError(f"the ID token is not valid ({error.error})") from None
        return check_claims(token.claims, provider.issuer, client_id, nonce, now)

    @staticmethod
    def _decode(id_token: str, keys: KeySet) -> jwt.Token:
        try:
            return jwt.decode(id_token, keys, algorithms=SUPPORTED_ALGORITHMS)
        except jose_errors.JoseError as error:
            raise OidcError(f"the ID token is not valid ({error.error})") from None


def check_claims(
    claims: Mapping[str, Any],
    issuer: str,
    client_id: str,
    nonce: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if claims.get("iss") != issuer:
        raise OidcError("the ID token is from another issuer")
    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if client_id not in audiences:
        raise OidcError("the ID token is for another client")
    azp = claims.get("azp")
    if (azp is not None or len(audiences) > 1) and azp != client_id:
        raise OidcError("the ID token was issued to another party")
    at = (now or datetime.now(UTC)).timestamp()
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, int | float) or isinstance(exp, bool) or exp + LEEWAY_SECONDS <= at:
        raise OidcError("the ID token has expired")
    if not isinstance(iat, int | float) or isinstance(iat, bool) or iat - LEEWAY_SECONDS > at:
        raise OidcError("the ID token was issued in the future")
    if claims.get("nonce") != nonce:
        raise OidcError("the ID token's nonce is not this sign-in's")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise OidcError("the ID token has no subject")
    return dict(claims)
