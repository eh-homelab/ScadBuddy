"""The camera recorder (printer camera recording, phase 1): ``python -m scadbuddy.camera.recorder``.

Two recorder pods run, each beside its own MediaMTX. Each MediaMTX reads every recorded
printer's stream from the relay over RTSP, with the relay read user, and cuts fMP4 segments
to the local disk (`SEGMENTS`). MediaMTX calls the local hook server when a segment is
complete (``runOnRecordSegmentComplete``), and `Recorder.on_segment` takes it.

Per printer, the recorder that holds the CRUD-backed lease (`camera_leases`, 20 s, renewed
every `TICK_SECONDS`) is the leader. The leader uploads its segments oldest first, then
indexes them (`POST …/segments`, at most `BATCH` per call): an index call never names an
object that is not in the bucket yet. A 409 ``not_leader`` means the lease is lost: the
recorder stops uploading at once and acts as a standby. The standby uploads nothing and
deletes each local segment that ends before the index's end, which each lease call answers.
So it holds what the leader has not indexed yet. When it takes the lease, it uploads from its
segment that contains the index's end (spec D14), marked ``discontinuity``.

A segment is also ``discontinuity`` when it starts more than `GAP` after the previous one on
this recorder ends, when its init differs, or when the disk cap dropped the segment before
it. The local disk holds at most ``SCADBUDDY_CAMERA_DISK_MAX_BYTES`` of segments; past it the
oldest unuploaded segments go first.

The leader reports the feed state (part ``recorder``) on change only: ``pulling`` while
segments arrive, ``stalled`` after `STALL_SECONDS` with no new segment. A printer that is no
longer recorded is flushed (when this recorder leads it), released, removed from MediaMTX
and reported ``stopped``. On SIGTERM each led printer is flushed and released.

Only the recorder pods hold the bucket's camera keys (`uploads.Uploader`). The relay read
password goes to MediaMTX through its control API only; a MediaMTX failure raises
`MediaMtxError`, which names the path and the status only. The recorder has no database
secret and refuses to start with one (`settings.RecorderSettings.check_environment`).
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import math
import re
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Protocol
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

import httpx
from prometheus_client import Counter, Gauge
from pydantic import TypeAdapter, ValidationError

from scadbuddy.auth.service_auth import ServiceAuth, exchange_source
from scadbuddy.camera.feed import RETRY_MAX, RETRY_MIN, FeedSender
from scadbuddy.camera.follow import ChangeFollower
from scadbuddy.camera.mediamtx import MediaMtx, MediaMtxError, why
from scadbuddy.camera.models import CameraSource, LeaseAnswer, SegmentIn
from scadbuddy.camera.mp4 import init_span, segment_start
from scadbuddy.camera.process import HOOK_HOST, HOOK_PORT, main_with, serve_health, serve_local
from scadbuddy.camera.settings import RecorderSettings
from scadbuddy.camera.uploads import LocalSegment, Uploader, UploadError
from scadbuddy.crud.client import CrudClient, CrudRefusedError, wait_for_areas

logger = logging.getLogger(__name__)

BASE: Final = "/api/v1/internal/crud/camera"
AREAS: Final = frozenset({"printers", "changes"})
#: MediaMTX's ``recordPath`` root: the volume both containers of the pod share.
SEGMENTS: Final = Path("/segments")
TICK_SECONDS: Final = 5.0
STALL_SECONDS: Final = 120.0
GAP: Final = timedelta(seconds=1)
#: The most segments one index call takes (`api/crud_camera.py` ``MAX_SEGMENTS``).
BATCH: Final = 500
#: How long one lease or index call may retry: well inside a tick.
CALL_BUDGET: Final = 3.0
HOOK_URL: Final = f"http://{HOOK_HOST}:{HOOK_PORT}/segment"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_SOURCES = TypeAdapter(list[CameraSource])

SEGMENTS_INDEXED = Counter(
    "scadbuddy_camera_segments_indexed_total", "Segments this recorder indexed.", ["printer"]
)
UPLOAD_LAG = Gauge(
    "scadbuddy_camera_upload_lag_seconds",
    "Seconds from the end of the newest indexed segment to its index.",
    ["printer"],
)
BACKLOG = Gauge(
    "scadbuddy_camera_backlog_bytes", "Bytes of segments held on the local disk.", ["printer"]
)
LEADER = Gauge(
    "scadbuddy_camera_leader",
    "1 while this recorder holds the printer's lease.",
    ["printer", "recorder"],
)


class Putter(Protocol):
    async def put(self, seg: LocalSegment) -> str: ...


@dataclass(eq=False)
class _Pending:
    seg: LocalSegment
    discontinuity: bool
    #: The object key, once the bucket holds it.
    key: str | None = None


@dataclass(eq=False)
class _Feed:
    arrived: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Local segments not indexed yet (leader) or not pruned yet (standby), oldest first.
    pending: list[_Pending] = field(default_factory=list)
    #: The last segment this recorder recorded, for the gap rule.
    last: LocalSegment | None = None
    #: The next segment comes after a dropped one.
    gap: bool = False
    leader: bool = False
    #: The end of the index as this recorder last left it.
    own_end: datetime | None = None
    seen: bool = False
    loop: asyncio.Task[None] | None = None


class SourceError(Exception):
    """The API sent a source list the recorder cannot read."""


class Recorder:
    def __init__(
        self,
        crud: CrudClient,
        mtx: MediaMtx,
        uploader: Putter,
        settings: RecorderSettings,
        *,
        segments: Path = SEGMENTS,
        tick_seconds: float | None = TICK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        call_budget: float = CALL_BUDGET,
        retry_min: float = RETRY_MIN,
        retry_max: float = RETRY_MAX,
    ) -> None:
        self._crud = crud
        self._mtx = mtx
        self._uploader = uploader
        self._name = settings.camera_recorder_name
        self._relay_url = settings.camera_relay_rtsp_url
        self._read_user = settings.camera_relay_read_user
        self._read_pass = settings.camera_relay_read_pass
        self._disk_max = settings.camera_disk_max_bytes
        self._segments = segments
        self._tick_seconds = tick_seconds
        self._clock = clock
        self._call_budget = call_budget
        self._feeds: dict[str, _Feed] = {}
        #: The conf this recorder last put for each path. MediaMTX lists a source with its
        #: password redacted (measured on v1.21.4 for the relay), so it keeps its own copy.
        self._confs: dict[str, dict[str, Any]] = {}
        self._feed = FeedSender(
            crud, BASE, "recorder", recorder=self._name, retry_min=retry_min, retry_max=retry_max
        )
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"Recorder(name={self._name!r}, printers={sorted(self._feeds)!r})"

    # MediaMTX paths

    def _conf(self, printer_id: str) -> dict[str, Any]:
        parts = urlsplit(self._relay_url)
        user = quote(self._read_user, safe="")
        password = quote(self._read_pass.get_secret_value(), safe="")
        source = urlunsplit(
            (
                parts.scheme,
                f"{user}:{password}@{parts.netloc}",
                f"{parts.path.rstrip('/')}/{quote(printer_id, safe='')}",
                "",
                "",
            )
        )
        return {
            "source": source,
            "sourceOnDemand": False,
            "record": True,
            "recordPath": f"{self._segments}/%path/%Y-%m-%d_%H-%M-%S-%f",
            "recordFormat": "fmp4",
            "recordPartDuration": "1s",
            "recordSegmentDuration": "4s",
            # The recorder deletes each segment itself.
            "recordDeleteAfter": "0s",
            # MediaMTX expands the MTX_* variables itself, with no shell; the duration is in
            # seconds, such as "4.8" (measured 2026-11-24 UTC on v1.21.4).
            "runOnRecordSegmentComplete": (
                f"curl -fsS -X POST {HOOK_URL}/$MTX_PATH"
                "?duration=$MTX_SEGMENT_DURATION&file=$MTX_SEGMENT_PATH"
            ),
        }

    async def _existing(self) -> set[str]:
        try:
            return set(await self._mtx.paths())
        except httpx.HTTPError as error:
            raise MediaMtxError(f"MediaMTX did not list its paths: {why(error)}") from None

    async def reconcile_all(self) -> None:
        """Make MediaMTX's paths match the API's list. A path that has not changed is left
        alone, so a resync after a quiet run restarts no recording."""
        async with self._lock:
            response = await self._crud.request("GET", f"{BASE}/printers")
            try:
                wanted = {s.printer_id for s in _SOURCES.validate_python(response.json())}
            except (ValueError, ValidationError):
                raise SourceError(
                    "the API sent a camera source list the recorder cannot read"
                ) from None
            existing = await self._existing()
            for name in sorted((existing | set(self._feeds)) - wanted):
                await self._stop(name, name in existing)
            for name in sorted(wanted):
                await self._start(name, name in existing)

    async def reconcile(self, printer_id: str) -> None:
        """Start one printer's recording, or stop it when the API answers 404."""
        async with self._lock:
            try:
                await self._crud.request("GET", f"{BASE}/printers/{quote(printer_id, safe='')}")
            except CrudRefusedError as refused:
                if refused.status != 404:
                    raise
                existing = await self._existing()
                if printer_id in existing or printer_id in self._feeds:
                    await self._stop(printer_id, printer_id in existing)
                return
            await self._start(printer_id, printer_id in await self._existing())

    async def stop(self, printer_id: str) -> None:
        async with self._lock:
            await self._stop(printer_id, printer_id in await self._existing())

    async def _start(self, printer_id: str, present: bool) -> None:
        if _SAFE_ID.fullmatch(printer_id) is None:
            logger.warning("camera recorder: ignored a printer id it cannot use as a path")
            return
        feed = self._feeds.get(printer_id)
        if feed is None:
            feed = self._feeds[printer_id] = _Feed(arrived=self._clock())
        conf = self._conf(printer_id)
        if not (present and self._confs.get(printer_id) == conf):
            self._confs.pop(printer_id, None)
            try:
                await self._mtx.put_path(printer_id, conf)
            except httpx.HTTPError as error:
                raise MediaMtxError(
                    f"MediaMTX did not take path {printer_id}: {why(error)}"
                ) from None
            self._confs[printer_id] = conf
        if feed.loop is None and self._tick_seconds is not None:
            feed.loop = asyncio.create_task(self._loop(printer_id, self._tick_seconds))

    async def _stop(self, printer_id: str, present: bool) -> None:
        """Flush (when leading), release, delete the path, report ``stopped``."""
        feed = self._feeds.get(printer_id)
        if feed is not None:
            async with feed.lock:
                await self._cancel_loop(feed)
                if feed.leader:
                    await self._flush(printer_id, feed, budget=None)
                    await self._release(printer_id, feed)
        if present:
            try:
                await self._mtx.delete_path(printer_id)
            except httpx.HTTPError as error:
                raise MediaMtxError(
                    f"MediaMTX did not delete path {printer_id}: {why(error)}"
                ) from None
        self._confs.pop(printer_id, None)
        if feed is not None:
            for item in feed.pending:
                item.seg.path.unlink(missing_ok=True)
            del self._feeds[printer_id]
            shutil.rmtree(self._segments / printer_id, ignore_errors=True)
            for gauge, labels in ((BACKLOG, (printer_id,)), (LEADER, (printer_id, self._name))):
                with suppress(KeyError):
                    gauge.remove(*labels)
        if feed is not None or present or self._feed.desired(printer_id) not in (None, "stopped"):
            self._feed.want(printer_id, "stopped")

    @staticmethod
    async def _cancel_loop(feed: _Feed) -> None:
        if feed.loop is not None:
            feed.loop.cancel()
            await asyncio.gather(feed.loop, return_exceptions=True)
            feed.loop = None

    async def _loop(self, printer_id: str, every: float) -> None:
        while True:
            try:
                await self.tick(printer_id)
            except Exception as error:
                logger.warning(
                    "camera recorder: a tick for %s failed (%s)", printer_id, type(error).__name__
                )
            await asyncio.sleep(every)

    # Segments

    def owns(self, printer_id: str, path: Path) -> bool:
        """Whether ``path`` is a segment file of ``printer_id`` under the segments root."""
        return (
            _SAFE_ID.fullmatch(printer_id) is not None
            and path.is_absolute()
            and path.suffix == ".mp4"
            and path.parent == self._segments / printer_id
        )

    async def on_segment(self, printer_id: str, path: Path, duration_s: float) -> None:
        """MediaMTX finished a segment (``runOnRecordSegmentComplete``)."""
        if not self.owns(printer_id, path):
            logger.warning("camera recorder: ignored a segment outside %s's folder", printer_id)
            return
        feed = self._feeds.get(printer_id)
        if feed is None:
            # A late hook for a printer that is no longer recorded.
            path.unlink(missing_ok=True)
            return
        if any(item.seg.path == path for item in feed.pending):
            return
        try:
            init_bytes, init_sha256 = await asyncio.to_thread(init_span, path)
            size = path.stat().st_size
            started_at = segment_start(path)
        except (OSError, ValueError) as error:
            logger.warning(
                "camera recorder: dropped an unreadable segment of %s (%s)",
                printer_id,
                type(error).__name__,
            )
            path.unlink(missing_ok=True)
            feed.gap = True
            return
        if self._feeds.get(printer_id) is not feed:
            path.unlink(missing_ok=True)
            return
        seg = LocalSegment(
            path=path,
            printer_id=printer_id,
            started_at=started_at,
            duration_ms=max(1, round(duration_s * 1000)),
            bytes=size,
            init_bytes=init_bytes,
            init_sha256=init_sha256,
        )
        last = feed.last
        gap = (
            feed.gap
            or last is None
            or seg.started_at - last.ends_at > GAP
            or seg.init_sha256 != last.init_sha256
        )
        feed.gap = False
        feed.last = seg
        bisect.insort(feed.pending, _Pending(seg, gap), key=lambda item: item.seg.started_at)
        feed.arrived = self._clock()
        feed.seen = True
        if feed.leader:
            self._feed.want(printer_id, "pulling")
        self.enforce_disk_cap()

    def _drop(self, printer_id: str, feed: _Feed, item: _Pending) -> None:
        """Delete a segment that will never be indexed: the next one starts a gap."""
        at = feed.pending.index(item)
        del feed.pending[at]
        item.seg.path.unlink(missing_ok=True)
        if at < len(feed.pending):
            feed.pending[at].discontinuity = True
        else:
            feed.gap = True

    def enforce_disk_cap(self) -> None:
        """Drop the oldest unuploaded segments (any, when all are uploaded) while the local
        segments pass the cap."""
        total = sum(item.seg.bytes for feed in self._feeds.values() for item in feed.pending)
        while total > self._disk_max:
            held = [(pid, f, item) for pid, f in self._feeds.items() for item in f.pending]
            if not held:
                break
            candidates = [entry for entry in held if entry[2].key is None] or held
            printer_id, feed, item = min(candidates, key=lambda entry: entry[2].seg.started_at)
            self._drop(printer_id, feed, item)
            total -= item.seg.bytes
            logger.warning(
                "camera recorder: the local disk passed its cap; dropped the segment of %s at %s",
                printer_id,
                item.seg.started_at.isoformat(),
            )
        for printer_id, feed in self._feeds.items():
            BACKLOG.labels(printer_id).set(sum(item.seg.bytes for item in feed.pending))

    # Lease, upload, index

    async def _lease(
        self, printer_id: str, action: Literal["acquire", "renew", "release"]
    ) -> LeaseAnswer:
        response = await self._crud.request(
            "POST",
            f"{BASE}/printers/{quote(printer_id, safe='')}/lease",
            json={"recorder": self._name, "action": action},
            budget=self._call_budget,
        )
        return LeaseAnswer.model_validate(response.json())

    def _lead(self, printer_id: str, feed: _Feed, held: bool) -> None:
        feed.leader = held
        LEADER.labels(printer_id, self._name).set(1 if held else 0)

    async def _release(self, printer_id: str, feed: _Feed) -> None:
        try:
            await self._lease(printer_id, "release")
        except Exception as error:
            # The lease runs out by itself in 20 s.
            logger.warning(
                "camera recorder: the release of %s failed (%s)", printer_id, type(error).__name__
            )
        self._lead(printer_id, feed, False)

    async def tick(self, printer_id: str) -> None:
        """Acquire or renew the lease. Holding it: upload, then index, the pending segments
        oldest first. Not holding it: drop the segments that end before the index's end."""
        feed = self._feeds.get(printer_id)
        if feed is None:
            return
        async with feed.lock:
            if self._feeds.get(printer_id) is not feed:
                return
            try:
                answer = await self._lease(printer_id, "renew" if feed.leader else "acquire")
            except Exception as error:
                # No answer: keep every segment, upload nothing.
                logger.warning(
                    "camera recorder: the lease call for %s failed (%s); keeping %d segments",
                    printer_id,
                    type(error).__name__,
                    len(feed.pending),
                )
                return
            self._lead(printer_id, feed, answer.held)
            if not answer.held:
                self._prune(feed, answer.indexed_until)
                return
            if answer.indexed_until != feed.own_end:
                self._handover(feed, answer.indexed_until)
            self._report(printer_id, feed)
            await self._flush(printer_id, feed, budget=self._tick_seconds or TICK_SECONDS)

    @staticmethod
    def _prune(feed: _Feed, indexed_until: datetime | None) -> None:
        """A standby keeps the segments that end at or after the index's end."""
        if indexed_until is None:
            return
        for item in [i for i in feed.pending if i.seg.ends_at < indexed_until]:
            feed.pending.remove(item)
            item.seg.path.unlink(missing_ok=True)

    @staticmethod
    def _handover(feed: _Feed, indexed_until: datetime | None) -> None:
        """Another recorder indexed after this one (D14): start from the segment that holds
        the index's end, or the oldest one after it, marked ``discontinuity``."""
        if indexed_until is not None:
            for item in [i for i in feed.pending if i.seg.ends_at <= indexed_until]:
                feed.pending.remove(item)
                item.seg.path.unlink(missing_ok=True)
        if feed.pending:
            feed.pending[0].discontinuity = True
        else:
            feed.gap = True
        feed.own_end = indexed_until

    def _report(self, printer_id: str, feed: _Feed) -> None:
        if self._clock() - feed.arrived >= STALL_SECONDS:
            self._feed.want(printer_id, "stalled")
        elif feed.seen:
            self._feed.want(printer_id, "pulling")

    async def _flush(self, printer_id: str, feed: _Feed, *, budget: float | None) -> None:
        """Upload, then index, in batches, while this recorder leads and the budget lasts.
        An upload that fails ends the flush; what was put before it is indexed."""
        started = self._clock()
        while feed.leader and feed.pending:
            batch: list[_Pending] = []
            stop = False
            for item in list(feed.pending[:BATCH]):
                if item not in feed.pending:
                    continue  # dropped by the disk cap meanwhile
                if item.key is None:
                    try:
                        key = await self._uploader.put(item.seg)
                    except FileNotFoundError:
                        if item in feed.pending:
                            self._drop(printer_id, feed, item)
                        continue
                    except UploadError as error:
                        logger.warning("camera recorder: %s", error)
                        stop = True
                        break
                    except Exception as error:
                        logger.warning(
                            "camera recorder: the upload of a segment of %s failed (%s)",
                            printer_id,
                            type(error).__name__,
                        )
                        stop = True
                        break
                    item.key = key
                if item in feed.pending:
                    batch.append(item)
                if budget is not None and self._clock() - started > budget:
                    stop = True
                    break
            if batch and not await self._index(printer_id, feed, batch):
                return
            if stop or not batch:
                return

    async def _index(self, printer_id: str, feed: _Feed, batch: list[_Pending]) -> bool:
        segments = [
            SegmentIn(
                started_at=item.seg.started_at,
                duration_ms=item.seg.duration_ms,
                object_key=item.key or "",
                bytes=item.seg.bytes,
                init_bytes=item.seg.init_bytes,
                init_sha256=item.seg.init_sha256,
                discontinuity=item.discontinuity,
            ).model_dump(mode="json")
            for item in batch
        ]
        try:
            await self._crud.request(
                "POST",
                f"{BASE}/printers/{quote(printer_id, safe='')}/segments",
                json={"recorder": self._name, "segments": segments},
                budget=self._call_budget,
            )
        except CrudRefusedError as refused:
            if refused.status == 409:
                # The lease is lost: stop uploading, act as a standby.
                self._lead(printer_id, feed, False)
                logger.warning("camera recorder: lost the lease of %s; standby now", printer_id)
            else:
                logger.warning(
                    "camera recorder: the API refused the index of %s (%s)",
                    printer_id,
                    refused.status,
                )
            return False
        except Exception as error:
            logger.warning(
                "camera recorder: the index of %s failed (%s)", printer_id, type(error).__name__
            )
            return False
        for item in batch:
            if item in feed.pending:
                feed.pending.remove(item)
            item.seg.path.unlink(missing_ok=True)
            end = item.seg.ends_at
            feed.own_end = end if feed.own_end is None else max(feed.own_end, end)
        SEGMENTS_INDEXED.labels(printer_id).inc(len(batch))
        lag = datetime.now(UTC) - batch[-1].seg.ends_at
        UPLOAD_LAG.labels(printer_id).set(max(0.0, lag.total_seconds()))
        BACKLOG.labels(printer_id).set(sum(item.seg.bytes for item in feed.pending))
        return True

    # Process

    async def flushed(self) -> None:
        """Wait until every desired feed state has landed."""
        await self._feed.flushed()

    async def shutdown(self) -> None:
        """Before exit: flush and release each printer this recorder leads. The paths stay
        and nothing is reported, since the other recorder goes on."""
        for printer_id, feed in list(self._feeds.items()):
            async with feed.lock:
                await self._cancel_loop(feed)
                if feed.leader:
                    await self._flush(printer_id, feed, budget=TICK_SECONDS)
                    await self._release(printer_id, feed)

    async def aclose(self) -> None:
        for feed in self._feeds.values():
            await self._cancel_loop(feed)
        await self._feed.aclose()


async def serve_hooks(recorder: Recorder, host: str, port: int) -> asyncio.Server:
    """``POST /segment/<printer_id>?duration=<s>&file=<path>``, from MediaMTX's
    ``runOnRecordSegmentComplete`` command: 204, or 404 for any other request."""

    async def route(method: str, target: str) -> bool:
        parts = urlsplit(target)
        names = parts.path.split("/")
        if method != "POST" or len(names) != 3 or names[1] != "segment":
            return False
        printer_id = unquote(names[2])
        query = parse_qs(parts.query)
        try:
            duration = float(query["duration"][0])
            path = Path(query["file"][0])
        except (KeyError, IndexError, ValueError):
            return False
        if not math.isfinite(duration) or duration <= 0 or not recorder.owns(printer_id, path):
            return False
        await recorder.on_segment(printer_id, path, duration)
        return True

    return await serve_local(route, host, port)


def clear_leftovers(root: Path) -> None:
    """Delete segment files a previous run left: no hook will ever name them, and the
    paths are put again (each restarts its recording)."""
    for path in root.glob("*/*.mp4"):
        path.unlink(missing_ok=True)


async def run(settings: RecorderSettings, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    auth = ServiceAuth(exchange_source("camera", settings))
    crud = CrudClient(settings.api_internal_url, "camera", auth)
    mtx = MediaMtx(settings.camera_mediamtx_api_url)
    uploader = Uploader(settings)
    recorder = Recorder(crud, mtx, uploader, settings)
    health = await serve_health()
    hooks: asyncio.Server | None = None
    follower: asyncio.Task[None] | None = None
    try:
        if not await wait_for_areas(crud, "camera", AREAS, stop):
            logger.info("stopped before the CRUD service served the camera areas")
            return
        await asyncio.to_thread(clear_leftovers, SEGMENTS)
        hooks = await serve_hooks(recorder, HOOK_HOST, HOOK_PORT)
        try:
            await recorder.reconcile_all()
        except Exception as error:
            # MediaMTX may still be starting: the follower's first ``ready`` reads again.
            logger.warning(
                "camera recorder: first reconcile failed (%s: %s)", type(error).__name__, error
            )
        follow = ChangeFollower(
            crud, f"{BASE}/changes", recorder.reconcile, recorder.reconcile_all, resync_first=True
        )
        follower = asyncio.create_task(follow.run())
        logger.info("camera recorder %s running", settings.camera_recorder_name)
        await stop.wait()
    finally:
        if follower is not None:
            follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)
        # The hooks first: a segment that completes now is the other recorder's to keep.
        if hooks is not None:
            hooks.close()
        await recorder.shutdown()
        await recorder.aclose()
        health.close()
        await uploader.aclose()
        await mtx.aclose()
        await crud.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    return main_with(
        RecorderSettings.check_environment,
        lambda: RecorderSettings(),  # type: ignore[call-arg]  # read from the environment
        lambda settings: settings.log_level,
        run,
    )


if __name__ == "__main__":
    sys.exit(main())
