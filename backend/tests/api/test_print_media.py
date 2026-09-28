"""Issue #307 — the archive media proxy. The browser gets Bambuddy's bytes, never its
key, and a seek is a ``Range`` request that reaches Bambuddy and comes back as a 206."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_send import API, configure

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
