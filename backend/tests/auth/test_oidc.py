"""The relying party's fetching rules (#4975 phase 2, spec 2026-11-01 §5.1): https only
(http on loopback), no redirects, a bounded body, the cache and the unknown-``kid``
refetch, and the ID token's claims."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
import pytest

from scadbuddy.auth.oidc import (
    CACHE_SECONDS,
    MAX_BODY_BYTES,
    REFETCH_SECONDS,
    OidcClient,
    OidcError,
    check_claims,
    check_url,
)
from tests.auth.fake_oidc import CLIENT_ID, ISSUER, FakeIssuer


@pytest.mark.parametrize(
    "url", ["https://idp.test", "http://localhost:8080", "http://127.0.0.1", "http://[::1]:9"]
)
def test_allowed_urls(url: str) -> None:
    assert check_url(url, "issuer") == url


@pytest.mark.parametrize(
    "url", ["http://idp.test", "ftp://idp.test", "https://user:pw@idp.test", "idp.test", ""]
)
def test_refused_urls(url: str) -> None:
    with pytest.raises(OidcError):
        check_url(url, "issuer")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_discovery_and_keys_are_cached_then_refetched() -> None:
    issuer = FakeIssuer()
    clock = Clock()
    client = OidcClient(transport=issuer.transport(), clock=clock)
    provider = await client.provider(ISSUER)
    await client.keys(provider)
    await client.keys(provider)
    assert issuer.jwks_fetches == 1
    # An unknown kid within 30 s of the fetch: the cache.
    clock.now += REFETCH_SECONDS - 1
    await client.keys(provider, refetch=True)
    assert issuer.jwks_fetches == 1
    clock.now += 2
    await client.keys(provider, refetch=True)
    assert issuer.jwks_fetches == 2
    clock.now += CACHE_SECONDS
    await client.keys(provider)
    assert issuer.jwks_fetches == 3


async def test_the_discovery_must_name_its_issuer() -> None:
    issuer = FakeIssuer(claimed_issuer="https://other.test")
    with pytest.raises(OidcError, match="names the issuer"):
        await OidcClient(transport=issuer.transport()).provider(ISSUER)


async def test_a_redirect_is_not_followed() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://elsewhere.test/"})

    client = OidcClient(transport=httpx.MockTransport(answer))
    with pytest.raises(OidcError, match="redirect"):
        await client.provider(ISSUER)


async def test_a_huge_body_is_refused() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b" " * (MAX_BODY_BYTES + 1))

    client = OidcClient(transport=httpx.MockTransport(answer))
    with pytest.raises(OidcError, match="more than"):
        await client.provider(ISSUER)


def _claims(**changes: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "alice",
        "iat": now,
        "exp": now + 60,
        "nonce": "n",
    }
    claims.update(changes)
    return claims


def test_claims_accepted() -> None:
    assert check_claims(_claims(), ISSUER, CLIENT_ID, "n")["sub"] == "alice"


def test_azp_must_be_the_client_with_several_audiences() -> None:
    with pytest.raises(OidcError, match="another party"):
        check_claims(_claims(aud=[CLIENT_ID, "x"]), ISSUER, CLIENT_ID, "n")
    assert check_claims(_claims(aud=[CLIENT_ID, "x"], azp=CLIENT_ID), ISSUER, CLIENT_ID, "n")


def test_the_leeway_is_30_seconds() -> None:
    now = datetime.now(UTC)
    at = int(now.timestamp())
    assert check_claims(_claims(exp=at - 29), ISSUER, CLIENT_ID, "n", now=now)
    with pytest.raises(OidcError, match="expired"):
        check_claims(_claims(exp=at - 31), ISSUER, CLIENT_ID, "n", now=now)
