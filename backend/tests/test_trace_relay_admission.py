# backend/tests/test_trace_relay_admission.py
"""telemetry/admission.py: who may post to the relay, and how often (spec 2026-10-01 §5.2)."""

from __future__ import annotations

import pytest
from starlette.datastructures import Headers

from scadbuddy.api.realtime import RateLimit
from scadbuddy.core.problems import ApiError
from scadbuddy.core.proxies import Network
from scadbuddy.core.settings import Settings
from scadbuddy.telemetry.admission import (
    RelayLimits,
    check_content_type,
    check_origin,
    relay_client,
)
from tests.conftest import UNUSED_DATABASE_URL, UNUSED_TEMPORAL_ADDRESS

PUBLIC = "https://scadbuddy.example"


def networks(trusted_proxies: str = "") -> tuple[Network, ...]:
    return settings(trusted_proxies).trusted_proxy_networks


def settings(trusted_proxies: str = "") -> Settings:
    return Settings(
        database_url=UNUSED_DATABASE_URL,
        temporal_address=UNUSED_TEMPORAL_ADDRESS,
        public_url=PUBLIC,
        allowed_origins="https://scadbuddy.lan",
        trusted_proxies=trusted_proxies,
    )


def headers(*pairs: tuple[str, str]) -> Headers:
    return Headers(raw=[(name.encode(), value.encode()) for name, value in pairs])


def refusal(error: pytest.ExceptionInfo[ApiError]) -> tuple[int, str]:
    return error.value.status, error.value.detail


@pytest.mark.parametrize(
    "origin",
    [PUBLIC, "https://SCADBUDDY.example:443", "https://scadbuddy.lan", "http://localhost:5173"],
)
def test_the_uis_own_origins_are_accepted(origin: str) -> None:
    check_origin(headers(("origin", origin)), settings())
    check_origin(headers(("origin", origin), ("sec-fetch-site", "same-origin")), settings())


def test_no_origin_is_refused() -> None:
    with pytest.raises(ApiError) as error:
        check_origin(headers(), settings())
    assert refusal(error) == (
        403,
        "the relay accepts requests from ScadBuddy's own pages, which send Origin",
    )


@pytest.mark.parametrize(
    "origin", ["https://evil.example", "null", "file://", "https://scadbuddy.example.evil"]
)
def test_a_foreign_origin_is_refused_by_rule(origin: str) -> None:
    with pytest.raises(ApiError) as error:
        check_origin(headers(("origin", origin)), settings())
    assert refusal(error) == (403, "Origin not allowed")
    assert origin not in error.value.detail


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none"])
def test_a_sec_fetch_site_other_than_same_origin_is_refused(site: str) -> None:
    with pytest.raises(ApiError) as error:
        check_origin(headers(("origin", PUBLIC), ("sec-fetch-site", site)), settings())
    assert refusal(error) == (403, "Sec-Fetch-Site must be same-origin")


@pytest.mark.parametrize(
    "kind", ["application/json", "application/json; charset=utf-8", "Application/JSON"]
)
def test_json_is_accepted(kind: str) -> None:
    check_content_type(headers(("content-type", kind)))


@pytest.mark.parametrize("kind", ["text/plain", "application/x-protobuf", ""])
def test_anything_else_is_415(kind: str) -> None:
    with pytest.raises(ApiError) as error:
        check_content_type(headers(("content-type", kind)))
    assert refusal(error) == (415, "the relay accepts application/json only")


def test_retry_after_is_the_wait_for_one_more_token() -> None:
    now = [0.0]
    limit = RateLimit(1, 0.5, lambda: now[0])
    assert limit.retry_after() == 0
    assert limit.take()
    assert limit.retry_after() == pytest.approx(2.0)
    now[0] = 1.5
    assert limit.retry_after() == pytest.approx(0.5)
    now[0] = 2.0
    assert limit.take()


def test_the_per_client_bucket_refuses_with_retry_after() -> None:
    limits = RelayLimits(clock=lambda: 0.0, client_burst=1, client_per_second=0.25)
    limits.take("203.0.113.9")
    with pytest.raises(ApiError) as error:
        limits.take("203.0.113.9")
    assert refusal(error) == (429, "this client is over the relay's rate limit")
    assert error.value.headers["Retry-After"] == "4"
    # Another client has a bucket of its own.
    limits.take("203.0.113.10")


def test_the_per_process_bucket_caps_every_client_together() -> None:
    limits = RelayLimits(clock=lambda: 0.0, process_burst=2, process_per_second=0.5)
    limits.take("a")
    limits.take("b")
    with pytest.raises(ApiError) as error:
        limits.take("c")
    assert refusal(error) == (429, "the relay is over its overall rate limit")
    assert error.value.headers["Retry-After"] == "2"


def test_the_client_buckets_are_bounded() -> None:
    limits = RelayLimits(clock=lambda: 0.0, max_clients=3, process_burst=1000)
    for index in range(10):
        limits.take(f"10.0.0.{index}")
    assert limits.tracked_clients == 3


def test_an_untrusted_peer_is_its_own_client() -> None:
    forwarded = headers(("x-forwarded-for", "203.0.113.9"))
    assert relay_client(forwarded, "10.43.0.5", networks("10.42.0.0/16")) == "10.43.0.5"
    assert relay_client(forwarded, "10.42.0.5", networks()) == "10.42.0.5"


def test_a_loopback_peer_is_not_believed_when_no_proxy_is_trusted() -> None:
    """Review 5 of #1090: nothing below `relay_client` decides for it. The image starts
    uvicorn with ``--no-proxy-headers``, so its peer is the socket's, and an in-pod
    caller (``kubectl port-forward``, a sidecar) cannot pick its own bucket."""
    forwarded = headers(("x-forwarded-for", "203.0.113.9"))
    for peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        assert relay_client(forwarded, peer, networks()) == peer


def test_a_trusted_proxy_names_the_client_by_its_last_value() -> None:
    trusted = networks("10.42.0.0/16")
    forwarded = headers(("x-forwarded-for", "198.51.100.1, 203.0.113.9"))
    assert relay_client(forwarded, "10.42.0.5", trusted) == "203.0.113.9"
    # Two header lines read as one list, as Node joins them for the agent.
    split = headers(("x-forwarded-for", "198.51.100.1"), ("x-forwarded-for", "203.0.113.9"))
    assert relay_client(split, "10.42.0.5", trusted) == "203.0.113.9"


def test_an_empty_last_value_falls_back_to_the_peer() -> None:
    forwarded = headers(("x-forwarded-for", "203.0.113.9, "))
    assert relay_client(forwarded, "10.42.0.5", networks("10.42.0.0/16")) == "10.42.0.5"
    assert relay_client(headers(), None, networks()) == "unknown"


def test_a_refusal_by_the_process_bucket_does_not_spend_the_clients_own() -> None:
    limits = RelayLimits(clock=lambda: 0.0, process_burst=1, client_burst=2)
    limits.take("a")
    for _ in range(5):
        with pytest.raises(ApiError) as error:
            limits.take("b")
        assert refusal(error) == (429, "the relay is over its overall rate limit")
    # "b" lost nothing to those refusals: both of its tokens are still there.
    assert limits._clients["b"].take()
    assert limits._clients["b"].take()
