"""The zero-Bambuddy harness (#3818 tier A, Task 1): it fails on any Bambuddy request
or client span, and leaves every other host alone."""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
import respx
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from scadbuddy.core.tracing import span
from tests.support.no_bambuddy import BambuddyCalledError, forbid_bambuddy


async def test_a_bambuddy_request_is_named(spans: InMemorySpanExporter) -> None:
    with (
        pytest.raises(BambuddyCalledError, match="/api/v1/printers/"),
        forbid_bambuddy(spans, "http://bambuddy.test"),
    ):
        async with httpx.AsyncClient() as http:
            await http.get("http://bambuddy.test/api/v1/printers/")


async def test_a_bambuddy_request_fails_the_caller(spans: InMemorySpanExporter) -> None:
    """A swallowed call is still caught by the record; an unswallowed one fails at once."""
    with (
        pytest.raises(BambuddyCalledError) as caught,
        forbid_bambuddy(spans, "http://bambuddy.test/") as record,
    ):
        async with httpx.AsyncClient() as http:
            answer = await http.get("http://bambuddy.test/api/v1/version")
        assert answer.status_code == 599
    assert [request.url.path for request in record.requests] == ["/api/v1/version"]
    assert "GET /api/v1/version" in str(caught.value)


def test_a_sync_bambuddy_request_is_named(spans: InMemorySpanExporter) -> None:
    with (
        pytest.raises(BambuddyCalledError, match="POST /api/v1/library/files/"),
        forbid_bambuddy(spans, "http://bambuddy.test"),
        httpx.Client() as http,
    ):
        http.post("http://bambuddy.test/api/v1/library/files/", content=b"x")


async def test_a_bambuddy_span_is_named(spans: InMemorySpanExporter) -> None:
    with (
        pytest.raises(BambuddyCalledError, match=r"bambuddy\.printers"),
        forbid_bambuddy(spans, "http://unused.test"),
        span("bambuddy.printers", kind=SpanKind.CLIENT),
    ):
        pass


async def test_spans_from_before_the_window_do_not_count(spans: InMemorySpanExporter) -> None:
    with span("outer"), span("bambuddy.printers", kind=SpanKind.CLIENT):
        pass
    with forbid_bambuddy(spans, "http://bambuddy.test") as record:
        pass
    assert record.spans == []


async def test_other_hosts_pass(spans: InMemorySpanExporter, respx_mock: respx.MockRouter) -> None:
    respx_mock.get("http://api.test/x").respond(200)
    with forbid_bambuddy(spans, "http://bambuddy.test"):
        async with httpx.AsyncClient() as http:
            assert (await http.get("http://api.test/x")).status_code == 200


async def test_an_outer_mock_of_bambuddy_does_not_hide_a_call(
    spans: InMemorySpanExporter, respx_mock: respx.MockRouter
) -> None:
    """A test that mocks Bambuddy for its setup must still see the window's calls fail."""
    respx_mock.get("http://bambuddy.test/api/v1/printers/").respond(200, json=[])
    with (
        pytest.raises(BambuddyCalledError, match="/api/v1/printers/"),
        forbid_bambuddy(spans, "http://bambuddy.test"),
    ):
        async with httpx.AsyncClient() as http:
            assert (await http.get("http://bambuddy.test/api/v1/printers/")).status_code == 599


async def test_a_router_opened_inside_the_window_still_answers(
    spans: InMemorySpanExporter,
) -> None:
    with (
        forbid_bambuddy(spans, "http://bambuddy.test"),
        respx.mock(assert_all_called=False) as inner,
    ):
        inner.get("http://api.test/y").respond(204)
        async with httpx.AsyncClient() as http:
            assert (await http.get("http://api.test/y")).status_code == 204


async def test_an_outer_strict_router_still_refuses_what_it_does_not_mock(
    spans: InMemorySpanExporter, respx_mock: respx.MockRouter
) -> None:
    with forbid_bambuddy(spans, "http://bambuddy.test"):
        async with httpx.AsyncClient() as http:
            with pytest.raises(respx.models.AllMockedAssertionError):
                await http.get("http://elsewhere.test/")


@pytest.fixture
def local_server() -> Iterator[str]:
    class Ok(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Ok)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_an_unmocked_host_reaches_the_network(
    spans: InMemorySpanExporter, local_server: str
) -> None:
    """With no other router, a request elsewhere (the API's own internal URL) is sent."""
    with forbid_bambuddy(spans, "http://bambuddy.test"):
        async with httpx.AsyncClient() as http:
            answer = await http.get(f"{local_server}/healthz")
    assert answer.text == "ok"


async def test_a_host_that_only_shares_a_prefix_passes(
    spans: InMemorySpanExporter, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("http://bambuddy.test.example/x").respond(200)
    with forbid_bambuddy(spans, "http://bambuddy.test"):
        async with httpx.AsyncClient() as http:
            assert (await http.get("http://bambuddy.test.example/x")).status_code == 200
