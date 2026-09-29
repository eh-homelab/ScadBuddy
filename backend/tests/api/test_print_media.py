"""Issues #307 and #306 — the archive media proxy. The browser gets Bambuddy's bytes,
never its key or address, and a seek is a ``Range`` request that reaches Bambuddy and
comes back as a 206. Only an archive one of ScadBuddy's outputs printed is served."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore
from tests.api.test_send import API, BASE, configure

# The gate reads the links, which live in Postgres (#306).
pytestmark = pytest.mark.requires_postgres

LINKED = 35


def print_links(client: TestClient) -> PrintLinkStore:
    links: PrintLinkStore = getattr(client.app.state, STATE_ATTR).print_links  # type: ignore[attr-defined]
    return links


@pytest.fixture(autouse=True)
def _linked(client: TestClient) -> None:
    """Archive 35 is a print of one of ScadBuddy's outputs."""
    asyncio.run(
        print_links(client).record("a" * 32, PrintLink(archive_id=LINKED, matched_by="queue_item"))
    )


VIDEO_HEADERS = {
    "Content-Type": "video/mp4",
    "Accept-Ranges": "bytes",
    "ETag": '"1790488620"',
}


@respx.mock
def test_a_seek_reaches_bambuddy_and_comes_back_as_206(client: TestClient) -> None:
    configure(client)
    route = respx.get(f"{API}/archives/35/timelapse").mock(
        return_value=httpx.Response(
            206,
            content=b"v" * 100,
            headers={**VIDEO_HEADERS, "Content-Range": "bytes 100-199/2143595"},
        )
    )

    response = client.get(
        "/api/v1/prints/35/timelapse", headers={"Range": "bytes=100-199", "If-Range": '"1"'}
    )

    upstream = route.calls.last.request
    assert upstream.headers["Range"] == "bytes=100-199"
    assert upstream.headers["If-Range"] == '"1"'
    assert upstream.headers["X-API-Key"] == "s3cret"
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 100-199/2143595"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["etag"] == '"1790488620"'
    assert response.content == b"v" * 100


@respx.mock
def test_the_key_and_bambuddys_other_headers_stay_server_side(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/archives/35/timelapse").mock(
        return_value=httpx.Response(
            200,
            content=b"whole",
            headers={**VIDEO_HEADERS, "Set-Cookie": "session=x", "Server": "uvicorn"},
        )
    )

    response = client.get("/api/v1/prints/35/timelapse")

    assert response.status_code == 200
    assert response.content == b"whole"
    assert "set-cookie" not in response.headers
    assert "s3cret" not in str(response.headers)


@pytest.mark.parametrize(
    ("path", "upstream"),
    [
        (
            "photos/finish_20260927_015703_93372185.jpg",
            "photos/finish_20260927_015703_93372185.jpg",
        ),
        ("thumbnail", "thumbnail"),
        ("plates/2/thumbnail", "plate-thumbnail/2"),
        ("files/sliced", "download"),
        ("files/source", "source"),
    ],
)
@respx.mock
def test_each_archive_file_is_proxied(client: TestClient, path: str, upstream: str) -> None:
    configure(client)
    route = respx.get(f"{API}/archives/35/{upstream}").mock(
        return_value=httpx.Response(200, content=b"bytes", headers={"Content-Type": "image/png"})
    )

    response = client.get(f"/api/v1/prints/35/{path}")

    assert route.called
    assert response.status_code == 200
    assert response.content == b"bytes"


@pytest.mark.parametrize(
    ("path", "upstream"),
    [("thumbnail", "thumbnail"), ("plates/2/thumbnail", "plate-thumbnail/2")],
)
@respx.mock
def test_each_library_file_image_is_proxied(client: TestClient, path: str, upstream: str) -> None:
    """#313: the Print dialog shows a library file's images without Bambuddy's key."""
    configure(client)
    route = respx.get(f"{API}/library/files/89/{upstream}").mock(
        return_value=httpx.Response(200, content=b"bytes", headers={"Content-Type": "image/png"})
    )

    response = client.get(f"/api/v1/print/library/89/{path}")

    assert route.called
    assert route.calls.last.request.headers["X-API-Key"] == "s3cret"
    assert response.status_code == 200
    assert response.content == b"bytes"
    assert response.headers["content-type"] == "image/png"


@pytest.mark.parametrize("path", ["thumbnail", "plates/2/thumbnail"])
@respx.mock
def test_a_missing_library_image_is_a_404(client: TestClient, path: str) -> None:
    configure(client)
    respx.route(method="GET", path__regex=r"/api/v1/library/files/89/").mock(
        return_value=httpx.Response(404, json={"detail": "Thumbnail not found"})
    )

    response = client.get(f"/api/v1/print/library/89/{path}")

    assert response.status_code == 404


@respx.mock
def test_a_seek_past_the_end_comes_back_as_416_with_the_length(client: TestClient) -> None:
    # A media element seeking past the end must learn the real length, not a 502.
    configure(client)
    respx.get(f"{API}/archives/35/timelapse").mock(
        return_value=httpx.Response(416, content=b"", headers={"Content-Range": "bytes */2143595"})
    )

    response = client.get("/api/v1/prints/35/timelapse", headers={"Range": "bytes=9999999-"})

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */2143595"


def test_no_route_serves_an_arbitrary_library_file(client: TestClient) -> None:
    # Attachments (#309) come with their own check; until then the library is not
    # reachable through the print proxy.
    configure(client)
    assert client.get("/api/v1/prints/35/attachments/77").status_code == 404


@respx.mock
def test_a_missing_timelapse_is_a_404_problem(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/archives/35/timelapse").mock(
        return_value=httpx.Response(404, json={"detail": "Timelapse not found"})
    )

    response = client.get("/api/v1/prints/35/timelapse")

    assert response.status_code == 404
    assert "Timelapse not found" in response.json()["detail"]


def test_a_photo_name_cannot_walk_out_of_the_archive(client: TestClient) -> None:
    configure(client)
    assert client.get("/api/v1/prints/35/photos/..%2Fsecret").status_code in (404, 422)


def test_without_bambuddy_configured_it_says_so(client: TestClient) -> None:
    response = client.get("/api/v1/prints/35/timelapse")
    assert response.status_code == 409


@respx.mock
def test_an_archive_no_output_printed_is_a_404_and_bambuddy_is_not_asked(
    client: TestClient,
) -> None:
    configure(client)
    route = respx.get(f"{API}/archives/36/timelapse").mock(return_value=httpx.Response(200))

    response = client.get("/api/v1/prints/36/timelapse")

    assert response.status_code == 404
    assert not route.called


@respx.mock
def test_head_answers_the_headers_and_no_body(client: TestClient) -> None:
    configure(client)
    route = respx.get(f"{API}/archives/35/timelapse").mock(
        return_value=httpx.Response(
            200, content=b"v" * 50, headers={**VIDEO_HEADERS, "Content-Length": "50"}
        )
    )

    response = client.head("/api/v1/prints/35/timelapse")

    assert route.called
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.content == b""


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@respx.mock
def test_the_proxy_is_read_only(client: TestClient, method: str) -> None:
    configure(client)
    route = respx.route(host="bambuddy.test").mock(return_value=httpx.Response(200))

    response = client.request(method, "/api/v1/prints/35/timelapse")

    assert response.status_code == 405
    assert not route.called


@respx.mock
def test_only_range_and_if_range_reach_bambuddy(client: TestClient) -> None:
    configure(client)
    route = respx.get(f"{API}/archives/35/photos/finish_1.jpg").mock(
        return_value=httpx.Response(200, content=b"jpg", headers={"Content-Type": "image/jpeg"})
    )

    client.get(
        "/api/v1/prints/35/photos/finish_1.jpg",
        headers={
            "Range": "bytes=0-9",
            "If-Range": '"1"',
            "Cookie": "scadbuddy=session",
            "Authorization": "Bearer browser-token",
            "X-API-Key": "from-the-browser",
            "X-Forwarded-For": "10.0.0.9",
            "Referer": "https://scadbuddy.example/prints/35",
        },
    )

    sent = route.calls.last.request.headers
    assert sent["Range"] == "bytes=0-9"
    assert sent["If-Range"] == '"1"'
    assert sent["X-API-Key"] == "s3cret", "the configured key, never the browser's"
    for name in ("Cookie", "Authorization", "X-Forwarded-For", "Referer"):
        assert name not in sent, name


@respx.mock
def test_bambuddys_address_never_reaches_the_browser(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/archives/35/download").mock(
        return_value=httpx.Response(
            200,
            content=b"3mf",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": 'attachment; filename="name-keychain.gcode.3mf"',
                "Location": f"{BASE}/api/v1/archives/35/download",
                "Content-Location": f"{BASE}/archive/35.3mf",
                "Link": f"<{BASE}/api/v1/archives/35>; rel=up",
            },
        )
    )

    response = client.get("/api/v1/prints/35/files/sliced")

    assert response.status_code == 200
    assert response.headers["content-disposition"].endswith('"name-keychain.gcode.3mf"')
    assert "bambuddy.test" not in str(response.headers)
    assert "s3cret" not in str(response.headers)
