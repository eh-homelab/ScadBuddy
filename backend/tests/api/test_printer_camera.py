"""Issue #796 — a printer's current camera frame, proxied from Bambuddy.

Bambuddy's snapshot route takes no API key: it wants a camera stream token in the
query (``RequireCameraStreamTokenIfAuthEnabled``, ``camera.py`` at v1.2.5.6), minted by
``POST /printers/camera/stream-token`` under ``camera:view``, which an API key holds
with ``can_read_status``. Both calls stay server-side; the browser sees a JPEG."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator
from typing import cast

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from starlette.requests import ClientDisconnect

from scadbuddy.api import printing
from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.bambuddy import client as bambuddy_client
from scadbuddy.bambuddy.client import mjpeg_type
from tests.api.test_send import API, configure


class _AsyncStream(httpx.AsyncByteStream):
    """A body served chunk by chunk, as a slow upstream would."""

    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk


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
    """Bambuddy's MJPEG stream: a few frames, and whether ScadBuddy closed it. With
    ``fail``, the read raises it after the frames, as a stalled camera does."""

    def __init__(
        self, count: int = 3, fail: Exception | None = None, *, hold: bool = False
    ) -> None:
        self.count = count
        self.fail = fail
        #: After the frames, wait as a live camera does, until the viewer leaves.
        self.hold = hold
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index in range(self.count):
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                + JPEG
                + str(index).encode()
                + b"\r\n"
            )
        if self.fail is not None:
            raise self.fail
        if self.hold:
            await asyncio.sleep(60)

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
def test_a_camera_that_stalls_mid_stream_ends_it_and_closes_bambuddys_stream(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """#2752 review: the frames are read outside ``camera_stream``'s try, so a stall is
    a raw ``httpx.ReadTimeout``. It ends the stream after the frames already sent, is
    logged, and closes Bambuddy's stream, rather than escaping as an unhandled error."""
    configure(client)
    token_route()
    frames = Frames(count=2, fail=httpx.ReadTimeout("camera stalled"))
    respx.get(f"{API}/printers/7/camera/stream").mock(
        return_value=httpx.Response(200, stream=frames, headers={"Content-Type": MJPEG})
    )

    with caplog.at_level("INFO", logger="scadbuddy.api.printing"):
        response = client.get("/api/v1/print/printers/7/camera/stream")

    assert response.status_code == 200
    assert response.content.count(b"--frame") == 2
    assert frames.closed
    assert any(record.message == "camera stream ended" for record in caplog.records)


@respx.mock
def test_a_printer_serves_a_few_streams_at_once_and_refuses_more(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2752 review: each stream holds a Bambuddy connection for as long as it is
    watched, so past the cap a viewer is told so, and Bambuddy is never asked."""
    configure(client)
    monkeypatch.setattr(
        printing, "_camera_streams", Counter({7: printing.CAMERA_STREAMS_PER_PRINTER})
    )
    minted = token_route()
    stream = respx.get(f"{API}/printers/7/camera/stream")

    response = client.get("/api/v1/print/printers/7/camera/stream")

    assert response.status_code == 429
    assert response.json()["type"] == printing.CAMERA_STREAMS_BUSY_PROBLEM
    assert response.headers["retry-after"] == "5"
    assert not minted.called and not stream.called
    # Another printer's camera is not held up by this one's viewers.
    other = respx.get(f"{API}/printers/8/camera/stream").mock(
        return_value=httpx.Response(200, stream=Frames(), headers={"Content-Type": MJPEG})
    )
    assert client.get("/api/v1/print/printers/8/camera/stream").status_code == 200
    assert other.called


@respx.mock
def test_a_stream_gives_its_place_back_however_it_ends(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Served to the end, stalled, or refused by Bambuddy: none keeps a place."""
    configure(client)
    monkeypatch.setattr(printing, "_camera_streams", Counter())
    token_route()
    respx.get(f"{API}/printers/7/camera/stream").mock(
        side_effect=[
            httpx.Response(200, stream=Frames(), headers={"Content-Type": MJPEG}),
            httpx.Response(
                200,
                stream=Frames(count=1, fail=httpx.ReadTimeout("stalled")),
                headers={"Content-Type": MJPEG},
            ),
            httpx.Response(404, json={"detail": "Printer not found"}),
        ]
    )

    for _ in range(3):
        client.get("/api/v1/print/printers/7/camera/stream")

    assert printing._camera_streams[7] == 0


@pytest.mark.parametrize("leaves", ["closed", "cancelled"])
@respx.mock
def test_a_viewer_leaving_ends_the_stream_span_cleanly(
    client: TestClient, spans: InMemorySpanExporter, leaves: str
) -> None:
    """#3206: a viewer closing the dialog ends the body mid-stream. Starlette closes the
    generator (``GeneratorExit``) or cancels its task (``CancelledError``). That is the
    normal end of a live view, never a failed Bambuddy call on the dashboards. Driven on
    the route's own body, as the server would, because a test client's close never
    reaches it."""
    configure(client)
    store = getattr(client.app.state, STATE_ATTR).settings_store  # type: ignore[attr-defined]
    token_route()
    frames = Frames(count=1, hold=True)
    respx.get(f"{API}/printers/7/camera/stream").mock(
        return_value=httpx.Response(200, stream=frames, headers={"Content-Type": MJPEG})
    )
    spans.clear()

    async def watch_then_leave() -> None:
        response = await printing.stream_printer_camera(7, store, fps=5)
        body = cast(AsyncGenerator[bytes, None], response.body_iterator)
        assert b"--frame" in await anext(body)
        if leaves == "closed":
            await body.aclose()
            return
        reading = asyncio.ensure_future(anext(body))
        await asyncio.sleep(0.05)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        await body.aclose()

    # The request's own span, as the HTTP middleware would open it: the sampler follows it.
    with trace.get_tracer(__name__).start_as_current_span("GET camera stream"):
        asyncio.run(watch_then_leave())

    (stream,) = [
        s for s in spans.get_finished_spans() if s.name == "bambuddy.printers.camera.stream"
    ]
    assert stream.status.status_code != StatusCode.ERROR, stream.status
    assert frames.closed
    assert printing._camera_streams[7] == 0


@respx.mock
def test_a_viewer_gone_before_the_first_byte_releases_the_stream(client: TestClient) -> None:
    """#2752 review: a viewer who closes the dialog while the stream is still opening
    fails the response's start, so the body never runs and Starlette skips any
    ``background``. The stream slot and Bambuddy's stream are still released."""
    configure(client)
    store = getattr(client.app.state, STATE_ATTR).settings_store  # type: ignore[attr-defined]
    token_route()
    frames = Frames(count=1, hold=True)
    respx.get(f"{API}/printers/7/camera/stream").mock(
        return_value=httpx.Response(200, stream=frames, headers={"Content-Type": MJPEG})
    )

    async def receive() -> dict[str, object]:
        await asyncio.sleep(60)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        raise OSError("the viewer is gone")

    async def open_then_leave() -> None:
        response = await printing.stream_printer_camera(7, store, fps=5)
        assert printing._camera_streams[7] == 1
        scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)  # type: ignore[arg-type]

    asyncio.run(open_then_leave())

    assert printing._camera_streams[7] == 0
    assert frames.closed


@pytest.mark.parametrize("answer", [{"json": {}}, {"content": b"not json"}])
@pytest.mark.parametrize("path", ["/camera", "/camera/stream"])
@respx.mock
def test_a_token_answer_without_a_token_is_a_bad_gateway(
    client: TestClient, answer: dict[str, object], path: str
) -> None:
    """#2752 review: an unexpected stream-token body is Bambuddy's fault, a 502, not a
    bare ``KeyError`` from ScadBuddy."""
    configure(client)
    respx.post(f"{API}/printers/camera/stream-token").mock(
        return_value=httpx.Response(200, **answer)  # type: ignore[arg-type]
    )

    response = client.get(f"/api/v1/print/printers/7{path}")

    assert response.status_code == 502
    assert "no camera token" in response.json()["detail"]


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


@respx.mock
def test_a_stream_is_never_served_as_the_type_bambuddy_labels_it(client: TestClient) -> None:
    """ScadBuddy's origin serves the bytes, so an upstream ``text/html`` (Bambuddy, or an
    external camera behind it) must not become HTML here."""
    configure(client)
    token_route()
    respx.get(f"{API}/printers/7/camera/stream").mock(
        return_value=httpx.Response(
            200, stream=Frames(1), headers={"Content-Type": "text/html; charset=utf-8"}
        )
    )

    response = client.get("/api/v1/print/printers/7/camera/stream")

    assert response.headers["content-type"] == MJPEG
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    ("upstream", "served"),
    [
        (MJPEG, MJPEG),
        (
            'multipart/x-mixed-replace; boundary="cam42"',
            "multipart/x-mixed-replace; boundary=cam42",
        ),
        ("text/html", MJPEG),
        ("multipart/x-mixed-replace; boundary=frame\r\nX-Evil: 1", MJPEG),
        ("multipart/x-mixed-replace; boundary=<script>", MJPEG),
        (None, MJPEG),
    ],
)
def test_only_a_multipart_mjpeg_type_with_a_plain_boundary_is_kept(
    upstream: str | None, served: str
) -> None:
    assert mjpeg_type(upstream) == served


@respx.mock
def test_a_snapshot_refused_after_the_mint_maps_as_any_refusal(client: TestClient) -> None:
    """#1006: the token minted, then the snapshot refused it (expired in a slow capture)."""
    configure(client)
    token_route()
    respx.get(f"{API}/printers/7/camera/snapshot").mock(
        return_value=httpx.Response(403, json={"detail": "Invalid stream token"})
    )

    response = client.get("/api/v1/print/printers/7/camera")

    assert response.status_code == 409
    assert response.json()["required_scope"] == "Read Status"


@respx.mock
def test_a_mint_with_no_token_is_a_bad_gateway(client: TestClient) -> None:
    """#1006: a 200 whose body is not ``{"token": str}`` is Bambuddy's fault, named."""
    configure(client)
    snapshot = respx.get(f"{API}/printers/7/camera/snapshot")
    bodies: list[object] = [{}, None, [], {"token": 5}]
    answers = [httpx.Response(200, json=body) for body in bodies]
    # Not JSON at all: a proxy's error page.
    answers.append(httpx.Response(200, content=b"<html>", headers={"Content-Type": "text/html"}))
    for answer in answers:
        respx.post(f"{API}/printers/camera/stream-token").mock(return_value=answer)
        response = client.get("/api/v1/print/printers/7/camera")
        assert response.status_code == 502, answer.content
        assert "no camera token" in response.json()["detail"]
    assert not snapshot.called


@respx.mock
def test_a_snapshot_answered_416_is_refused_not_served_as_a_frame(client: TestClient) -> None:
    """#3397: nothing asks the snapshot for a range, so a 416 is a refusal, never a frame."""
    configure(client)
    token_route()
    respx.get(f"{API}/printers/7/camera/snapshot").mock(
        return_value=httpx.Response(
            416, content=JPEG, headers={"Content-Type": "image/jpeg", "Content-Range": "*/9"}
        )
    )

    response = client.get("/api/v1/print/printers/7/camera")

    # Mapped as any unexpected answer is: Bambuddy's fault, naming the 416.
    assert response.status_code == 502, response.text
    assert response.json()["bambuddy_status"] == 416


@respx.mock
def test_a_frame_past_the_cap_is_refused_before_it_is_all_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter
) -> None:
    """#1006: the frame is read with a cap, not buffered whole: the read stops at the
    chunk that passes it, and the chunks after it are never pulled. #3397: an oversized
    frame is Bambuddy's doing, so the snapshot call's span is failed."""
    monkeypatch.setattr(bambuddy_client, "MAX_CAMERA_FRAME_BYTES", 64)
    configure(client)
    token_route()
    pulled: list[int] = []

    async def chunks() -> AsyncIterator[bytes]:
        for index in range(4):
            pulled.append(index)
            yield b"x" * 40

    respx.get(f"{API}/printers/7/camera/snapshot").mock(
        return_value=httpx.Response(
            200, stream=_AsyncStream(chunks()), headers={"Content-Type": "image/jpeg"}
        )
    )

    spans.clear()
    response = client.get("/api/v1/print/printers/7/camera")

    assert response.status_code == 502
    assert "over 64 bytes" in response.json()["detail"]
    assert pulled == [0, 1], "the third and fourth chunks are never read"
    (snapshot,) = (
        s for s in spans.get_finished_spans() if s.name == "bambuddy.printers.camera.snapshot"
    )
    assert snapshot.status.status_code == StatusCode.ERROR, snapshot.status
    # No message is recorded (`core/trace_scrub.py`): the detail names the printer.
    assert not snapshot.status.description
