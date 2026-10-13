"""Issue #796 — a printer's current camera frame, proxied from Bambuddy.

Bambuddy's snapshot route takes no API key: it wants a camera stream token in the
query (``RequireCameraStreamTokenIfAuthEnabled``, ``camera.py`` at v1.2.5.6), minted by
``POST /printers/camera/stream-token`` under ``camera:view``, which an API key holds
with ``can_read_status``. Both calls stay server-side; the browser sees a JPEG."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import respx
from fastapi.testclient import TestClient

from tests.api.test_send import API, configure

JPEG = b"\xff\xd8\xff\xe0camera-frame\xff\xd9"


def token_route() -> respx.Route:
    return respx.post(f"{API}/printers/camera/stream-token").mock(
        return_value=httpx.Response(200, json={"token": "t0k3n"})
    )


@respx.mock
def test_the_frame_comes_back_as_a_jpeg_and_the_token_stays_server_side(
    client: TestClient,
) -> None:
    configure(client)
    minted = token_route()
    snapshot = respx.get(f"{API}/printers/7/camera/snapshot").mock(
        return_value=httpx.Response(
            200,
            content=JPEG,
            headers={"Content-Type": "image/jpeg", "Set-Cookie": "session=x"},
        )
    )

    response = client.get("/api/v1/print/printers/7/camera")

    assert minted.calls.last.request.headers["X-API-Key"] == "s3cret"
    assert snapshot.calls.last.request.url.params["token"] == "t0k3n"
    assert response.status_code == 200
    assert response.content == JPEG
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in response.headers
    assert "t0k3n" not in response.text


@respx.mock
def test_a_key_without_read_status_names_the_scope(client: TestClient) -> None:
    configure(client)
    respx.post(f"{API}/printers/camera/stream-token").mock(
        return_value=httpx.Response(403, json={"detail": "camera:view required"})
    )

    response = client.get("/api/v1/print/printers/7/camera")

    assert response.status_code == 409
    assert response.json()["required_scope"] == "Read Status"


@respx.mock
def test_a_camera_that_gives_no_frame_is_a_bad_gateway(client: TestClient) -> None:
    configure(client)
    token_route()
    respx.get(f"{API}/printers/7/camera/snapshot").mock(
        return_value=httpx.Response(503, json={"detail": "Failed to capture camera frame."})
    )

    response = client.get("/api/v1/print/printers/7/camera")

    assert response.status_code == 502
    assert "Failed to capture camera frame." in response.json()["detail"]


@respx.mock
def test_an_unknown_printer_is_a_404(client: TestClient) -> None:
    configure(client)
    token_route()
    respx.get(f"{API}/printers/99/camera/snapshot").mock(
        return_value=httpx.Response(404, json={"detail": "Printer not found"})
    )

    assert client.get("/api/v1/print/printers/99/camera").status_code == 404


# --- #981: the live stream ----------------------------------------------------------

MJPEG = "multipart/x-mixed-replace; boundary=frame"


class Frames(httpx.AsyncByteStream):
    """Bambuddy's MJPEG stream: a few frames, and whether ScadBuddy closed it."""

    def __init__(self, count: int = 3) -> None:
        self.count = count
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index in range(self.count):
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                + JPEG
                + str(index).encode()
                + b"\r\n"
            )

    async def aclose(self) -> None:
        self.closed = True


@respx.mock
def test_the_live_stream_is_bambuddys_mjpeg_and_its_token_stays_server_side(
    client: TestClient,
) -> None:
    configure(client)
    minted = token_route()
    frames = Frames()
    stream = respx.get(f"{API}/printers/7/camera/stream").mock(
        return_value=httpx.Response(
            200, stream=frames, headers={"Content-Type": MJPEG, "Set-Cookie": "session=x"}
        )
    )

    response = client.get("/api/v1/print/printers/7/camera/stream?fps=8")

    assert response.status_code == 200, response.text
    assert minted.called
    sent = stream.calls.last.request
    assert sent.url.params["token"] == "t0k3n"
    assert sent.url.params["fps"] == "8"
    assert response.headers["content-type"] == MJPEG
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert response.content.count(b"--frame") == 3
    assert b"t0k3n" not in response.content
    # The viewer is gone, so Bambuddy's stream is closed; /camera/stop is never called
    # (respx refuses any route it does not mock).
    assert frames.closed


@respx.mock
def test_a_refused_stream_is_an_error_not_an_empty_stream(client: TestClient) -> None:
    configure(client)
    token_route()
    respx.get(f"{API}/printers/99/camera/stream").mock(
        return_value=httpx.Response(404, json={"detail": "Printer not found"})
    )

    response = client.get("/api/v1/print/printers/99/camera/stream")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_fps_is_capped(client: TestClient) -> None:
    assert client.get("/api/v1/print/printers/7/camera/stream?fps=60").status_code == 422
