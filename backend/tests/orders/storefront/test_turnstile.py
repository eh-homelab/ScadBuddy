"""The CRUD side's bot check (#5283, spec 2026-11-02 §5.8): Cloudflare Turnstile's
Siteverify, called with the sealed secret, and the verdict the edge acts on."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from scadbuddy.orders.storefront.turnstile import (
    MAX_TOKEN_CHARS,
    SITEVERIFY_URL,
    TurnstileConfig,
    verify,
)

TOKEN = "XXXX.DUMMY.TOKEN.XXXX"
ON = TurnstileConfig(
    site_key="1x00000000000000000000AA", secret="s3cret", hostname="shop.example.com"
)
PASSED = {
    "success": True,
    "challenge_ts": "2026-11-09T12:00:00.000Z",
    "hostname": "shop.example.com",
    "error-codes": [],
    "action": "submit",
}


@pytest.fixture
def cloudflare() -> Iterator[respx.Route]:
    with respx.mock(assert_all_called=False) as router:
        yield router.post(SITEVERIFY_URL).respond(200, json=PASSED)


async def run(config: TurnstileConfig, token: str | None = TOKEN, action: Any = "submit") -> str:
    return await verify(config, token=token, action=action, remoteip="203.0.113.9")


async def test_it_is_off_without_a_site_key(cloudflare: respx.Route) -> None:
    off = TurnstileConfig(site_key=None, secret="s3cret", hostname=None)
    assert await run(off, token=None) == "off"
    assert not cloudflare.called


async def test_a_site_key_without_a_secret_fails_closed(cloudflare: respx.Route) -> None:
    # A secret this process cannot open (no KEK, another KEK) reads as none: the check
    # stays on and refuses, never passes.
    assert await run(TurnstileConfig(site_key="k", secret=None, hostname=None)) == "unavailable"
    assert not cloudflare.called


async def test_a_good_token_passes_and_siteverify_gets_the_secret_and_address(
    cloudflare: respx.Route,
) -> None:
    assert await run(ON) == "pass"
    sent = parse_qs(cloudflare.calls.last.request.content.decode())
    assert sent == {"secret": ["s3cret"], "response": [TOKEN], "remoteip": ["203.0.113.9"]}


@pytest.mark.parametrize("token", [None, "", "x" * (MAX_TOKEN_CHARS + 1)])
async def test_a_missing_or_oversized_token_fails_before_any_call(
    cloudflare: respx.Route, token: str | None
) -> None:
    assert await run(ON, token=token) == "fail"
    assert not cloudflare.called


@pytest.mark.parametrize(
    "answer",
    [
        {**PASSED, "success": False, "error-codes": ["invalid-input-response"]},
        {**PASSED, "success": False, "error-codes": ["timeout-or-duplicate"]},
        {**PASSED, "action": "sign_up"},
        {**PASSED, "hostname": "evil.example"},
        {**PASSED, "action": None},
    ],
)
async def test_a_refused_token_or_one_for_another_form_or_host_fails(
    cloudflare: respx.Route, answer: dict[str, Any]
) -> None:
    cloudflare.respond(200, json=answer)
    assert await run(ON) == "fail"


async def test_the_hostname_is_not_checked_without_a_public_url(cloudflare: respx.Route) -> None:
    cloudflare.respond(200, json={**PASSED, "hostname": "localhost"})
    assert await run(ON._replace(hostname=None)) == "pass"


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.Response(500),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-secret"]}),
        httpx.Response(200, json={"success": False, "error-codes": ["internal-error"]}),
        httpx.ConnectError("down"),
        httpx.ReadTimeout("slow"),
    ],
)
async def test_siteverify_out_of_reach_or_misconfigured_fails_closed(
    cloudflare: respx.Route, outcome: httpx.Response | Exception
) -> None:
    cloudflare.mock(side_effect=outcome) if isinstance(outcome, Exception) else cloudflare.mock(
        return_value=outcome
    )
    assert await run(ON) == "unavailable"
