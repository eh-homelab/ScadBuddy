"""The API's live camera proxy (camera live view, phase 3): WHEP and LL-HLS, forwarded to the
live edge (`api/camera.py`). The edge is an ``httpx.MockTransport`` here; the last test walks
a real MediaMTX edge."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from scadbuddy.api import camera as camera_api
from scadbuddy.auth.check import SIGNED_OUT
from scadbuddy.core.settings import Settings
from scadbuddy.printers.component import PRINTERS
from tests.api.test_auth_enforce import PUBLIC, _app, _configure
from tests.api.test_camera_routes import _make_printer, _state
from tests.auth.fake_oidc import CLIENT_ID, ISSUER, FakeIssuer
from tests.camera.conftest import Media, Spawn, media, spawn  # noqa: F401 (fixtures)
from tests.camera.test_e2e_mediamtx import (
    DEPLOY,
    EDGE_PASS,
    EDGE_USER,
    publishing,
    start_mediamtx,
    until,
)

pytestmark = pytest.mark.requires_postgres

WHEP_BASE = "http://edge-whep:8889"
HLS_BASE = "http://edge-hls:8888"
SESSION = "5718e681-89ec-4fde-84be-42b9f2d60a14"
OFFER = b"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
ANSWER = b"v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n"

Handler = Callable[[httpx.Request], httpx.Response]


class Edge:
    """The edge's two ports, as a MockTransport that records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answer: Handler = lambda request: httpx.Response(200, content=b"#EXTM3U\n")

    def handle(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self.answer(request)


def _configure_edge(client: TestClient, **update: object) -> None:
    state = _state(client)
    values: dict[str, object] = {
        "camera_edge_whep_url": WHEP_BASE,
        "camera_edge_hls_url": HLS_BASE,
        "camera_edge_read_user": EDGE_USER,
        "camera_edge_read_pass": SecretStr(EDGE_PASS),
    }
    state.settings = state.settings.model_copy(update=values | update)


@pytest.fixture
def edge(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Edge:
    fake = Edge()
    monkeypatch.setattr(camera_api, "_edge_transport", httpx.MockTransport(fake.handle))
    _configure_edge(client)
    return fake


@pytest.fixture
def printer(client: TestClient) -> str:
    return _make_printer(client, recorded=True)


def _basic(request: httpx.Request) -> str:
    return request.headers["authorization"]


EXPECTED_AUTH = "Basic " + base64.b64encode(f"{EDGE_USER}:{EDGE_PASS}".encode()).decode()


# WHEP.


def test_the_offer_reaches_the_edge_and_the_location_is_rewritten(
    client: TestClient, edge: Edge, printer: str
) -> None:
    edge.answer = lambda request: httpx.Response(
        201,
        content=ANSWER,
        headers={
            "Content-Type": "application/sdp",
            "Location": f"/{printer}/whep/{SESSION}",
            "ETag": "*",
            "Link": '<stun:stun.example:3478>; rel="ice-server"',
            "Set-Cookie": "x=1",
            "Access-Control-Allow-Origin": "*",
        },
    )
    response = client.post(
        f"/api/v1/camera/live/{printer}/whep",
        content=OFFER,
        headers={"Content-Type": "application/sdp", "Cookie": "scadbuddy_session=abc"},
    )
    assert response.status_code == 201, response.text
    assert response.content == ANSWER
    assert response.headers["content-type"] == "application/sdp"
    assert response.headers["location"] == f"/api/v1/camera/live/{printer}/whep/{SESSION}"
    assert response.headers["etag"] == "*"
    assert response.headers["link"] == '<stun:stun.example:3478>; rel="ice-server"'
    assert "set-cookie" not in response.headers
    assert "access-control-allow-origin" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    [sent] = edge.requests
    assert sent.method == "POST"
    assert str(sent.url) == f"{WHEP_BASE}/{printer}/whep"
    assert sent.content == OFFER
    assert sent.headers["content-type"] == "application/sdp"
    assert _basic(sent) == EXPECTED_AUTH
    assert "cookie" not in sent.headers


def test_the_printer_id_reaches_the_edge_in_its_canonical_form(
    client: TestClient, edge: Edge, printer: str
) -> None:
    edge.answer = lambda request: httpx.Response(201, content=ANSWER)
    response = client.post(
        f"/api/v1/camera/live/{printer.upper()}/whep",
        content=OFFER,
        headers={"Content-Type": "application/sdp"},
    )
    assert response.status_code == 201
    assert str(edge.requests[0].url) == f"{WHEP_BASE}/{printer}/whep"


def test_an_offer_of_another_type_is_refused(client: TestClient, edge: Edge, printer: str) -> None:
    response = client.post(
        f"/api/v1/camera/live/{printer}/whep",
        content=OFFER,
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 415
    assert edge.requests == []


def test_an_offer_over_64_kib_is_refused(client: TestClient, edge: Edge, printer: str) -> None:
    response = client.post(
        f"/api/v1/camera/live/{printer}/whep",
        content=b"a" * (64 * 1024 + 1),
        headers={"Content-Type": "application/sdp"},
    )
    assert response.status_code == 413
    assert edge.requests == []


def test_a_foreign_location_is_not_passed(client: TestClient, edge: Edge, printer: str) -> None:
    other = str(uuid.uuid4())
    edge.answer = lambda request: httpx.Response(
        201, content=ANSWER, headers={"Location": f"/{other}/whep/{SESSION}"}
    )
    response = client.post(
        f"/api/v1/camera/live/{printer}/whep",
        content=OFFER,
        headers={"Content-Type": "application/sdp"},
    )
    assert response.status_code == 201
    assert "location" not in response.headers


def test_patch_and_delete_reach_the_session(client: TestClient, edge: Edge, printer: str) -> None:
    edge.answer = lambda request: httpx.Response(204 if request.method == "PATCH" else 200)
    frag = b"a=ice-ufrag:abcd\r\na=ice-pwd:abcdefghijklmnopqrstuvwx\r\n"
    url = f"/api/v1/camera/live/{printer}/whep/{SESSION}"
    patched = client.patch(
        url,
        content=frag,
        headers={"Content-Type": "application/trickle-ice-sdpfrag", "If-Match": "*"},
    )
    assert patched.status_code == 204
    assert client.delete(url).status_code == 200
    patch, delete = edge.requests
    assert (patch.method, str(patch.url)) == ("PATCH", f"{WHEP_BASE}/{printer}/whep/{SESSION}")
    assert patch.content == frag
    assert patch.headers["content-type"] == "application/trickle-ice-sdpfrag"
    assert patch.headers["if-match"] == "*"
    assert (delete.method, str(delete.url)) == ("DELETE", f"{WHEP_BASE}/{printer}/whep/{SESSION}")
    assert all(_basic(request) == EXPECTED_AUTH for request in edge.requests)


def test_a_patch_of_another_type_is_refused(client: TestClient, edge: Edge, printer: str) -> None:
    response = client.patch(
        f"/api/v1/camera/live/{printer}/whep/{SESSION}",
        content=b"x",
        headers={"Content-Type": "application/sdp"},
    )
    assert response.status_code == 415
    assert edge.requests == []


@pytest.mark.parametrize("session", ["a" * 65, "a.b", "a_b"])
def test_a_session_of_another_shape_never_reaches_the_edge(
    client: TestClient, edge: Edge, printer: str, session: str
) -> None:
    assert client.delete(f"/api/v1/camera/live/{printer}/whep/{session}").status_code == 404
    assert edge.requests == []


# LL-HLS.


def test_a_segment_streams_from_the_edge(client: TestClient, edge: Edge, printer: str) -> None:
    body = b"\x00\x00\x00\x18ftypmp42" * 100
    edge.answer = lambda request: httpx.Response(
        200,
        content=body,
        headers={"Content-Type": "video/mp4", "Set-Cookie": "hlsSession=x; HttpOnly"},
    )
    name = "6ecd292928db_video1_seg7.mp4"
    response = client.get(f"/api/v1/camera/live/{printer}/hls/{name}?session={SESSION}")
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in response.headers
    [sent] = edge.requests
    assert str(sent.url) == f"{HLS_BASE}/{printer}/{name}?session={SESSION}"
    assert _basic(sent) == EXPECTED_AUTH


def test_only_the_listed_query_keys_reach_the_edge(
    client: TestClient, edge: Edge, printer: str
) -> None:
    response = client.get(
        f"/api/v1/camera/live/{printer}/hls/video1_stream.m3u8",
        params={
            "_HLS_msn": "12",
            "_HLS_part": "3",
            "_HLS_skip": "YES",
            "session": SESSION,
            "cookieCheck": "1",
            "foo": "bar",
            "source": "rtsp://x",
        },
    )
    assert response.status_code == 200
    [sent] = edge.requests
    assert dict(sent.url.params) == {
        "_HLS_msn": "12",
        "_HLS_part": "3",
        "_HLS_skip": "YES",
        "session": SESSION,
        "cookieCheck": "1",
    }


def test_a_query_value_of_another_shape_is_dropped(
    client: TestClient, edge: Edge, printer: str
) -> None:
    response = client.get(
        f"/api/v1/camera/live/{printer}/hls/index.m3u8",
        params={"session": "../x", "_HLS_msn": "1" * 65},
    )
    assert response.status_code == 200
    assert dict(edge.requests[0].url.params) == {}


def test_no_cookie_or_authorization_of_the_browser_reaches_the_edge(
    client: TestClient, edge: Edge, printer: str
) -> None:
    response = client.get(
        f"/api/v1/camera/live/{printer}/hls/index.m3u8",
        headers={"Cookie": "scadbuddy_session=abc; hlsSession=x", "Authorization": "Bearer t"},
    )
    assert response.status_code == 200
    [sent] = edge.requests
    assert "cookie" not in sent.headers
    assert _basic(sent) == EXPECTED_AUTH


def test_the_cookie_check_redirect_stays_in_the_proxy(
    client: TestClient, edge: Edge, printer: str
) -> None:
    # MediaMTX v1.21.4 answers a bare index.m3u8 with this (measured 2026-11-25).
    edge.answer = lambda request: httpx.Response(
        302,
        headers={
            "Location": f"/{printer}/index.m3u8?cookieCheck=1",
            "Set-Cookie": "cookieCheck=1; HttpOnly; Secure; SameSite=None; Partitioned",
        },
    )
    response = client.get(f"/api/v1/camera/live/{printer}/hls/index.m3u8", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == (
        f"/api/v1/camera/live/{printer}/hls/index.m3u8?cookieCheck=1"
    )
    assert "set-cookie" not in response.headers


def test_a_relative_redirect_stays_relative(client: TestClient, edge: Edge, printer: str) -> None:
    edge.answer = lambda request: httpx.Response(
        302, headers={"Location": "index.m3u8?cookieCheck=1"}
    )
    response = client.get(f"/api/v1/camera/live/{printer}/hls/index.m3u8", follow_redirects=False)
    assert response.headers["location"] == "index.m3u8?cookieCheck=1"


@pytest.mark.parametrize(
    "location", ["http://evil.example/x.m3u8", "//evil.example/x.m3u8", "/other/index.m3u8", None]
)
def test_a_redirect_elsewhere_is_a_502(
    client: TestClient, edge: Edge, printer: str, location: str | None
) -> None:
    headers = {} if location is None else {"Location": location}
    edge.answer = lambda request: httpx.Response(302, headers=headers)
    response = client.get(f"/api/v1/camera/live/{printer}/hls/index.m3u8", follow_redirects=False)
    assert response.status_code == 502
    assert response.json()["type"].endswith("/camera-edge-refused")
    assert "location" not in response.headers


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "%2e%2e",
        "%2e%2e%2fx.m3u8",
        "..%2fx.m3u8",
        ".m3u8",
        "x.ts",
        "index.m3u8.exe",
        "a.b.m3u8",
        "x" * 129 + ".mp4",
    ],
)
def test_a_name_of_another_shape_never_reaches_the_edge(
    client: TestClient, edge: Edge, printer: str, name: str
) -> None:
    response = client.get(f"/api/v1/camera/live/{printer}/hls/{name}")
    assert response.status_code == 404
    assert edge.requests == []


def test_a_second_printer_in_the_path_never_reaches_the_edge(
    client: TestClient, edge: Edge, printer: str
) -> None:
    other = _make_printer_with_serial(client, "01P00A000000002")
    for path in (
        f"/api/v1/camera/live/{printer}/hls/{other}/index.m3u8",
        f"/api/v1/camera/live/{printer}/hls/..%2f{other}%2findex.m3u8",
        f"/api/v1/camera/live/{printer}/hls/%2e%2e/{other}/index.m3u8",
    ):
        assert client.get(path).status_code == 404, path
    assert edge.requests == []


def _make_printer_with_serial(client: TestClient, serial: str) -> str:
    from datetime import UTC, datetime

    from scadbuddy.printers.models import LanLink, PrinterCreate
    from tests.api.test_camera_routes import REPORT

    printers = _state(client).components.get(PRINTERS)
    row = printers.store.create(PrinterCreate(serial=serial, name="H2C 2", model="H2C"))
    printers.store.link_lan(
        row.id, LanLink(host="10.0.0.10", access_code="12345678"), cert_sha256="cd" * 32
    )
    printers.reports.put(row.id, REPORT, datetime.now(UTC))
    return row.id


def test_a_blocking_playlist_has_at_least_15_s(
    client: TestClient, edge: Edge, printer: str
) -> None:
    """LL-HLS holds a blocking reload open until the part exists; a short read timeout
    would stall the player (review focus 5). MockTransport enforces no timeout, so the
    request's own timeout is what is checked."""
    seen: list[dict[str, float | None]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, content=b"#EXTM3U\n")

    edge.answer = answer
    url = f"/api/v1/camera/live/{printer}/hls/video1_stream.m3u8?_HLS_msn=9&_HLS_part=1"
    assert client.get(url).status_code == 200
    assert seen[0]["read"] is not None and seen[0]["read"] >= 15


def test_the_edge_refusing_the_proxy_is_a_502(client: TestClient, edge: Edge, printer: str) -> None:
    edge.answer = lambda request: httpx.Response(
        401,
        json={"status": "error", "error": "authentication error"},
        headers={"WWW-Authenticate": 'Basic realm="mediamtx"'},
    )
    response = client.get(f"/api/v1/camera/live/{printer}/hls/index.m3u8")
    assert response.status_code == 502
    assert response.json()["type"].endswith("/camera-edge-refused")
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize(
    "body", [b'["session not found"]', b'"session not found"', b"null", b"<x>"]
)
def test_an_edge_refusal_of_another_body_is_a_502(
    client: TestClient, edge: Edge, printer: str, body: bytes
) -> None:
    edge.answer = lambda request: httpx.Response(
        401, content=body, headers={"Content-Type": "application/json"}
    )
    response = client.get(f"/api/v1/camera/live/{printer}/hls/video1_stream.m3u8?session=x")
    assert response.status_code == 502
    assert response.json()["type"].endswith("/camera-edge-refused")


def test_an_ended_hls_session_is_a_404(client: TestClient, edge: Edge, printer: str) -> None:
    # MediaMTX v1.21.4's answer to a session it no longer holds (measured 2026-11-25).
    edge.answer = lambda request: httpx.Response(
        401, json={"status": "error", "error": "session not found"}
    )
    response = client.get(f"/api/v1/camera/live/{printer}/hls/video1_stream.m3u8?session=x")
    assert response.status_code == 404
    assert response.json()["type"].endswith("/camera-live-session-ended")


def test_an_edge_error_is_passed_without_its_headers(
    client: TestClient, edge: Edge, printer: str
) -> None:
    edge.answer = lambda request: httpx.Response(
        404, json={"status": "error", "error": "no stream is available"}
    )
    response = client.get(f"/api/v1/camera/live/{printer}/hls/index.m3u8")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


# Refusals before any proxy call.


def _down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize("method", ["hls", "whep", "patch", "delete"])
def test_the_edge_down_is_a_502(client: TestClient, edge: Edge, printer: str, method: str) -> None:
    edge.answer = _down
    response = _call(client, printer, method)
    assert response.status_code == 502
    assert response.json()["type"].endswith("/camera-edge-unreachable")


def _call(client: TestClient, printer: str, method: str) -> httpx.Response:
    base = f"/api/v1/camera/live/{printer}"
    response: httpx.Response
    if method == "hls":
        response = client.get(f"{base}/hls/index.m3u8")
    elif method == "whep":
        response = client.post(
            f"{base}/whep", content=OFFER, headers={"Content-Type": "application/sdp"}
        )
    elif method == "patch":
        response = client.patch(
            f"{base}/whep/{SESSION}",
            content=b"a=x\r\n",
            headers={"Content-Type": "application/trickle-ice-sdpfrag"},
        )
    else:
        response = client.delete(f"{base}/whep/{SESSION}")
    return response


@pytest.mark.parametrize(
    "unset",
    [
        {"camera_edge_whep_url": None},
        {"camera_edge_hls_url": None},
        {"camera_edge_read_user": None},
        {"camera_edge_read_pass": None},
    ],
)
@pytest.mark.parametrize("method", ["hls", "whep", "patch", "delete"])
def test_an_unset_edge_is_a_503(
    client: TestClient,
    edge: Edge,
    printer: str,
    method: str,
    unset: dict[str, object],
) -> None:
    _configure_edge(client, **unset)
    response = _call(client, printer, method)
    hls_only = "camera_edge_hls_url" in unset
    whep_only = "camera_edge_whep_url" in unset
    if (hls_only and method != "hls") or (whep_only and method == "hls"):
        assert response.status_code != 503
        return
    assert response.status_code == 503
    assert response.json()["type"].endswith("/camera-edge-unset")
    assert edge.requests == []


@pytest.mark.parametrize("method", ["hls", "whep", "patch", "delete"])
def test_a_printer_not_recorded_is_a_404(client: TestClient, edge: Edge, method: str) -> None:
    printer = _make_printer(client, recorded=False)
    assert _call(client, printer, method).status_code == 404
    assert _call(client, str(uuid.uuid4()), method).status_code == 404
    assert _call(client, "not-a-uuid", method).status_code == 404
    assert edge.requests == []


def test_the_read_password_never_reaches_a_response_or_the_log(
    client: TestClient, edge: Edge, printer: str, caplog: pytest.LogCaptureFixture
) -> None:
    edge.answer = _down
    response = client.get(f"/api/v1/camera/live/{printer}/hls/index.m3u8")
    assert EDGE_PASS not in response.text
    assert EDGE_PASS not in caplog.text
    assert EDGE_PASS not in repr(_state(client).settings)
    settings = client.get("/api/v1/settings").text
    assert EDGE_PASS not in settings


def test_live_needs_both_edge_urls_and_the_read_user(client: TestClient, printer: str) -> None:
    _configure_edge(client)
    assert client.get(f"/api/v1/printers/{printer}/camera").json()["live"] is True
    for unset in ("camera_edge_hls_url", "camera_edge_read_pass"):
        _configure_edge(client, **{unset: None})
        assert client.get(f"/api/v1/printers/{printer}/camera").json()["live"] is False


# Signed out.


@pytest.fixture
def enforced(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(os.urandom(32)).decode())
    configured = settings.model_copy(
        update={
            "secret_key_file": key,
            "public_url": PUBLIC,
            "auth_oidc_issuer": ISSUER,
            "auth_oidc_client_id": CLIENT_ID,
            "camera_edge_whep_url": WHEP_BASE,
            "camera_edge_hls_url": HLS_BASE,
            "camera_edge_read_user": EDGE_USER,
            "camera_edge_read_pass": SecretStr(EDGE_PASS),
        }
    )
    issuer = FakeIssuer()
    _configure(configured, issuer)
    with TestClient(_app(configured, "enforce", issuer.transport()), base_url=PUBLIC) as client:
        yield client


@pytest.mark.parametrize("method", ["hls", "whep", "patch", "delete"])
def test_a_signed_out_viewer_is_refused(
    enforced: TestClient, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    fake = Edge()
    monkeypatch.setattr(camera_api, "_edge_transport", httpx.MockTransport(fake.handle))
    response = _call(enforced, str(uuid.uuid4()), method)
    assert response.status_code == 401
    assert response.json()["type"] == SIGNED_OUT
    assert fake.requests == []


# A real edge.


async def test_an_hls_walk_through_the_proxy_to_a_real_edge(
    client: TestClient,
    tmp_path: Path,
    spawn: Spawn,  # noqa: F811
    media: Media,  # noqa: F811
) -> None:
    """index.m3u8 → its cookieCheck redirect → the variant → the init and a segment, each
    through the proxy, against MediaMTX v1.21.4 with `deploy/camera/mediamtx-edge.yml`."""
    printer = _make_printer(client, recorded=True)
    printer_rtsp, publisher = await publishing(tmp_path, spawn, media)
    _, mtx, ports = await start_mediamtx(
        spawn,
        tmp_path,
        media,
        "edge",
        DEPLOY / "mediamtx-edge.yml",
        (
            "MTX_APIADDRESS",
            "MTX_METRICSADDRESS",
            "MTX_HLSADDRESS",
            "MTX_WEBRTCADDRESS",
            "MTX_WEBRTCLOCALUDPADDRESS",
        ),
        {
            "MTX_AUTHINTERNALUSERS_0_USER": EDGE_USER,
            "MTX_AUTHINTERNALUSERS_0_PASS": EDGE_PASS,
        },
    )
    hls, whep = ports["MTX_HLSADDRESS"], ports["MTX_WEBRTCADDRESS"]
    try:
        await mtx.put_path(
            printer,
            {
                "source": f"rtsp://127.0.0.1:{printer_rtsp}/cam",
                "sourceOnDemand": True,
                "rtspTransport": "tcp",
            },
        )
        _configure_edge(
            client,
            camera_edge_whep_url=f"http://127.0.0.1:{whep}",
            camera_edge_hls_url=f"http://127.0.0.1:{hls}",
        )
        base = f"/api/v1/camera/live/{printer}/hls/"
        variant: list[str] = []

        async def index() -> bool:
            assert publisher.poll() is None, "the publisher exited; see publisher.log"
            response = await asyncio.to_thread(client.get, base + "index.m3u8")
            if response.status_code != 200 or not response.text.startswith("#EXTM3U"):
                return False
            assert str(response.url).endswith("index.m3u8?cookieCheck=1")
            variant[:] = [line for line in response.text.splitlines() if line and line[0] != "#"]
            return True

        await until("the edge's playlist through the proxy", index, 30)
        assert re.fullmatch(r"video1_stream\.m3u8\?session=[0-9a-f-]{36}", variant[0])
        playlist = await asyncio.to_thread(client.get, base + variant[0])
        assert playlist.status_code == 200, playlist.text
        init = re.search(r'#EXT-X-MAP:URI="([^"]+)"', playlist.text)
        assert init is not None, playlist.text
        segment = next(
            line for line in playlist.text.splitlines() if re.match(r"\w+_seg\d+\.mp4\?", line)
        )
        for name in (init.group(1), segment):
            got = await asyncio.to_thread(client.get, base + name)
            assert got.status_code == 200, (name, got.text)
            assert got.headers["content-type"] == "video/mp4"
            assert got.content[4:8] in (b"ftyp", b"moof", b"styp")
        # A blocking reload of the next part answers through the proxy.
        msn = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", playlist.text)
        assert msn is not None
        segments = sum(1 for line in playlist.text.splitlines() if line.startswith("#EXTINF"))
        blocking = await asyncio.to_thread(
            client.get,
            base + variant[0] + f"&_HLS_msn={int(msn.group(1)) + segments + 1}&_HLS_part=0",
        )
        assert blocking.status_code == 200, blocking.text
    finally:
        await mtx.aclose()
