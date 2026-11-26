from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from prometheus_client import REGISTRY

from scadbuddy.api.crud_camera import MAX_SEGMENTS
from scadbuddy.auth.service_credentials import ServiceAuth
from scadbuddy.camera.models import SegmentIn
from scadbuddy.camera.process import serve_local
from scadbuddy.camera.recorder import (
    ADOPT_AGE,
    BATCH,
    LEADER,
    STALL_SECONDS,
    STALLED_LEAD_TICKS,
    Recorder,
    serve_hooks,
)
from scadbuddy.camera.settings import CameraRefusal, RecorderSettings
from scadbuddy.camera.uploads import LocalSegment, Uploader, UploadError, object_key
from scadbuddy.crud.client import CrudClient
from tests.camera.support import (
    KEY_ID,
    RELAY_PASS,
    SECRET,
    FakeMtx,
    Token,
    settings,
    until,
    write_segment,
)
from tests.support.store import moto_s3

BASE = "/api/v1/internal/crud/camera"
CRUD = {"X-ScadBuddy-Crud": "1"}
P1 = "p1"
T0 = datetime(2026, 11, 23, 10, 0, 0, tzinfo=UTC)
LEASE = timedelta(seconds=20)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class FakeCamera:
    """The API's camera routes in memory, with `CameraStore`'s lease and index rules."""

    def __init__(self, bucket: FakeBucket, printers: list[str] | None = None) -> None:
        self.bucket = bucket
        self.printers = [P1] if printers is None else printers
        self.now = T0
        self.leases: dict[str, tuple[str, datetime]] = {}
        self.index: list[tuple[str, str, SegmentIn]] = []
        self.index_calls: list[int] = []
        self.feeds: list[tuple[str, dict[str, Any]]] = []
        #: Each line names a call that reached the API, in order.
        self.events: list[str] = []
        #: Keys named by an index call before the bucket held them.
        self.unput: list[str] = []
        self.down = False
        self.index_down = False
        self.feed_down = False
        #: Printers deleted from the API: their lease and feed calls answer 404.
        self.gone: set[str] = set()
        #: The API's clock against this host's, in seconds; ``None`` sends no ``Date``.
        self.skew: float | None = None
        #: How stale the ``Date`` header is: uvicorn refreshes it once a second.
        self.lag = 0.0
        #: Each call's round trip, in seconds of `wall`.
        self.latency = 0.0
        #: This host's wall clock, which the recorders read (`Recorder` ``wall``).
        self.wall = Wall()

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.wall.now += self.latency / 2
        response = self._answer(request)
        if self.skew is not None:
            # As email.utils.formatdate: whole seconds, the fraction dropped.
            when = datetime.fromtimestamp(int(self.wall.now + self.skew - self.lag), UTC)
            response.headers["Date"] = format_datetime(when, usegmt=True)
        self.wall.now += self.latency / 2
        return response

    def _answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(BASE)
        parts = path.split("/")
        if path == "/printers":
            return httpx.Response(200, headers=CRUD, json=[self.source(p) for p in self.printers])
        if self.down:
            return httpx.Response(503, headers=CRUD, json={"detail": "down"})
        body = json.loads(request.content) if request.content else {}
        printer_id = parts[2]
        if printer_id in self.gone and len(parts) == 4 and parts[3] in ("lease", "feed"):
            problem = {"detail": "There is no such printer.", "reason": "printer_not_found"}
            return httpx.Response(404, headers=CRUD, json=problem)
        if len(parts) == 3:
            if printer_id not in self.printers:
                problem = {"detail": "not recorded", "reason": "not_recorded"}
                return httpx.Response(404, headers=CRUD, json=problem)
            return httpx.Response(200, headers=CRUD, json=self.source(printer_id))
        if parts[3] == "lease":
            return self.lease(printer_id, body["recorder"], body["action"])
        if parts[3] == "segments":
            if self.index_down:
                return httpx.Response(503, headers=CRUD, json={"detail": "down"})
            return self.segments(printer_id, body)
        if parts[3] == "feed":
            if self.feed_down:
                return httpx.Response(503, headers=CRUD, json={"detail": "down"})
            self.feeds.append((printer_id, body))
            self.events.append(f"feed {body['state']}")
            return httpx.Response(204, headers=CRUD)
        return httpx.Response(404, headers=CRUD, json={"detail": "no route"})

    @staticmethod
    def source(printer_id: str) -> dict[str, Any]:
        return {
            "printer_id": printer_id,
            "host": "10.0.0.5",
            "port": 322,
            "path": "/streaming/live/1",
            "cert_sha256": "ab" * 32,
        }

    def indexed_until(self, printer_id: str) -> datetime | None:
        ends = [
            s.started_at + timedelta(milliseconds=s.duration_ms)
            for p, _, s in self.index
            if p == printer_id
        ]
        return max(ends, default=None)

    def lease(self, printer_id: str, recorder: str, action: str) -> httpx.Response:
        self.events.append(f"lease {action} {recorder}")
        current = self.leases.get(printer_id)
        held = False
        if action == "release":
            if current is not None and current[0] == recorder:
                del self.leases[printer_id]
        elif current is None or current[0] == recorder or current[1] < self.now:
            self.leases[printer_id] = (recorder, self.now + LEASE)
            held = True
        current = self.leases.get(printer_id)
        until_ = self.indexed_until(printer_id)
        return httpx.Response(
            200,
            headers=CRUD,
            json={
                "held": held,
                "holder": current[0] if current else None,
                "expires_at": current[1].isoformat() if current else None,
                "indexed_until": until_.isoformat() if until_ else None,
            },
        )

    def segments(self, printer_id: str, body: dict[str, Any]) -> httpx.Response:
        current = self.leases.get(printer_id)
        if current is None or current[0] != body["recorder"] or current[1] <= self.now:
            problem = {"detail": "This recorder does not hold the lease.", "reason": "not_leader"}
            return httpx.Response(409, headers=CRUD, json=problem)
        segments = [SegmentIn.model_validate(s) for s in body["segments"]]
        assert len(segments) <= MAX_SEGMENTS
        self.index_calls.append(len(segments))
        known = {s.object_key for _, _, s in self.index}
        for s in segments:
            if s.object_key not in self.bucket.objects:
                self.unput.append(s.object_key)
            if s.object_key not in known:
                self.index.append((printer_id, body["recorder"], s))
        self.events.append(f"index {len(segments)}")
        return httpx.Response(200, headers=CRUD, json={"indexed": len(segments)})

    def client(self) -> CrudClient:
        return CrudClient(
            "http://api",
            "camera",
            ServiceAuth(Token()),
            transport=httpx.MockTransport(self.answer),
            unavailable_budget=0.0,
        )

    def flags(self) -> list[bool]:
        return [s.discontinuity for _, _, s in self.index]

    def starts(self) -> list[datetime]:
        return [s.started_at for _, _, s in self.index]


class Wall:
    def __init__(self) -> None:
        self.now = 1_800_000_000.25

    def __call__(self) -> float:
        return self.now


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = False
        self.events: list[str] | None = None


class FakeUploader:
    def __init__(self, bucket: FakeBucket, prefix: str = "rec") -> None:
        self.bucket = bucket
        self.prefix = prefix

    async def put(self, seg: LocalSegment) -> str:
        if self.bucket.fail:
            raise UploadError(f"the bucket did not take a segment of {seg.printer_id}: down")
        key = object_key(self.prefix, seg.printer_id, seg.started_at)
        self.bucket.objects[key] = seg.path.read_bytes()
        if self.bucket.events is not None:
            self.bucket.events.append("put")
        return key

    async def aclose(self) -> None:
        pass


class LoggedMtx(FakeMtx):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            self.events.append(f"mtx {request.url.path.split('/')[-2]}")
        return super().handle(request)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Rig:
    """One recorder over shared fakes."""

    def __init__(
        self,
        root: Path,
        api: FakeCamera,
        *,
        name: str = "rec-a",
        disk_max_bytes: int = 1 << 30,
        uploader: Any = None,
        config: RecorderSettings | None = None,
        tick_seconds: float | None = None,
        real_clock: bool = False,
    ) -> None:
        self.root = root / name
        self.root.mkdir(parents=True, exist_ok=True)
        self.api = api
        self.mtx = LoggedMtx(api.events)
        self.clock = Clock()
        self.recorder = Recorder(
            api.client(),
            self.mtx.client(),
            uploader or FakeUploader(api.bucket),
            config or settings(camera_recorder_name=name, camera_disk_max_bytes=disk_max_bytes),
            segments=self.root,
            tick_seconds=tick_seconds,
            clock=time.monotonic if real_clock else self.clock,
            wall=api.wall,
            call_budget=0.0,
            retry_min=0.01,
            retry_max=0.02,
        )

    async def segment(
        self, seconds: float, *, length: float = 4.0, init: bytes = b"A", printer: str = P1
    ) -> Path:
        path = write_segment(self.root, printer, at(seconds), init=init)
        await self.recorder.on_segment(printer, path, length)
        return path

    def files(self, printer: str = P1) -> list[str]:
        return sorted(p.name for p in (self.root / printer).glob("*.mp4"))


def name(seconds: float) -> str:
    return at(seconds).strftime("%Y-%m-%d_%H-%M-%S-%f") + ".mp4"


@pytest.fixture
def api() -> FakeCamera:
    return FakeCamera(FakeBucket())


async def test_leader_uploads_before_index(
    tmp_path: Path, api: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    for second in (0, 4, 8):
        await rig.segment(second)
    # The bucket takes the first segment and fails the second: the third waits behind it.
    puts = 0
    real = FakeUploader.put

    async def flaky(self: FakeUploader, seg: LocalSegment) -> str:
        nonlocal puts
        puts += 1
        if puts == 2:
            raise UploadError("the bucket did not take a segment of p1: down")
        return await real(self, seg)

    monkeypatch.setattr(rig.recorder._uploader, "put", flaky.__get__(rig.recorder._uploader))
    await rig.recorder.tick(P1)
    assert api.starts() == [at(0)]
    assert rig.files() == [name(4), name(8)]
    monkeypatch.setattr(rig.recorder._uploader, "put", real.__get__(rig.recorder._uploader))
    await rig.recorder.tick(P1)
    assert api.starts() == [at(0), at(4), at(8)]
    assert api.unput == []
    assert rig.files() == []
    assert [s.object_key for _, _, s in api.index] == [
        object_key("rec", P1, at(s)) for s in (0, 4, 8)
    ]


async def test_batches_hold_at_most_500(tmp_path: Path, api: FakeCamera) -> None:
    assert BATCH == MAX_SEGMENTS
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    for i in range(BATCH + 1):
        await rig.segment(i * 4)
    await rig.recorder.tick(P1)
    assert api.index_calls == [BATCH, 1]
    assert api.unput == []


async def test_standby_prunes_before_indexed_until(tmp_path: Path, api: FakeCamera) -> None:
    leader, standby = Rig(tmp_path, api, name="rec-a"), Rig(tmp_path, api, name="rec-b")
    for rig in (leader, standby):
        await rig.recorder.reconcile_all()
    await leader.segment(0)
    await leader.segment(4)
    await leader.recorder.tick(P1)
    assert api.indexed_until(P1) == at(8)
    # The standby's own cuts: one ends before 8, one ends at 8, one holds 8, one after.
    await standby.segment(0.5, length=3.5)
    await standby.segment(4, length=4)
    await standby.segment(6, length=4)
    await standby.segment(10, length=4)
    await standby.recorder.tick(P1)
    assert standby.files() == [name(4), name(6), name(10)]
    assert api.starts() == [at(0), at(4)]


async def test_handover_uploads_straddling_segment(tmp_path: Path, api: FakeCamera) -> None:
    leader, standby = Rig(tmp_path, api, name="rec-a"), Rig(tmp_path, api, name="rec-b")
    for rig in (leader, standby):
        await rig.recorder.reconcile_all()
    await leader.segment(0)
    await leader.segment(4)
    await leader.recorder.tick(P1)
    await standby.segment(1)
    await standby.segment(5)  # holds 8, the index's end
    await standby.segment(9)
    await standby.recorder.tick(P1)
    # The leader dies: its lease runs out.
    api.now += LEASE + timedelta(seconds=1)
    await standby.recorder.tick(P1)
    assert api.starts() == [at(0), at(4), at(5), at(9)]
    assert api.flags()[2:] == [True, False]
    assert standby.files() == []


async def test_handover_without_a_straddling_segment(tmp_path: Path, api: FakeCamera) -> None:
    leader, standby = Rig(tmp_path, api, name="rec-a"), Rig(tmp_path, api, name="rec-b")
    for rig in (leader, standby):
        await rig.recorder.reconcile_all()
    await leader.segment(0)
    await leader.recorder.tick(P1)
    await leader.recorder.shutdown()  # releases the lease
    await standby.segment(1, length=3)
    await standby.segment(6)
    await standby.segment(10)
    await standby.recorder.tick(P1)
    assert api.starts() == [at(0), at(6), at(10)]
    assert api.flags()[1:] == [True, False]


async def test_gap_marks_discontinuity(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    await rig.segment(4)
    await rig.segment(9.5)  # 1.5 s after the last end
    await rig.segment(13.5, init=b"B")  # a new init
    await rig.segment(18.4, init=b"B")  # 0.9 s after the last end
    await rig.recorder.tick(P1)
    assert api.flags() == [True, False, True, True, False]


async def test_backlog_drops_oldest_at_cap(tmp_path: Path, api: FakeCamera) -> None:
    size = write_segment(tmp_path / "probe", P1, T0).stat().st_size
    rig = Rig(tmp_path, api, disk_max_bytes=3 * size)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    await rig.recorder.tick(P1)
    api.down = True
    # Down past the lease period: the recorder leads no more and uploads nothing.
    rig.clock.now += LEASE.total_seconds() + 1
    for second in (4, 8, 12, 16, 20):
        await rig.segment(second)
        await rig.recorder.tick(P1)
    assert rig.files() == [name(12), name(16), name(20)]
    api.down = False
    await rig.recorder.tick(P1)
    assert api.starts() == [at(0), at(12), at(16), at(20)]
    assert api.flags() == [True, True, False, False]
    assert object_key("rec", P1, at(4)) not in api.bucket.objects
    assert api.unput == []


async def test_api_down_keeps_segments(tmp_path: Path, api: FakeCamera) -> None:
    leader, standby = Rig(tmp_path, api, name="rec-a"), Rig(tmp_path, api, name="rec-b")
    for rig in (leader, standby):
        await rig.recorder.reconcile_all()
    await leader.segment(0)
    await leader.segment(4)
    await leader.recorder.tick(P1)
    assert api.indexed_until(P1) == at(8)
    api.down = True
    # The standby holds a segment that ends before the index's end, but gets no answer.
    await standby.segment(1)
    for second in (8, 12, 16):
        await leader.segment(second)
        await standby.segment(second - 3)
        await leader.recorder.tick(P1)
        await standby.recorder.tick(P1)
    assert leader.files() == [name(8), name(12), name(16)]
    assert standby.files() == [name(1), name(5), name(9), name(13)]
    assert api.starts() == [at(0), at(4)]
    api.down = False
    await standby.recorder.tick(P1)
    assert standby.files() == [name(5), name(9), name(13)]
    await leader.recorder.tick(P1)
    assert api.starts() == [at(0), at(4), at(8), at(12), at(16)]
    assert api.flags() == [True, False, False, False, False]


async def test_not_leader_falls_back_to_standby(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    await rig.recorder.tick(P1)
    await rig.segment(4)
    await rig.segment(8)
    # Another recorder holds the lease by the time the index call comes.
    real = api.lease

    def stolen(printer_id: str, recorder: str, action: str) -> httpx.Response:
        answer = real(printer_id, recorder, action)
        api.leases[printer_id] = ("rec-b", api.now + LEASE)
        return answer

    api.lease = stolen  # type: ignore[method-assign]
    await rig.recorder.tick(P1)
    assert api.starts() == [at(0)]
    assert len(api.bucket.objects) == 3  # the batch was put before the 409
    api.lease = real  # type: ignore[method-assign]
    await rig.recorder.tick(P1)  # standby now: prunes nothing, uploads nothing
    assert len(api.bucket.objects) == 3
    assert rig.files() == [name(4), name(8)]


async def test_stop_flushes_then_reports(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    api.bucket.events = api.events
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    await rig.recorder.tick(P1)
    await rig.recorder.flushed()
    await rig.segment(4)
    await rig.segment(8)
    api.events.clear()
    api.printers = []
    await rig.recorder.reconcile(P1)
    await rig.recorder.flushed()
    assert api.events == [
        "put",
        "put",
        "index 2",
        "lease release rec-a",
        "mtx delete",
        "feed stopped",
    ]
    assert api.starts() == [at(0), at(4), at(8)]
    assert not (rig.root / P1).exists()
    assert P1 not in api.leases
    # A late hook for a stopped printer deletes its file and indexes nothing.
    late = write_segment(rig.root, P1, at(12))
    await rig.recorder.on_segment(P1, late, 4.0)
    assert not late.exists()


async def test_stalled_after_120s(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.recorder.tick(P1)
    await rig.segment(0)
    await rig.recorder.tick(P1)
    await rig.recorder.flushed()
    rig.clock.now += STALL_SECONDS - 1
    await rig.recorder.tick(P1)
    await rig.recorder.flushed()
    assert [b["state"] for _, b in api.feeds] == ["pulling"]
    rig.clock.now += 1
    await rig.recorder.tick(P1)
    await rig.recorder.flushed()
    await rig.segment(4)
    await rig.recorder.tick(P1)
    await rig.recorder.flushed()
    assert [b["state"] for _, b in api.feeds] == ["pulling", "stalled", "pulling"]
    assert all(b["part"] == "recorder" and b["recorder"] == "rec-a" for _, b in api.feeds)


async def test_standby_reports_no_feed_state(tmp_path: Path, api: FakeCamera) -> None:
    leader, standby = Rig(tmp_path, api, name="rec-a"), Rig(tmp_path, api, name="rec-b")
    for rig in (leader, standby):
        await rig.recorder.reconcile_all()
        await rig.recorder.tick(P1)
    await standby.segment(0)
    standby.clock.now += STALL_SECONDS * 2
    await standby.recorder.tick(P1)
    await standby.recorder.flushed()
    assert api.feeds == []


async def test_segment_hook_calls_the_given_url(tmp_path: Path, api: FakeCamera) -> None:
    """A test runs two recorders on one host: each one's hook names its own port."""
    rig = Rig(tmp_path, api)
    recorder = Recorder(
        api.client(),
        rig.mtx.client(),
        FakeUploader(api.bucket),
        settings(),
        segments=rig.root,
        tick_seconds=None,
        hook_url="http://127.0.0.1:40002/segment",
    )
    assert recorder._conf(P1)["runOnRecordSegmentComplete"] == (
        "curl -fsS -X POST http://127.0.0.1:40002/segment/$MTX_PATH"
        "?duration=$MTX_SEGMENT_DURATION&file=$MTX_SEGMENT_PATH"
    )
    assert rig.recorder._conf(P1)["runOnRecordSegmentComplete"].startswith(
        "curl -fsS -X POST http://127.0.0.1:9091/segment/$MTX_PATH?"
    )


async def test_resync_keeps_unchanged_paths(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.recorder.reconcile_all()
    await rig.recorder.reconcile(P1)
    assert rig.mtx.calls == [("replace", P1), ("add", P1)]
    conf = rig.mtx.paths[P1]
    assert conf["source"] == f"rtsp://reader:{RELAY_PASS}@relay:8554/{P1}"
    assert conf["record"] is True
    assert conf["recordPath"] == f"{rig.root}/%path/%Y-%m-%d_%H-%M-%S-%f"
    assert conf["recordFormat"] == "fmp4"
    assert conf["recordPartDuration"] == "1s"
    assert conf["recordSegmentDuration"] == "4s"
    assert conf["recordDeleteAfter"] == "0s"
    api.printers = []
    await rig.recorder.reconcile_all()
    await rig.recorder.flushed()
    assert P1 not in rig.mtx.paths


async def test_hook_server_calls_on_segment(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    path = write_segment(rig.root, P1, at(0))
    outside = write_segment(tmp_path / "elsewhere", P1, at(4))
    server = await serve_hooks(rig.recorder, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient() as http:
            url = f"http://127.0.0.1:{port}/segment/{P1}"
            ok = await http.post(f"{url}?duration=4.8&file={path}")
            bad = await http.post(f"{url}?duration=4&file={outside}")
            odd = await http.post(f"{url}?duration=soon&file={path}")
    finally:
        server.close()
    assert (ok.status_code, bad.status_code, odd.status_code) == (204, 404, 404)
    assert outside.exists()
    await rig.recorder.tick(P1)
    assert api.starts() == [at(0)]
    assert api.index[0][2].duration_ms == 4800


async def test_bucket_keys_never_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    api = FakeCamera(FakeBucket())
    errors: list[BaseException] = []
    with moto_s3() as s3:
        # The bucket does not exist: every put fails with the bucket's own error.
        config = settings(camera_s3_endpoint=s3.endpoint, camera_s3_bucket="missing")
        uploader = Uploader(config)
        rig = Rig(tmp_path, api, uploader=uploader, config=config)
        try:
            await rig.recorder.reconcile_all()
            await rig.segment(0)
            await rig.recorder.tick(P1)
            # A client error whose text carries a presigned query and the keys.
            leaky = await uploader.client()

            async def put_object(**_: Any) -> None:
                raise RuntimeError(f"?X-Amz-Credential={KEY_ID}&X-Amz-Signature=x {SECRET}")

            monkeypatch.setattr(leaky, "put_object", put_object)
            await rig.recorder.tick(P1)
            try:
                await uploader.put(rig.recorder._feeds[P1].pending[0].seg)
            except UploadError as error:
                errors.append(error)
                logging.getLogger("test").exception("put failed", exc_info=error)
            # MediaMTX refuses the path and echoes its body, which holds the relay password.
            rig.mtx.fail = True
            api.printers = [P1, "p2"]
            try:
                await rig.recorder.reconcile_all()
            except Exception as error:
                errors.append(error)
                logging.getLogger("test").exception("reconcile failed", exc_info=error)
        finally:
            await rig.recorder.shutdown()
            await uploader.aclose()
    assert len(errors) == 2
    assert api.index == []
    text = caplog.text + "".join(repr(e) + str(e) for e in errors)
    text += repr(rig.recorder) + repr(uploader) + repr(config)
    assert "p1" in caplog.text
    for secret in (KEY_ID, SECRET, RELAY_PASS):
        assert secret not in text


async def test_shutdown_releases_led_printers(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    await rig.recorder.tick(P1)
    await rig.segment(4)
    await rig.recorder.shutdown()
    assert api.starts() == [at(0), at(4)]
    assert P1 not in api.leases
    assert P1 in rig.mtx.paths  # a shutdown is not a stop: the path stays, nothing reported
    await rig.recorder.flushed()
    assert [b["state"] for _, b in api.feeds] == ["pulling"]


async def test_renew_failure_past_the_lease_drops_leadership(
    tmp_path: Path, api: FakeCamera
) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.recorder.tick(P1)
    assert LEADER.labels(P1, "rec-a")._value.get() == 1
    api.down = True
    rig.clock.now += LEASE.total_seconds() - 1
    await rig.recorder.renew(P1)
    assert LEADER.labels(P1, "rec-a")._value.get() == 1
    rig.clock.now += 1
    await rig.recorder.renew(P1)
    assert LEADER.labels(P1, "rec-a")._value.get() == 0
    # The bucket is up, but a recorder that does not lead uploads nothing.
    api.down = False
    api.leases[P1] = ("rec-b", api.now + LEASE)
    await rig.segment(0)
    await rig.recorder.upload(P1)
    assert api.bucket.objects == {}


async def test_renewal_cadence_holds_while_a_put_is_slow(
    tmp_path: Path, api: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    every = 0.05
    rig = Rig(tmp_path, api, tick_seconds=every, real_clock=True)
    slow = asyncio.Event()

    async def put(seg: LocalSegment) -> str:
        await slow.wait()
        return object_key("rec", seg.printer_id, seg.started_at)

    monkeypatch.setattr(rig.recorder._uploader, "put", put)
    try:
        await rig.recorder.reconcile_all()
        await until(lambda: P1 in api.leases)
        await rig.segment(0)
        renewals = api.events.count
        before = renewals("lease renew rec-a")
        started = time.monotonic()
        await asyncio.sleep(every * 10)
        elapsed = time.monotonic() - started
        made = renewals("lease renew rec-a") - before
        # The put is still hanging; the renewals keep their rate.
        assert not slow.is_set()
        assert made >= int(elapsed / every) - 2
    finally:
        slow.set()
        await rig.recorder.shutdown()
        await rig.recorder.aclose()


async def test_stop_with_a_large_backlog_renews_while_it_flushes(
    tmp_path: Path, api: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, api, tick_seconds=0.01, real_clock=True)
    fake = cast("FakeUploader", rig.recorder._uploader)

    async def put(seg: LocalSegment) -> str:
        if api.bucket.fail:
            raise UploadError("the bucket did not take a segment of p1: down")
        # Each put takes 3 s of the API's time: 30 puts outlast the lease 4 times over.
        api.now += timedelta(seconds=3)
        await asyncio.sleep(0.005)
        return await FakeUploader.put(fake, seg)

    monkeypatch.setattr(rig.recorder._uploader, "put", put)
    try:
        await rig.recorder.reconcile_all()
        await until(lambda: P1 in api.leases)
        api.bucket.fail = True
        for i in range(30):
            await rig.segment(i * 4)
        api.bucket.fail = False
        api.printers = []
        async with asyncio.timeout(10):
            await rig.recorder.reconcile(P1)
            await rig.recorder.flushed()
    finally:
        await rig.recorder.aclose()
    assert len(api.index) == 30
    assert api.unput == []
    assert api.events[-3:] == ["lease release rec-a", "mtx delete", "feed stopped"]


async def test_restart_adopts_leftovers_and_releases(tmp_path: Path, api: FakeCamera) -> None:
    first = Rig(tmp_path, api, name="rec-a")
    await first.recorder.reconcile_all()
    await first.segment(0)
    await first.recorder.tick(P1)
    assert api.indexed_until(P1) == at(4)
    # More segments, not uploaded yet, then the container dies: its files stay.
    old = time.time() - 60
    for offset in (4, 8, 12):
        path = write_segment(first.root, P1, at(offset))
        os.utime(path, (old + offset, old + offset))
    writing = write_segment(first.root, P1, at(16))  # still open in MediaMTX
    puts = list(first.mtx.calls)

    second = Rig(tmp_path, api, name="rec-a")
    second.mtx = first.mtx
    second.recorder._mtx = first.mtx.client()
    await second.recorder.release_all()
    assert P1 not in api.leases
    await second.recorder.reconcile_all()
    # The listed path shows the same fields, but its credentials cannot be seen: put again.
    assert first.mtx.paths[P1]["recordPath"] == second.recorder._conf(P1)["recordPath"]
    assert first.mtx.calls == [*puts, ("replace", P1)]
    held = sorted(item.seg.path.name for item in second.recorder._feeds[P1].pending)
    assert held == [name(4), name(8), name(12)]
    # No acquire while the hold-off lasts: the standby gets the lease first.
    await second.recorder.tick(P1)
    assert P1 not in api.leases
    assert second.files() == [name(4), name(8), name(12), name(16)]
    # The hook for the open file adopts it.
    await second.recorder.on_segment(P1, writing, 4.0)
    held = sorted(item.seg.path.name for item in second.recorder._feeds[P1].pending)
    assert held == [name(4), name(8), name(12), name(16)]
    # After the hold-off it leads again and uploads from the index's end.
    second.clock.now += 11
    await second.recorder.tick(P1)
    assert api.starts() == [at(0), at(4), at(8), at(12), at(16)]
    assert api.unput == []


async def test_adopt_waits_for_a_file_in_writing(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    path = write_segment(rig.root, P1, at(0))
    await rig.recorder.adopt(P1)
    assert rig.recorder._feeds[P1].pending == []
    written = time.time() - ADOPT_AGE - 1
    os.utime(path, (written, written))
    await rig.recorder.adopt(P1)
    assert [item.seg.path for item in rig.recorder._feeds[P1].pending] == [path]


async def test_shutdown_flushes_every_printer(tmp_path: Path) -> None:
    api = FakeCamera(FakeBucket(), printers=[P1, "p2"])
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    for printer in (P1, "p2"):
        await rig.recorder.tick(printer)
        await rig.segment(0, printer=printer)
    await rig.recorder.shutdown()
    assert sorted(p for p, _, _ in api.index) == [P1, "p2"]
    assert api.leases == {}


async def test_hook_answers_204_when_its_action_fails(caplog: pytest.LogCaptureFixture) -> None:
    async def fail() -> None:
        raise RuntimeError("secret detail")

    server = await serve_local(lambda method, target: fail, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient() as http:
            answer = await http.post(f"http://127.0.0.1:{port}/anything")
    finally:
        server.close()
    assert answer.status_code == 204
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text


async def test_adoption_races_hooks_flushes_and_reconciles(tmp_path: Path, api: FakeCamera) -> None:
    size = write_segment(tmp_path / "probe", P1, T0).stat().st_size
    rig = Rig(tmp_path, api, disk_max_bytes=150 * size)
    await rig.recorder.reconcile_all()
    await rig.recorder.tick(P1)
    old = time.time() - 60
    paths = []
    for i in range(200):
        path = write_segment(rig.root, P1, at(i * 4))
        os.utime(path, (old, old))
        paths.append(path)
    async with asyncio.timeout(20):
        await asyncio.gather(
            rig.recorder.adopt(P1),
            rig.recorder.adopt(P1),
            *(rig.recorder.on_segment(P1, path, 4.0) for path in paths[::3]),
            rig.recorder.upload(P1),
            rig.recorder.reconcile_all(),
        )
    pending = rig.recorder._feeds[P1].pending
    starts = [item.seg.started_at for item in pending]
    assert starts == sorted(starts)
    assert len({item.seg.path for item in pending}) == len(pending)
    assert sum(item.seg.bytes for item in pending) <= 150 * size
    async with asyncio.timeout(20):
        await rig.recorder.tick(P1)
    indexed = api.starts()
    assert indexed == sorted(indexed) and len(set(indexed)) == len(indexed)
    assert api.unput == []


async def test_stale_renewal_answer_is_no_handover(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    await rig.recorder.tick(P1)
    await rig.segment(4)
    await rig.segment(8)
    real = rig.recorder._lease

    async def lease(printer_id: str, action: Any) -> Any:
        # Sent before the index commits, answered after it: the end it names is stale.
        answer = await real(printer_id, action)
        await rig.recorder.upload(P1)
        return answer

    rig.recorder._lease = lease  # type: ignore[method-assign]
    await rig.recorder.renew(P1)
    rig.recorder._lease = real  # type: ignore[method-assign]
    feed = rig.recorder._feeds[P1]
    assert feed.own_end == at(12)
    await rig.segment(12)
    await rig.recorder.tick(P1)
    assert api.starts() == [at(0), at(4), at(8), at(12)]
    assert api.flags() == [True, False, False, False]


async def test_last_segment_age(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    rig.clock.now += 30
    await rig.recorder.tick(P1)
    age = {"printer": P1}
    assert REGISTRY.get_sample_value("scadbuddy_camera_last_segment_age_seconds", age) == 30
    api.printers = []
    await rig.recorder.reconcile_all()
    await rig.recorder.flushed()
    assert REGISTRY.get_sample_value("scadbuddy_camera_last_segment_age_seconds", age) is None
    await rig.recorder.aclose()


async def test_clock_offset_from_lease_answers(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    assert rig.recorder.clock_ok()
    # The API's clock runs 5 s behind this host's.
    api.skew = -5.0
    await rig.recorder.tick(P1)
    await rig.recorder.flushed()
    assert not rig.recorder.clock_ok()
    assert rig.recorder.clock_offset is not None and 4.0 <= rig.recorder.clock_offset <= 6.0
    error = api.feeds[-1][1].get("error")
    assert error is not None and "clock" in error
    api.skew = 0.0
    await rig.recorder.tick(P1)
    await rig.recorder.flushed()
    assert rig.recorder.clock_ok()
    assert "error" not in api.feeds[-1][1]
    await rig.recorder.aclose()


async def test_start_refuses_a_clock_offset(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.check_clock()  # no Date header: nothing to compare
    api.skew = 3.5
    with pytest.raises(CameraRefusal, match="clock"):
        await rig.recorder.check_clock()
    api.skew = -0.4
    await rig.recorder.check_clock()
    assert rig.recorder.clock_ok()
    await rig.recorder.aclose()


async def test_wanted_names_the_recorded_paths(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    assert rig.recorder.wanted() == set()
    await rig.recorder.reconcile_all()
    assert rig.recorder.wanted() == {P1}
    await rig.recorder.aclose()


async def test_a_stale_date_on_a_good_clock_is_not_refused(tmp_path: Path, api: FakeCamera) -> None:
    """uvicorn's Date header can be almost 2 s old: a perfect clock still starts."""
    rig = Rig(tmp_path, api)
    api.skew, api.lag, api.latency = 0.0, 1.9, 0.2
    for fraction in (0.0, 0.05, 0.5, 0.95):
        api.wall.now = 1_800_000_000 + fraction
        await rig.recorder.check_clock()
        assert rig.recorder.clock_ok(), (fraction, rig.recorder.clock_offset)
    await rig.recorder.aclose()


async def test_a_3s_skew_is_refused_at_start(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    api.skew, api.latency = -3.0, 0.2
    with pytest.raises(CameraRefusal, match="clock"):
        await rig.recorder.check_clock()
    await rig.recorder.aclose()


async def test_one_outlier_sample_does_not_trip_health(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    api.skew = 0.0
    for _ in range(3):
        await rig.recorder.tick(P1)
    api.skew = -5.0
    await rig.recorder.tick(P1)
    assert rig.recorder.clock_ok()
    api.skew = 0.0
    await rig.recorder.tick(P1)
    await rig.recorder.flushed()
    assert all("error" not in body for _, body in api.feeds)
    await rig.recorder.aclose()


async def test_clock_error_does_not_repost_a_stopped_printer(
    tmp_path: Path, api: FakeCamera
) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    await rig.recorder.tick(P1)
    api.printers = []
    await rig.recorder.reconcile_all()
    await rig.recorder.flushed()
    stopped = len(api.feeds)
    assert api.feeds[-1][1]["state"] == "stopped"
    rig.recorder._feed.set_error("the recorder's clock is +5.0 s off the API's")
    await rig.recorder.flushed()
    assert len(api.feeds) == stopped
    await rig.recorder.aclose()


async def test_a_404_on_the_lease_ends_leadership_at_once(
    tmp_path: Path, api: FakeCamera, caplog: pytest.LogCaptureFixture
) -> None:
    """The printer was deleted: the recorder leads it no more, and uploads nothing."""
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.recorder.tick(P1)
    assert LEADER.labels(P1, "rec-a")._value.get() == 1
    api.gone.add(P1)
    await rig.segment(0)
    await rig.recorder.tick(P1)
    assert LEADER.labels(P1, "rec-a")._value.get() == 0
    assert api.bucket.objects == {}
    assert rig.files() == [name(0)]
    await rig.recorder.aclose()


async def test_a_former_leader_drops_its_feed_state(tmp_path: Path, api: FakeCamera) -> None:
    """A recorder reports only as leader: once the lease moves, it stops posting the state it
    wanted as leader, so the new leader's report is the only writer."""
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    api.feed_down = True
    await rig.segment(0)
    await rig.recorder.tick(P1)
    await asyncio.sleep(0.05)  # the sender retries "pulling" in the background
    assert rig.recorder._feed.desired(P1) == "pulling"
    api.leases[P1] = ("rec-b", api.now + LEASE)
    rig.clock.now += LEASE.total_seconds()
    await rig.recorder.renew(P1)
    assert LEADER.labels(P1, "rec-a")._value.get() == 0
    assert rig.recorder._feed.desired(P1) is None
    api.feed_down = False
    await asyncio.wait_for(rig.recorder.flushed(), 1)
    await asyncio.sleep(0.05)
    assert api.feeds == []
    await rig.recorder.aclose()


async def test_ready_once_every_path_delivered_a_segment(tmp_path: Path, api: FakeCamera) -> None:
    """``/readyz``: a new recorder pod is ready only when it holds video of every printer,
    so a rolling update stops the old pod only then."""
    api.printers = [P1, "p2"]
    rig = Rig(tmp_path, api)
    assert not rig.recorder.ready()  # no full reconcile yet
    await rig.recorder.reconcile_all()
    assert not rig.recorder.ready()
    await rig.segment(0)
    assert not rig.recorder.ready()
    await rig.segment(0, printer="p2")
    assert rig.recorder.ready()
    await rig.recorder.aclose()


async def test_ready_after_the_stall_time_without_a_segment(
    tmp_path: Path, api: FakeCamera
) -> None:
    """A printer that is off sends no video to any recorder: past `STALL_SECONDS` it does
    not hold back the rollout."""
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    rig.clock.now += STALL_SECONDS - 1
    assert not rig.recorder.ready()
    rig.clock.now += 1
    assert rig.recorder.ready()
    await rig.recorder.aclose()


async def test_a_stalled_leader_gives_up_its_lease(tmp_path: Path, api: FakeCamera) -> None:
    """#7407: a leader that renews but indexes nothing while MediaMTX says the path is ready
    releases its lease after `STALLED_LEAD_TICKS` renewals, so the standby takes over."""
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    await rig.recorder.tick(P1)
    assert api.starts() == [at(0)]
    rig.mtx.ready[P1] = True
    api.bucket.fail = True
    # The first renewal still sees the index of the tick before.
    for i in range(STALLED_LEAD_TICKS + 1):
        assert api.leases[P1][0] == "rec-a"
        await rig.segment(4 + i * 4)
        rig.clock.now += 5
        await rig.recorder.tick(P1)
    assert P1 not in api.leases
    assert api.events.count("lease release rec-a") == 1
    assert (
        REGISTRY.get_sample_value("scadbuddy_camera_leader", {"printer": P1, "recorder": "rec-a"})
        == 0
    )
    # It does not take the lease straight back: the standby gets it first.
    rig.clock.now += 5
    await rig.recorder.tick(P1)
    assert P1 not in api.leases
    await rig.recorder.aclose()


async def test_a_leader_whose_path_is_not_ready_keeps_its_lease(
    tmp_path: Path, api: FakeCamera
) -> None:
    """A printer that is off sends no video: no segment is no stall."""
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    await rig.recorder.tick(P1)
    rig.mtx.ready[P1] = False
    for _ in range(3 * STALLED_LEAD_TICKS):
        rig.clock.now += 5
        await rig.recorder.tick(P1)
    assert api.leases[P1][0] == "rec-a"
    assert "lease release rec-a" not in api.events
    await rig.recorder.aclose()


async def test_a_leader_that_indexes_keeps_its_lease(tmp_path: Path, api: FakeCamera) -> None:
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    rig.mtx.ready[P1] = True
    for i in range(3 * STALLED_LEAD_TICKS):
        await rig.segment(i * 4)
        rig.clock.now += 5
        await rig.recorder.tick(P1)
    assert api.leases[P1][0] == "rec-a"
    assert "lease release rec-a" not in api.events
    await rig.recorder.aclose()


async def test_a_takeover_seen_after_a_failed_index_is_handed_over(
    tmp_path: Path, api: FakeCamera
) -> None:
    """A's index fails (409) while its acquire is on its way, and B indexed meanwhile: the
    answer names B's end, so A starts after it and indexes nothing that overlaps."""
    a, b = Rig(tmp_path, api, name="rec-a"), Rig(tmp_path, api, name="rec-b")
    for rig in (a, b):
        await rig.recorder.reconcile_all()
    await a.segment(0)
    await a.recorder.tick(P1)
    await a.segment(4)
    await a.segment(8)
    # A's lease runs out; B takes it, indexes past A's segments, and releases.
    api.now += LEASE + timedelta(seconds=1)
    await b.segment(4.5)
    await b.segment(8.5)
    await b.recorder.tick(P1)
    await b.recorder.shutdown()
    assert api.indexed_until(P1) == at(12.5)
    feed = a.recorder._feeds[P1]
    real = a.recorder._lease

    async def lease(printer_id: str, action: Any) -> Any:
        # A's index call 409s while the lease call is on its way.
        first = feed.pending[0]
        first.key = await a.recorder._uploader.put(first.seg)
        assert not await a.recorder._index(P1, feed, [first])
        return await real(printer_id, action)

    a.recorder._lease = lease  # type: ignore[method-assign]
    await a.recorder.renew(P1)
    a.recorder._lease = real  # type: ignore[method-assign]
    await a.recorder.upload(P1)
    assert api.starts() == [at(0), at(4.5), at(8.5)]
    assert a.files() == []


async def test_adopt_measures_a_gap_from_the_segment_before(
    tmp_path: Path, api: FakeCamera
) -> None:
    """A leftover file right after a hooked one is no discontinuity."""
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    old = time.time() - 60
    for offset in (0, 8):
        path = write_segment(rig.root, P1, at(offset))
        os.utime(path, (old + offset, old + offset))
    await rig.segment(4)  # its hook came; the others' were lost
    await rig.recorder.adopt(P1)
    await rig.recorder.tick(P1)
    assert api.starts() == [at(0), at(4), at(8)]
    assert api.flags()[2] is False


async def test_scan_keeps_a_file_on_an_os_error(
    tmp_path: Path, api: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error that is not about the file's bytes (EMFILE, EIO) keeps the file for a later
    pass; only an unreadable file is deleted."""
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    path = write_segment(rig.root, P1, at(0))
    old = time.time() - 60
    os.utime(path, (old, old))

    def busy(_: Path) -> datetime:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr("scadbuddy.camera.recorder.segment_start", busy)
    await rig.recorder.adopt(P1)
    assert path.exists()
    assert rig.recorder._feeds[P1].pending == []
    monkeypatch.undo()
    await rig.recorder.adopt(P1)
    assert [item.seg.path for item in rig.recorder._feeds[P1].pending] == [path]


async def test_a_printer_added_at_run_time_does_not_hold_back_ready(
    tmp_path: Path, api: FakeCamera
) -> None:
    """Readiness gates a rollout only: a printer added after the first full reconcile does
    not take a running recorder out of ready for `STALL_SECONDS`."""
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    assert rig.recorder.ready()
    api.printers = [P1, "p2"]
    await rig.recorder.reconcile("p2")
    assert rig.recorder.ready()
    await rig.recorder.reconcile_all()
    assert rig.recorder.ready()
    await rig.recorder.aclose()


async def test_a_stop_that_flushes_does_not_hold_other_printers(
    tmp_path: Path, api: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.printers = [P1]
    rig = Rig(tmp_path, api)
    fake = cast("FakeUploader", rig.recorder._uploader)
    gate = asyncio.Event()

    async def put(seg: LocalSegment) -> str:
        if seg.printer_id == P1:
            await gate.wait()
        return await FakeUploader.put(fake, seg)

    try:
        await rig.recorder.reconcile_all()
        await rig.recorder.renew(P1)
        await rig.segment(0)
        monkeypatch.setattr(rig.recorder._uploader, "put", put)
        api.printers = ["p2"]
        async with asyncio.timeout(10):
            await rig.recorder.reconcile(P1)
            # P1's flush waits on the bucket; p2 starts meanwhile.
            await rig.recorder.reconcile("p2")
        assert "p2" in rig.mtx.paths
        assert P1 in rig.mtx.paths
        gate.set()
        async with asyncio.timeout(10):
            await rig.recorder.flushed()
        assert api.starts() == [at(0)]
        assert P1 not in rig.mtx.paths
        assert api.events[-3:] == ["lease release rec-a", "mtx delete", "feed stopped"]
    finally:
        gate.set()
        await rig.recorder.aclose()


async def test_a_stop_whose_delete_fails_retries_it(tmp_path: Path, api: FakeCamera) -> None:
    """A failed ``delete_path`` during a stop is tried again by the stop itself: no other
    change event or resync is needed to stop the recording."""
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    await rig.segment(0)
    await rig.recorder.tick(P1)
    real = rig.mtx.handle
    failed: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method != "GET" and request.url.path.split("/")[-2] == "delete" and not failed:
            failed.append(request.url.path)
            return httpx.Response(500)
        return real(request)

    rig.mtx.handle = handle  # type: ignore[method-assign]
    rig.recorder._mtx = rig.mtx.client()
    api.printers = []
    await rig.recorder.reconcile(P1)
    async with asyncio.timeout(10):
        await rig.recorder.flushed()
    assert failed
    assert P1 not in rig.mtx.paths
    assert P1 not in rig.recorder._feeds
    assert api.events[-1] == "feed stopped"
    await rig.recorder.aclose()


async def test_a_printer_added_again_ends_a_stop_that_cannot_delete(
    tmp_path: Path, api: FakeCamera
) -> None:
    """MediaMTX refuses every delete: the stop retries until the printer is added again,
    which takes the path back."""
    rig = Rig(tmp_path, api)
    await rig.recorder.reconcile_all()
    real = rig.mtx.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method != "GET" and request.url.path.split("/")[-2] == "delete":
            return httpx.Response(500)
        return real(request)

    rig.mtx.handle = handle  # type: ignore[method-assign]
    rig.recorder._mtx = rig.mtx.client()
    api.printers = []
    await rig.recorder.reconcile(P1)
    await until(lambda: rig.recorder._feeds[P1].removing)
    api.printers = [P1]
    async with asyncio.timeout(10):
        await rig.recorder.reconcile(P1)
    feed = rig.recorder._feeds[P1]
    assert feed.stopping is None
    assert P1 in rig.mtx.paths
    await rig.recorder.aclose()
