"""#7025: the 3DFP search and create over the browser service's RPC. The answers cross as
JSON the worker validates; a failure crosses as its kind only."""

from __future__ import annotations

import json

import httpx
import pytest

from scadbuddy.profile_fetch.remote import RemoteSite
from scadbuddy.profile_fetch.steps import SiteError
from scadbuddy.profile_fetch.wire import CreateAnswer, CreateAsk, SearchAsk
from tests.profile_fetch.test_activities import SEARCH_ANSWER, FakeSite
from tests.profile_fetch.test_remote import INJECTED, TOKEN, Service, _with_remote

ASK = CreateAsk(
    brand="SUNLU",
    material="PLA+/Pro",
    material_type="Silk",
    colour="Copper",
    hexes=["#B87333"],
    gtin="06975373321234",
)


async def test_a_search_answers_the_sites_candidates() -> None:
    site = FakeSite()
    async with Service(site) as running:
        answer = await _with_remote(
            running.url, lambda remote: remote.search(SearchAsk(brand="SUNLU"))
        )
    assert answer == SEARCH_ANSWER
    assert site.searches == [SearchAsk(brand="SUNLU")]


async def test_a_create_answers_what_the_site_did() -> None:
    site = FakeSite()
    async with Service(site) as running:
        answer = await _with_remote(running.url, lambda remote: remote.create(ASK))
    assert answer == site.create_answer
    assert site.creates == [ASK]


@pytest.mark.parametrize("kind", ["no_defaults", "not_created", "option_missing"])
async def test_a_create_failure_crosses_as_its_kind_only(kind: str) -> None:
    site = FakeSite()
    site.tdfp_error = SiteError(kind, INJECTED)  # type: ignore[arg-type]
    async with Service(site) as running:
        with pytest.raises(SiteError) as caught:
            await _with_remote(running.url, lambda remote: remote.create(ASK))
    assert caught.value.kind == kind
    assert INJECTED not in str(caught.value)
    assert "3DFP create of SUNLU PLA+/Pro Silk Copper" in str(caught.value)


def _fake(answer: httpx.Response) -> RemoteSite:
    return RemoteSite(
        "http://browser.example", TOKEN, transport=httpx.MockTransport(lambda r: answer)
    )


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        json.dumps({"status": "created", "tdfp_id": -1}).encode(),
        json.dumps({"status": "created", "extra": INJECTED}).encode(),
        json.dumps({"status": "deleted"}).encode(),
    ],
)
async def test_a_create_answer_that_is_not_one_is_a_browser_failure(body: bytes) -> None:
    """The service may be compromised: its answer is checked, and its text never crosses."""
    with pytest.raises(SiteError) as caught:
        async with _fake(httpx.Response(200, content=body)) as remote:
            await remote.create(ASK)
    assert caught.value.kind == "browser"
    assert INJECTED not in str(caught.value)


async def test_a_search_answer_past_its_cap_is_refused() -> None:
    body = json.dumps({"query": "x", "candidates": [], "pad": "x" * 70_000}).encode()
    with pytest.raises(SiteError) as caught:
        async with _fake(httpx.Response(200, content=body)) as remote:
            await remote.search(SearchAsk(brand="SUNLU"))
    assert caught.value.kind == "browser"


async def test_the_create_ask_fits_the_services_ask_cap() -> None:
    """The longest create ask the worker can send is read whole by the service."""
    longest = CreateAsk(
        brand="b" * 200,
        material="m" * 200,
        material_type="t" * 200,
        colour="c" * 200,
        hexes=["#000000"] * 4,
        nominal_weight_g=100_000,
        mpn="p" * 200,
        gtin="1" * 14,
        asin="B" * 10,
        manufacturer_url="https://example.com/" + "u" * 980,  # type: ignore[arg-type]
        empty_spool_weight_g=10_000,
        td=100,
        not_duplicates=list(range(10**9, 10**9 + 25)),
    )
    site = FakeSite()
    async with Service(site) as running:
        answer = await _with_remote(running.url, lambda remote: remote.create(longest))
    assert answer == CreateAnswer(status="created", tdfp_id=40000, sent=["mpn"])
