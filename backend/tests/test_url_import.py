from __future__ import annotations

import asyncio
import gzip
import socket
import threading
import time
import zlib
from collections.abc import AsyncIterator, Callable, Iterable, Iterator

import httpcore
import httpx
import pytest
import respx

from scadbuddy.library import url_import
from scadbuddy.library.url_import import (
    ImportRefusedError,
    PublicOnlyBackend,
    ResolverBusyError,
    fetch_model,
    is_public,
    unreachable,
)
from scadbuddy.library.url_import import resolve_host as real_resolve_host
from tests.conftest import PUBLIC_ADDRESS

pytestmark = pytest.mark.usefixtures("fake_dns")

RAW_URL = "https://raw.githubusercontent.com/someone/models/main/Gridfinity%20Bin.scad"
SOURCE = "width = 10;\ncube(width);\n"
LIMIT = 1024

NOT_PUBLIC = [
    "127.0.0.1",
    "10.1.2.3",
    "172.16.0.1",
    "192.168.1.10",
    "169.254.169.254",
    "100.64.0.1",
    "0.0.0.0",
    "224.0.0.1",
    "240.0.0.1",
    "::1",
    "::",
    "fd00::1",
    "fe80::1",
    "ff0e::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "64:ff9b::a00:1",
    "::10.0.0.1",
    "::a9fe:a9fe",
    "2002:a00:1::1",
]


@respx.mock
async def test_a_raw_scad_url_yields_its_source_named_after_the_file() -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text=SOURCE))

    imported = await fetch_model(RAW_URL, limit=LIMIT)

    assert imported.source == SOURCE
    assert imported.name == "Gridfinity Bin"
    assert imported.origin_url == RAW_URL


@respx.mock
async def test_a_url_with_no_file_name_is_named_after_its_host() -> None:
    respx.get("https://example.com/").mock(return_value=httpx.Response(200, text=SOURCE))

    imported = await fetch_model("https://example.com/", limit=LIMIT)

    assert imported.name == "example.com"


@pytest.mark.parametrize(
    "url",
    ["http://example.com/model.scad", "ftp://example.com/model.scad", "file:///etc/passwd"],
)
async def test_anything_but_https_is_refused_before_a_request_is_made(url: str) -> None:
    with respx.mock(assert_all_called=False) as mock, pytest.raises(ImportRefusedError) as caught:
        await fetch_model(url, limit=LIMIT)
    assert "https" in str(caught.value)
    assert not mock.calls


async def test_a_string_that_is_not_a_url_is_refused() -> None:
    with pytest.raises(ImportRefusedError):
        await fetch_model("not a url", limit=LIMIT)


@respx.mock
async def test_a_redirect_to_plain_http_is_not_followed() -> None:
    respx.get("https://example.com/model.scad").mock(
        return_value=httpx.Response(302, headers={"Location": "http://example.com/model.scad"})
    )
    downgraded = respx.get("http://example.com/model.scad")

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model("https://example.com/model.scad", limit=LIMIT)

    assert "https" in str(caught.value)
    assert not downgraded.called


@respx.mock
async def test_an_https_redirect_is_followed_and_the_pasted_url_is_the_origin() -> None:
    respx.get("https://example.com/latest.scad").mock(
        return_value=httpx.Response(302, headers={"Location": "https://cdn.example.com/v2.scad"})
    )
    respx.get("https://cdn.example.com/v2.scad").mock(return_value=httpx.Response(200, text=SOURCE))

    imported = await fetch_model("https://example.com/latest.scad", limit=LIMIT)

    assert imported.source == SOURCE
    assert imported.origin_url == "https://example.com/latest.scad"


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")], ids=["refused", "slow"]
)
@respx.mock
async def test_a_redirect_target_that_does_not_answer_is_the_host_named(
    failure: Exception,
) -> None:
    respx.get("https://example.com/latest.scad").mock(
        return_value=httpx.Response(302, headers={"Location": "https://cdn.example.com/v2.scad"})
    )
    respx.get("https://cdn.example.com/v2.scad").mock(side_effect=failure)

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model("https://example.com/latest.scad", limit=LIMIT)

    assert str(caught.value) == str(unreachable("cdn.example.com", redirected_from="example.com"))
    assert "cdn.example.com (redirected from example.com)" in str(caught.value)


@respx.mock
async def test_a_redirect_target_past_the_deadline_is_the_host_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(url_import, "IMPORT_TIMEOUT", 0.05)

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        raise AssertionError("the deadline should have cut this off")

    respx.get("https://example.com/latest.scad").mock(
        return_value=httpx.Response(302, headers={"Location": "https://cdn.example.com/v2.scad"})
    )
    respx.get("https://cdn.example.com/v2.scad").mock(side_effect=hang)

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model("https://example.com/latest.scad", limit=LIMIT)

    assert str(caught.value) == str(unreachable("cdn.example.com", redirected_from="example.com"))


@respx.mock
async def test_a_redirect_into_the_cluster_reads_like_a_redirect_that_did_not_answer(
    fake_dns: dict[str, list[str]],
) -> None:
    fake_dns["internal.example.com"] = ["10.0.0.7"]
    respx.get("https://example.com/a.scad").mock(
        return_value=httpx.Response(302, headers={"Location": "https://internal.example.com/x"})
    )
    respx.get("https://example.com/b.scad").mock(
        return_value=httpx.Response(302, headers={"Location": "https://slow.example.com/x"})
    )
    internal = respx.get("https://internal.example.com/x")
    respx.get("https://slow.example.com/x").mock(side_effect=httpx.ConnectTimeout("slow"))

    with pytest.raises(ImportRefusedError) as blocked:
        await fetch_model("https://example.com/a.scad", limit=LIMIT)
    with pytest.raises(ImportRefusedError) as silent:
        await fetch_model("https://example.com/b.scad", limit=LIMIT)

    assert str(blocked.value) == str(silent.value).replace(
        "slow.example.com", "internal.example.com"
    )
    assert not internal.called


async def test_the_pasted_host_is_not_repeated_when_it_is_the_one_that_failed() -> None:
    assert str(unreachable("example.com", redirected_from="example.com")) == str(
        unreachable("example.com")
    )


class _WatchedBody(httpx.AsyncByteStream):
    """A body that records whether anything read it -- by default far over the limit."""

    def __init__(self, body: bytes = b"x" * LIMIT * 100) -> None:
        self.body = body
        self.read = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read = True
        yield self.body


async def test_a_redirect_is_followed_without_reading_its_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not respx: it reads a mocked body itself to build the response, so whether
    # the fetch read it could not be seen. A MockTransport hands the stream over as is.
    huge = _WatchedBody()

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start.scad":
            return httpx.Response(
                302, headers={"Location": "https://cdn.example.com/final.scad"}, stream=huge
            )
        # A stream, not `text=`: a Response built from bytes reads itself at once.
        return httpx.Response(200, stream=_WatchedBody(SOURCE.encode()))

    monkeypatch.setattr(url_import, "_transport", lambda: httpx.MockTransport(answer))

    imported = await fetch_model("https://example.com/start.scad", limit=LIMIT)

    assert imported.source == SOURCE
    assert not huge.read


@respx.mock
async def test_too_many_redirects_are_refused() -> None:
    hop = respx.get("https://example.com/loop.scad").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/loop.scad"})
    )

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model("https://example.com/loop.scad", limit=LIMIT)

    assert "redirected more than" in str(caught.value)
    assert hop.call_count == url_import.MAX_REDIRECTS + 1


@respx.mock
async def test_a_body_over_the_limit_is_refused() -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, text="x" * (LIMIT + 1)))

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model(RAW_URL, limit=LIMIT)

    assert str(LIMIT) in str(caught.value)


@respx.mock
async def test_the_fetch_asks_for_the_file_uncompressed() -> None:
    route = respx.get(RAW_URL).mock(return_value=httpx.Response(200, text=SOURCE))

    await fetch_model(RAW_URL, limit=LIMIT)

    assert route.calls[0].request.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    ("encoding", "compress"), [("gzip", gzip.compress), ("deflate", zlib.compress)]
)
@respx.mock
async def test_a_compression_bomb_is_refused_without_being_inflated(
    encoding: str, compress: Callable[[bytes], bytes]
) -> None:
    # Under the limit on the wire, a hundred times it once inflated.
    bomb = compress(b"x" * (LIMIT * 100))
    assert len(bomb) < LIMIT
    respx.get(RAW_URL).mock(
        return_value=httpx.Response(200, content=bomb, headers={"Content-Encoding": encoding})
    )

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model(RAW_URL, limit=LIMIT)

    # Refused on the header, before a byte of the body is read or decoded.
    assert "compressed" in str(caught.value)


def test_the_transport_connects_through_the_public_only_backend() -> None:
    pool = url_import._transport()._pool

    assert isinstance(pool, httpcore.AsyncConnectionPool)
    assert isinstance(pool._network_backend, PublicOnlyBackend)


def test_a_transport_the_backend_cannot_be_fitted_to_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If httpx stops keeping its pool where this looks, building the client must
    fail -- not quietly connect through a backend that re-resolves the name."""

    class Moved(httpx.AsyncHTTPTransport):
        def __init__(self) -> None:
            super().__init__()
            self._connections = self._pool
            del self._pool

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", Moved)

    with pytest.raises(RuntimeError, match="PublicOnlyBackend"):
        url_import._transport()


@respx.mock
async def test_an_html_page_is_refused_rather_than_parse_checked() -> None:
    respx.get("https://github.com/someone/models/blob/main/bin.scad").mock(
        return_value=httpx.Response(
            200, text="<!doctype html><html></html>", headers={"Content-Type": "text/html"}
        )
    )

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model("https://github.com/someone/models/blob/main/bin.scad", limit=LIMIT)

    assert "raw" in str(caught.value)


@respx.mock
async def test_a_binary_body_is_refused() -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(200, content=b"PK\x03\x04\x00\x00"))

    with pytest.raises(ImportRefusedError):
        await fetch_model(RAW_URL, limit=LIMIT)


@respx.mock
async def test_a_public_server_error_status_is_reported() -> None:
    respx.get(RAW_URL).mock(return_value=httpx.Response(404))

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model(RAW_URL, limit=LIMIT)

    assert "404" in str(caught.value)


@pytest.mark.parametrize(
    "failure", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")], ids=["refused", "slow"]
)
@respx.mock
async def test_no_answer_reads_the_same_as_an_address_that_is_not_public(
    failure: Exception,
) -> None:
    respx.get(RAW_URL).mock(side_effect=failure)

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model(RAW_URL, limit=LIMIT)

    assert str(caught.value) == str(unreachable("raw.githubusercontent.com"))


@pytest.mark.parametrize("address", NOT_PUBLIC)
def test_addresses_that_are_not_globally_routable_are_not_public(address: str) -> None:
    assert not is_public(address)


@pytest.mark.parametrize(
    "address", [PUBLIC_ADDRESS, "2606:4700::1111", "::ffff:8.8.8.8", "2002:808:808::1"]
)
def test_globally_routable_addresses_are_public(address: str) -> None:
    assert is_public(address)


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "[fd00::1]", "[::ffff:127.0.0.1]"]
)
async def test_an_address_literal_that_is_not_public_is_refused_before_a_request(
    host: str,
) -> None:
    with respx.mock(assert_all_called=False) as mock, pytest.raises(ImportRefusedError) as caught:
        await fetch_model(f"https://{host}/model.scad", limit=LIMIT)

    assert "not a public internet address" in str(caught.value)
    assert not mock.calls


async def test_a_name_that_resolves_into_the_cluster_is_refused(
    fake_dns: dict[str, list[str]],
) -> None:
    fake_dns["bambuddy.bambuddy.svc.cluster.local"] = ["10.43.0.12"]

    with respx.mock(assert_all_called=False) as mock, pytest.raises(ImportRefusedError) as caught:
        await fetch_model("https://bambuddy.bambuddy.svc.cluster.local/api", limit=LIMIT)

    # Word for word what a name that does not answer at all gets.
    assert str(caught.value) == str(unreachable("bambuddy.bambuddy.svc.cluster.local"))
    assert not mock.calls


async def test_a_name_with_any_private_address_is_refused(
    fake_dns: dict[str, list[str]],
) -> None:
    fake_dns["mixed.example.com"] = [PUBLIC_ADDRESS, "192.168.1.1"]

    with respx.mock(assert_all_called=False) as mock, pytest.raises(ImportRefusedError):
        await fetch_model("https://mixed.example.com/model.scad", limit=LIMIT)

    assert not mock.calls


class HungResolver:
    """A `getaddrinfo` that does not answer until released, under the real
    `resolve_host`, so the lookups run on the import's own threads."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.threads: list[str] = []
        self.returned = threading.Semaphore(0)

    def __call__(self, *args: object, **kwargs: object) -> list[object]:
        self.threads.append(threading.current_thread().name)
        try:
            self.release.wait(5)
            raise OSError("released")
        finally:
            self.returned.release()


@pytest.fixture
def hung_resolver(monkeypatch: pytest.MonkeyPatch) -> Iterator[HungResolver]:
    hung = HungResolver()
    monkeypatch.setattr(url_import, "resolve_host", real_resolve_host)
    monkeypatch.setattr(socket, "getaddrinfo", hung)
    monkeypatch.setattr(url_import, "RESOLVE_TIMEOUT", 0.05)
    yield hung
    hung.release.set()
    # Every hung lookup has to have given its thread back before the next test.
    for _ in hung.threads:
        assert hung.returned.acquire(timeout=5)


async def test_a_hung_resolver_is_unreachable_within_its_deadline_on_its_own_threads(
    hung_resolver: HungResolver,
) -> None:
    started = time.monotonic()
    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model("https://hangs.invalid/model.scad", limit=LIMIT)

    assert str(caught.value) == str(unreachable("hangs.invalid"))
    assert time.monotonic() - started < 1
    assert len(hung_resolver.threads) == 1
    assert hung_resolver.threads[0].startswith("import-dns")


async def test_with_every_resolver_thread_busy_a_lookup_is_refused_not_queued(
    hung_resolver: HungResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("first", "second"):
        with pytest.raises(ImportRefusedError):
            await fetch_model(f"https://{name}.invalid/model.scad", limit=LIMIT)
    assert len(hung_resolver.threads) == url_import.RESOLVER_THREADS

    # Long enough that waiting for a thread would show: a queued lookup would sit
    # out this whole deadline before it gave up.
    monkeypatch.setattr(url_import, "RESOLVE_TIMEOUT", 3.0)
    started = time.monotonic()
    # Busy, not the refusal: the lookup never started, so it says nothing about the
    # host, and the route turns it into its retryable 503.
    with pytest.raises(ResolverBusyError):
        await fetch_model("https://third.invalid/model.scad", limit=LIMIT)
    assert time.monotonic() - started < 1

    # Once the two threads are free: a queued third lookup would run now.
    hung_resolver.release.set()
    for _ in range(url_import.RESOLVER_THREADS):
        assert hung_resolver.returned.acquire(timeout=5)
    assert not hung_resolver.returned.acquire(timeout=0.2)
    assert len(hung_resolver.threads) == url_import.RESOLVER_THREADS
    hung_resolver.returned.release(url_import.RESOLVER_THREADS)
    # The threads came back, and with them the slots.
    assert url_import._RESOLVER_SLOTS.acquire(blocking=False)
    url_import._RESOLVER_SLOTS.release()


async def test_a_name_that_does_not_resolve_reads_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(host: str, port: int) -> list[str]:
        raise OSError("Name or service not known")

    monkeypatch.setattr(url_import, "resolve_host", fail)

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model("https://nowhere.example/model.scad", limit=LIMIT)

    assert str(caught.value) == str(unreachable("nowhere.example"))


@respx.mock
async def test_a_redirect_into_the_cluster_is_not_followed(
    fake_dns: dict[str, list[str]],
) -> None:
    fake_dns["internal.example.com"] = ["10.0.0.7"]
    respx.get("https://example.com/model.scad").mock(
        return_value=httpx.Response(302, headers={"Location": "https://internal.example.com/x"})
    )
    internal = respx.get("https://internal.example.com/x")
    metadata = respx.get("https://169.254.169.254/latest/meta-data/")

    with pytest.raises(ImportRefusedError):
        await fetch_model("https://example.com/model.scad", limit=LIMIT)
    respx.get("https://example.com/other.scad").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://169.254.169.254/latest/meta-data/"}
        )
    )
    with pytest.raises(ImportRefusedError):
        await fetch_model("https://example.com/other.scad", limit=LIMIT)

    assert not internal.called
    assert not metadata.called


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    """Stands in for the socket layer: records where it was asked to connect."""

    def __init__(self) -> None:
        self.connected: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connected.append((host, port))
        raise httpcore.ConnectError("recorded, not connected")


async def test_the_backend_connects_to_the_address_it_vetted_not_the_name() -> None:
    inner = _RecordingBackend()

    with pytest.raises(httpcore.ConnectError):
        await PublicOnlyBackend(inner).connect_tcp("example.com", 443)

    assert inner.connected == [(PUBLIC_ADDRESS, 443)]


async def test_the_backend_refuses_a_private_address_without_connecting(
    fake_dns: dict[str, list[str]],
) -> None:
    fake_dns["internal.example.com"] = ["10.0.0.7"]
    inner = _RecordingBackend()

    with pytest.raises(ImportRefusedError):
        await PublicOnlyBackend(inner).connect_tcp("internal.example.com", 443)

    assert inner.connected == []


async def test_a_name_that_rebinds_after_the_first_check_is_refused_at_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No respx here: the request goes through the real transport, so this is also
    what proves the backend is still wired into the pool httpx builds."""
    answers = [[PUBLIC_ADDRESS], ["127.0.0.1"]]
    asked: list[str] = []

    async def rebinding(host: str, port: int) -> list[str]:
        asked.append(host)
        return answers[len(asked) - 1]

    monkeypatch.setattr(url_import, "resolve_host", rebinding)

    with pytest.raises(ImportRefusedError) as caught:
        await fetch_model("https://rebind.invalid/model.scad", limit=LIMIT)

    assert str(caught.value) == str(unreachable("rebind.invalid"))
    # Once by the hook, once by the backend: an unwired backend would have let the
    # socket layer resolve the name itself, and failed the same way for another reason.
    assert asked == ["rebind.invalid", "rebind.invalid"]


@pytest.mark.parametrize(
    "url",
    [
        "https://makerworld.com/en/models/1398039-parametric-customizable-keychain-openscad",
        "https://www.makerworld.com/models/1398039",
        "https://makerworld.com./models/1398039",
    ],
)
async def test_a_makerworld_model_page_is_refused_with_the_way_round_it(url: str) -> None:
    with respx.mock(assert_all_called=False) as mock, pytest.raises(ImportRefusedError) as caught:
        await fetch_model(url, limit=LIMIT)
    message = str(caught.value)
    assert "MakerWorld" in message
    assert "Upload" in message
    assert not mock.calls


def test_the_fetch_budget_ends_before_the_gateway_does() -> None:
    """#966: past Envoy's 15 s route timeout the client already has a plain-text 504,
    yet an import that finishes behind it still creates the model, so a retry 409s.
    The fetch's whole budget must end first, with room left for the parse check
    `_create` runs after it, so a slow host gets the importer's own 422."""
    gateway_route_timeout = 15.0
    parse_check_headroom = 5.0
    assert url_import.IMPORT_TIMEOUT + parse_check_headroom <= gateway_route_timeout
