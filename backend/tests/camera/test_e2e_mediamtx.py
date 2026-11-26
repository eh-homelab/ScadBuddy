"""The camera relay, two recorders and the live edge against real MediaMTX processes (phase 1,
Task 9; phase 3, the edge).

ffmpeg publishes a test stream to a third MediaMTX that stands in for the printer. The relay
pulls it over plain RTSP (a test-only `Relay` ``source_for``), and each recorder's MediaMTX
reads the relay and cuts 4 s fMP4 segments. The edge's MediaMTX reads the relay on demand
and serves LL-HLS. The CRUD routes are a fake over the real
`CameraStore` on the test Postgres; the bucket is moto. Every port is free and every process
has its own, so the test runs anywhere. Every wait is bounded."""

from __future__ import annotations

import asyncio
import itertools
import json
import socket
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from pydantic import SecretStr

from scadbuddy.auth.service_credentials import ServiceAuth
from scadbuddy.camera import edge as edge_module
from scadbuddy.camera import recorder as recorder_module
from scadbuddy.camera import relay as relay_module
from scadbuddy.camera.mediamtx import MediaMtx
from scadbuddy.camera.models import FeedReport, SegmentIn
from scadbuddy.camera.settings import EdgeSettings
from scadbuddy.camera.store import CameraStore, NotLeaderError
from scadbuddy.camera.uploads import Uploader
from scadbuddy.core.secrets import Kek
from scadbuddy.crud.client import CrudClient
from scadbuddy.printers.store import PrinterStore
from tests.camera.conftest import Media, Spawn
from tests.camera.test_follow import Token
from tests.camera.test_store import make_printer
from tests.camera.test_uploads import settings
from tests.conftest import PgPool
from tests.support.store import moto_s3

pytestmark = pytest.mark.requires_postgres

DEPLOY = Path(__file__).resolve().parents[3] / "deploy" / "camera"
CRUD = {"X-ScadBuddy-Crud": "1"}
READ_USER, READ_PASS = "reader", "relay-read-pass"
#: The edge's read user, which the API sends to the edge's HLS and WHEP ports.
EDGE_USER, EDGE_PASS = "edge-reader", "edge-read-pass"
#: Segments to index before the kill, and after it.
BEFORE, AFTER = 10, 10
#: The longest hole allowed between one indexed segment's end and the next one's start.
HOLE = timedelta(seconds=1)


@pytest.fixture
def printers(pg_pool: PgPool, kek: Kek) -> PrinterStore:
    return PrinterStore(pg_pool, kek)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def listening(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return False
    writer.close()
    return True


class StoreCrud:
    """The camera and camera-relay CRUD routes, over the real `CameraStore`."""

    def __init__(self, store: CameraStore) -> None:
        self.store = store

    async def answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        relay = path.startswith(relay_module.BASE + "/")
        parts = path.removeprefix(relay_module.BASE if relay else recorder_module.BASE).split("/")
        body = json.loads(request.content) if request.content else {}
        if parts == ["", "printers"]:
            found = await asyncio.to_thread(self.sources, relay, None)
            return httpx.Response(200, headers=CRUD, json=found)
        printer_id = parts[2]
        if len(parts) == 3:
            found = await asyncio.to_thread(self.sources, relay, printer_id)
            if not found:
                problem = {"detail": "This printer is not recorded.", "reason": "not_recorded"}
                return httpx.Response(404, headers=CRUD, json=problem)
            return httpx.Response(200, headers=CRUD, json=found[0])
        now = datetime.now(UTC)
        if parts[3] == "lease":
            answer = await asyncio.to_thread(
                self.store.lease, printer_id, body["recorder"], body["action"], now
            )
            return httpx.Response(200, headers=CRUD, json=answer.model_dump(mode="json"))
        if parts[3] == "segments":
            segments = [SegmentIn.model_validate(s) for s in body["segments"]]
            try:
                indexed = await asyncio.to_thread(
                    self.store.index, printer_id, body["recorder"], segments, now
                )
            except NotLeaderError:
                problem = {
                    "detail": "This recorder does not hold the lease.",
                    "reason": "not_leader",
                }
                return httpx.Response(409, headers=CRUD, json=problem)
            return httpx.Response(200, headers=CRUD, json={"indexed": indexed})
        if parts[3] == "feed":
            report = FeedReport.model_validate(body | {"part": "relay" if relay else "recorder"})
            await asyncio.to_thread(self.store.report_feed, printer_id, report)
            return httpx.Response(204, headers=CRUD)
        return httpx.Response(404, headers=CRUD, json={"detail": "no route"})

    def sources(self, relay: bool, printer_id: str | None) -> list[dict[str, Any]]:
        if not relay:
            return [s.model_dump(mode="json") for s in self.store.sources(printer_id)]
        return [
            {**s.model_dump(mode="json"), "access_code": s.access_code.get_secret_value()}
            for s in self.store.sources_with_codes(printer_id)
        ]

    def client(self, caller: Literal["camera", "camera-relay"]) -> CrudClient:
        return CrudClient(
            "http://api",
            caller,
            ServiceAuth(Token()),
            transport=httpx.MockTransport(self.answer),
            unavailable_budget=0.0,
        )


async def until(what: str, check: Callable[[], Awaitable[bool]], seconds: float) -> None:
    try:
        async with asyncio.timeout(seconds):
            while not await check():
                await asyncio.sleep(0.5)
    except TimeoutError:
        raise AssertionError(f"waited {seconds} s for {what}") from None


async def start_mediamtx(
    spawn: Spawn, media: Media, name: str, config: Path, env: dict[str, str]
) -> tuple[subprocess.Popen[bytes], MediaMtx]:
    """Start MediaMTX with ``config``, the keys in ``env`` (``MTX_<KEY>``) overriding it,
    and wait for its control API."""
    proc = spawn(name, [str(media.mediamtx), str(config)], env)
    api = f"http://{env['MTX_APIADDRESS']}"
    mtx = MediaMtx(api)

    async def up() -> bool:
        assert proc.poll() is None, f"{name} exited; see {name}.log"
        with suppress(httpx.HTTPError):
            await mtx.paths()
            return True
        return False

    await until(f"{name}'s control API", up, 20)
    return proc, mtx


async def publishing(
    tmp_path: Path, spawn: Spawn, media: Media
) -> tuple[int, subprocess.Popen[bytes]]:
    """The printer: a MediaMTX that takes ffmpeg's test stream on path ``cam``. Its RTSP port
    and the publisher."""
    printer_rtsp = free_port()
    printer_conf = tmp_path / "printer.yml"
    printer_conf.write_text(
        "logLevel: info\napi: no\nmetrics: no\n"
        f"rtspAddress: 127.0.0.1:{printer_rtsp}\nrtspTransports: [tcp]\n"
        "rtmp: no\nhls: no\nwebrtc: no\nsrt: no\nmoq: no\npaths:\n  cam: {}\n"
    )
    spawn("printer", [str(media.mediamtx), str(printer_conf)], {})
    await until("the printer's RTSP port", lambda: listening(printer_rtsp), 20)
    publisher = spawn(
        "publisher",
        [
            str(media.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=25",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "10",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            f"rtsp://127.0.0.1:{printer_rtsp}/cam",
        ],
        {},
    )
    return printer_rtsp, publisher


@dataclass
class RelayRig:
    rtsp: int
    api: str
    relay: relay_module.Relay


@asynccontextmanager
async def relaying(
    spawn: Spawn, media: Media, crud: StoreCrud, printer_rtsp: int
) -> AsyncIterator[RelayRig]:
    """The relay's MediaMTX with the read user, and its controller, which pulls the printer
    over plain RTSP (a test-only ``source_for``)."""
    async with AsyncExitStack() as stack:
        relay_rtsp, relay_hook, relay_api = free_port(), free_port(), free_port()
        _, relay_mtx = await start_mediamtx(
            spawn,
            media,
            "relay",
            DEPLOY / "mediamtx-relay.yml",
            {
                "MTX_APIADDRESS": f"127.0.0.1:{relay_api}",
                "MTX_METRICSADDRESS": f"127.0.0.1:{free_port()}",
                "MTX_RTSPADDRESS": f"127.0.0.1:{relay_rtsp}",
                "MTX_AUTHINTERNALUSERS_0_USER": READ_USER,
                "MTX_AUTHINTERNALUSERS_0_PASS": READ_PASS,
            },
        )
        stack.push_async_callback(relay_mtx.aclose)
        relay_client = crud.client("camera-relay")
        stack.push_async_callback(relay_client.aclose)
        relay = relay_module.Relay(
            relay_client,
            relay_mtx,
            source_for=lambda source: f"rtsp://127.0.0.1:{printer_rtsp}/cam",
            hook_url=f"http://127.0.0.1:{relay_hook}/hook",
        )
        stack.push_async_callback(relay.aclose)
        relay_hooks = await relay_module.serve_hooks(relay, "127.0.0.1", relay_hook)
        stack.callback(relay_hooks.close)
        await relay.reconcile_all()
        yield RelayRig(relay_rtsp, f"http://127.0.0.1:{relay_api}", relay)


@dataclass
class RecorderRig:
    name: str
    proc: subprocess.Popen[bytes]
    recorder: recorder_module.Recorder
    hooks: asyncio.Server


@asynccontextmanager
async def recording(
    tmp_path: Path,
    spawn: Spawn,
    media: Media,
    crud: StoreCrud,
    s3: Any,
    name: str,
    relay_rtsp: int,
) -> AsyncIterator[RecorderRig]:
    async with AsyncExitStack() as stack:
        api, hook = free_port(), free_port()
        proc, mtx = await start_mediamtx(
            spawn,
            media,
            name,
            DEPLOY / "mediamtx-recorder.yml",
            {
                "MTX_APIADDRESS": f"127.0.0.1:{api}",
                "MTX_METRICSADDRESS": f"127.0.0.1:{free_port()}",
            },
        )
        stack.push_async_callback(mtx.aclose)
        config = settings(
            camera_recorder_name=name,
            camera_s3_endpoint=s3.endpoint,
            camera_s3_bucket=s3.bucket,
            camera_s3_prefix=s3.prefix,
            camera_s3_access_key=SecretStr(s3.access_key_id),
            camera_s3_secret_key=SecretStr(s3.secret_access_key),
            camera_relay_rtsp_url=f"rtsp://127.0.0.1:{relay_rtsp}",
            camera_relay_read_user=READ_USER,
            camera_relay_read_pass=SecretStr(READ_PASS),
        )
        uploader = Uploader(config)
        stack.push_async_callback(uploader.aclose)
        client = crud.client("camera")
        stack.push_async_callback(client.aclose)
        segments = tmp_path / name
        segments.mkdir()
        recorder = recorder_module.Recorder(
            client,
            mtx,
            uploader,
            config,
            segments=segments,
            hook_url=f"http://127.0.0.1:{hook}/segment",
        )
        stack.push_async_callback(recorder.aclose)
        hooks = await recorder_module.serve_hooks(recorder, "127.0.0.1", hook)
        stack.callback(hooks.close)
        await recorder.reconcile_all()
        yield RecorderRig(name, proc, recorder, hooks)


async def test_two_recorders_no_gap_on_leader_kill(
    tmp_path: Path, spawn: Spawn, media: Media, pg_pool: PgPool, printers: PrinterStore
) -> None:
    printer_id = make_printer(printers, pg_pool)
    store = CameraStore(pg_pool, printers)
    crud = StoreCrud(store)

    def indexed() -> list[dict[str, Any]]:
        with pg_pool.connection() as conn:
            return conn.execute(
                "SELECT started_at, duration_ms, discontinuity, recorder FROM camera_segments"
                " WHERE printer_id = %s ORDER BY started_at",
                (printer_id,),
            ).fetchall()

    def feed(part: str) -> str | None:
        with pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT report->>'state' AS state FROM camera_feeds"
                " WHERE printer_id = %s AND part = %s",
                (printer_id, part),
            ).fetchone()
        return None if row is None else str(row["state"])

    def holder() -> str | None:
        with pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT holder FROM camera_leases WHERE printer_id = %s", (printer_id,)
            ).fetchone()
        return None if row is None else str(row["holder"])

    async def count_at_least(n: int) -> bool:
        assert publisher.poll() is None, "the publisher exited; see publisher.log"
        return len(await asyncio.to_thread(indexed)) >= n

    printer_rtsp, publisher = await publishing(tmp_path, spawn, media)

    async with AsyncExitStack() as stack:
        s3 = stack.enter_context(moto_s3())
        relay_rtsp = (
            await stack.enter_async_context(relaying(spawn, media, crud, printer_rtsp))
        ).rtsp

        rigs = {
            rig.name: rig
            for rig in [
                await stack.enter_async_context(
                    recording(tmp_path, spawn, media, crud, s3, name, relay_rtsp)
                )
                for name in ("rec-a", "rec-b")
            ]
        }

        await until(f"{BEFORE} indexed segments", lambda: count_at_least(BEFORE), 120)
        # The relay's ready hook (``runOnAvailable``) and the leader both report.
        assert await asyncio.to_thread(feed, "relay") == "pulling"
        assert await asyncio.to_thread(feed, "recorder") == "pulling"
        leader = rigs[str(await asyncio.to_thread(holder))]
        standby = rigs[({"rec-a", "rec-b"} - {leader.name}).pop()]
        before = len(await asyncio.to_thread(indexed))

        # The leader's pod dies: MediaMTX is killed, and the controller stops with no flush
        # and no release.
        leader.proc.kill()
        leader.hooks.close()
        await leader.recorder.aclose()

        await until(f"{AFTER} more indexed segments", lambda: count_at_least(before + AFTER), 180)
        assert await asyncio.to_thread(holder) == standby.name
        await standby.recorder.shutdown()

    rows = await asyncio.to_thread(indexed)
    table = "\n".join(
        f"{r['started_at'].isoformat()} {r['duration_ms']} {r['discontinuity']} {r['recorder']}"
        for r in rows
    )
    holes = [
        (prev["started_at"], row["started_at"])
        for prev, row in itertools.pairwise(rows)
        if row["started_at"] - (prev["started_at"] + timedelta(milliseconds=prev["duration_ms"]))
        > HOLE
    ]
    assert holes == [], table
    after = [row for row in rows if row["recorder"] == standby.name]
    assert [row["recorder"] for row in rows] == [leader.name] * (len(rows) - len(after)) + [
        standby.name
    ] * len(after), table
    # The first segment, and the handover's: no other.
    flags = [row["discontinuity"] for row in rows]
    assert flags == [i in (0, len(rows) - len(after)) for i in range(len(rows))], table
    # Spec §6.8: no MediaMTX process writes the relay read password to its output. The relay
    # gets it from MTX_AUTHINTERNALUSERS_0_PASS, each recorder inside its paths' source URL.
    for name in ("printer", "relay", "rec-a", "rec-b"):
        output = (tmp_path / f"{name}.log").read_text(errors="replace")
        assert output, f"{name} wrote nothing: the check would prove nothing"
        assert READ_PASS not in output, f"{name}.log holds the relay read password"


async def runtime(api: str, path: str) -> dict[str, Any] | None:
    """``path``'s entry in MediaMTX's runtime list, or ``None`` when it is not listed."""
    async with httpx.AsyncClient(timeout=5) as http:
        response = await http.get(f"{api}/v3/paths/list", params={"itemsPerPage": 1000})
    response.raise_for_status()
    return next((item for item in response.json()["items"] if item["name"] == path), None)


async def readers(api: str, path: str) -> int | None:
    """How many readers MediaMTX's runtime list shows on ``path``."""
    item = await runtime(api, path)
    return None if item is None else len(item["readers"])


async def path_ready(api: str, path: str) -> bool:
    item = await runtime(api, path)
    return item is not None and item["ready"] is True


async def test_edge_serves_hls_on_demand_from_the_relay(
    tmp_path: Path, spawn: Spawn, media: Media, pg_pool: PgPool, printers: PrinterStore
) -> None:
    """The edge pulls the relay only while someone watches: a playlist request starts the
    pull, and the pull closes once requests stop (``sourceOnDemand``)."""
    printer_id = make_printer(printers, pg_pool)
    crud = StoreCrud(CameraStore(pg_pool, printers))
    printer_rtsp, publisher = await publishing(tmp_path, spawn, media)

    async with AsyncExitStack() as stack:
        relay = await stack.enter_async_context(relaying(spawn, media, crud, printer_rtsp))
        await until(
            "the relay's pull of the printer", lambda: path_ready(relay.api, printer_id), 30
        )
        hls, whep = free_port(), free_port()
        _, edge_mtx = await start_mediamtx(
            spawn,
            media,
            "edge",
            DEPLOY / "mediamtx-edge.yml",
            {
                "MTX_APIADDRESS": f"127.0.0.1:{free_port()}",
                "MTX_METRICSADDRESS": f"127.0.0.1:{free_port()}",
                "MTX_HLSADDRESS": f"127.0.0.1:{hls}",
                "MTX_WEBRTCADDRESS": f"127.0.0.1:{whep}",
                "MTX_WEBRTCLOCALUDPADDRESS": f"127.0.0.1:{free_port()}",
                "MTX_AUTHINTERNALUSERS_0_USER": EDGE_USER,
                "MTX_AUTHINTERNALUSERS_0_PASS": EDGE_PASS,
            },
        )
        stack.push_async_callback(edge_mtx.aclose)
        edge_client = crud.client("camera")
        stack.push_async_callback(edge_client.aclose)
        edge = edge_module.Edge(
            edge_client,
            edge_mtx,
            EdgeSettings(
                api_internal_url="http://api",
                camera_relay_rtsp_url=f"rtsp://127.0.0.1:{relay.rtsp}",
                camera_relay_read_user=READ_USER,
                camera_relay_read_pass=SecretStr(READ_PASS),
            ),
        )
        await edge.reconcile_all()
        assert edge.wanted() == {printer_id}
        # On demand: no viewer, no pull from the relay.
        assert await readers(relay.api, printer_id) == 0

        # The edge fails closed: no read without the edge read user's password.
        index = f"http://127.0.0.1:{hls}/{printer_id}/index.m3u8?cookieCheck=1"
        offer = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
        sdp = {"Content-Type": "application/sdp"}
        whep_url = f"http://127.0.0.1:{whep}/{printer_id}/whep"
        async with httpx.AsyncClient(timeout=10) as http:
            # ``httpx.Auth()`` sends no credentials.
            for auth in (httpx.Auth(), httpx.BasicAuth(EDGE_USER, "wrong")):
                assert (await http.get(index, auth=auth)).status_code == 401
                answer = await http.post(whep_url, content=offer, headers=sdp, auth=auth)
                assert answer.status_code == 401
        assert await readers(relay.api, printer_id) == 0

        last_request = 0.0
        loop = asyncio.get_running_loop()

        async def playlist() -> bool:
            nonlocal last_request
            assert publisher.poll() is None, "the publisher exited; see publisher.log"
            # MediaMTX v1.21.4 first answers 302 to ``?cookieCheck=1`` with a cookie.
            auth = (EDGE_USER, EDGE_PASS)
            async with httpx.AsyncClient(timeout=10, follow_redirects=True, auth=auth) as http:
                response = await http.get(f"http://127.0.0.1:{hls}/{printer_id}/index.m3u8")
            last_request = loop.time()
            return response.status_code == 200 and response.text.startswith("#EXTM3U")

        await until("the edge's HLS playlist", playlist, 15)
        assert await readers(relay.api, printer_id) == 1

        async def closed() -> bool:
            return await readers(relay.api, printer_id) == 0

        # The muxer closes 10 s after the last request (``hlsMuxerCloseAfter``), the HLS
        # session about 30 s later, then the on-demand pull 10 s after that: 48.5 s measured.
        await until("the relay to lose the edge's pull", closed, 90)
        waited = loop.time() - last_request
        assert waited <= 60, f"the edge held the relay pull {waited:.1f} s after the last request"

        # Last, as it starts the on-demand pull: with the password, WHEP gets past the check
        # and refuses the empty offer.
        async with httpx.AsyncClient(timeout=20) as http:
            answer = await http.post(
                whep_url, content=offer, headers=sdp, auth=(EDGE_USER, EDGE_PASS)
            )
        assert answer.status_code == 400, answer.text

    output = (tmp_path / "edge.log").read_text(errors="replace")
    assert output, "edge wrote nothing: the check would prove nothing"
    assert READ_PASS not in output, "edge.log holds the relay read password"
    assert EDGE_PASS not in output, "edge.log holds the edge read password"
