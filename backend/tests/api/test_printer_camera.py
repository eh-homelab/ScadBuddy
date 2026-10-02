"""Issue #796 — a printer's current camera frame, proxied from Bambuddy.

Bambuddy's snapshot route takes no API key: it wants a camera stream token in the
query (``RequireCameraStreamTokenIfAuthEnabled``, ``camera.py`` at v1.2.5.6), minted by
``POST /printers/camera/stream-token`` under ``camera:view``, which an API key holds
with ``can_read_status``. Both calls stay server-side; the browser sees a JPEG."""

from __future__ import annotations

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
