"""The camera recorder (printer camera recording, phase 1): ``python -m scadbuddy.camera.recorder``.

Two recorder pods run, each beside its own MediaMTX. Each MediaMTX reads every recorded
printer's stream from the relay over RTSP, with the relay read user, and cuts fMP4 segments
to the local disk (`SEGMENTS`). MediaMTX calls the local hook server when a segment is
complete (``runOnRecordSegmentComplete``), and `Recorder.on_segment` takes it.

Per printer, the recorder that holds the CRUD-backed lease (`camera_leases`, 20 s) is the
leader. A task of its own renews the lease every `TICK_SECONDS`, whatever the uploads do;
a recorder whose last renewal is a lease period old leads no more. The leader uploads its
segments oldest first (each put bounded by `PUT_TIMEOUT`), then indexes them (`POST
…/segments`, at most `BATCH` per call, after at most `INDEX_EVERY` seconds of puts): an
index call never names an object that is not in the bucket yet. A 409 ``not_leader`` means
the lease is lost: the recorder stops uploading at once and acts as a standby. The standby
uploads nothing and deletes each local segment that ends before the index's end, which each
lease call answers. So it holds what the leader has not indexed yet. When it takes the lease,
it uploads from its segment that contains the index's end (spec D14), marked ``discontinuity``.

A segment is also ``discontinuity`` when it starts more than `GAP` after the previous one on
this recorder ends, when its init differs, or when the disk cap dropped the segment before
it. The local disk holds at most ``SCADBUDDY_CAMERA_DISK_MAX_BYTES`` of segments; past it the
oldest unuploaded segments go first.

The leader reports the feed state (part ``recorder``) on change only: ``pulling`` while
segments arrive, ``stalled`` after `STALL_SECONDS` with no new segment. A recorder that stops
leading drops the state it has not posted yet (`FeedSender.forget`), so it does not overwrite
the new leader's report. A 404 on a lease call (the printer was deleted) ends the leadership
at once. A leader that indexes no segment in `STALLED_LEAD_TICKS` renewals while its MediaMTX
says the path is ready releases the lease and takes none for `START_HOLDOFF` seconds, so the
standby takes over (#7407). A printer that is no longer recorded is flushed (when this
recorder leads it, for at most `STOP_FLUSH_SECONDS`, in a task of its own so other printers'
changes go on), released, removed from MediaMTX and reported ``stopped``. On SIGTERM the led
printers are flushed together and released, within `SHUTDOWN_SECONDS`.

A container restart keeps the pod's name, and so the recorder's. At start the recorder
releases every lease under its name and acquires none for `START_HOLDOFF` seconds, so the
standby takes over. It adopts the segment files the last run left (`Recorder.adopt`) and
puts every path again, since it cannot see the credentials of a listed one; the open segment
that costs is in the standby's buffer.

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
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
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
from scadbuddy.camera.process import (
    CHECK_BUDGET,
    HEALTH_PORT,
    HOOK_HOST,
    HOOK_PORT,
    healthy,
    main_with,
    serve_health,
    serve_local,
)
from scadbuddy.camera.settings import CameraRefusal, RecorderSettings
from scadbuddy.camera.uploads import LocalSegment, Uploader, UploadError
from scadbuddy.crud.client import CrudClient, CrudRefusedError, wait_for_areas

logger = logging.getLogger(__name__)

BASE: Final = "/api/v1/internal/crud/camera"
AREAS: Final = frozenset({"printers", "changes"})
#: MediaMTX's ``recordPath`` root: the volume both containers of the pod share.
SEGMENTS: Final = Path("/segments")
#: The renewal period, and the pause between upload passes.
TICK_SECONDS: Final = 5.0
#: The API's lease (`camera/store.py` ``LEASE_SECONDS``).
LEASE_SECONDS: Final = 20.0
#: The longest one put may take.
PUT_TIMEOUT: Final = 10.0
#: The most seconds of puts before an index call.
INDEX_EVERY: Final = 8.0
STOP_FLUSH_SECONDS: Final = 60.0
SHUTDOWN_SECONDS: Final = 25.0
RELEASE_SECONDS: Final = 5.0
#: After a start, no acquire for two renewal periods: the standby takes the lease first.
START_HOLDOFF: Final = 2 * TICK_SECONDS
#: A file not written to for this long is complete: past twice the segment length and
#: MediaMTX's default ``readTimeout`` (10 s), after which a stalled source's file is closed.
ADOPT_AGE: Final = 12.0
STALL_SECONDS: Final = 120.0
#: A leader that renews this many times in a row with no segment indexed, while its
#: MediaMTX says the path is ready, gives up the lease (#7407): 30 s, past `ADOPT_AGE`
#: plus an upload pass and `INDEX_EVERY`, the longest a healthy leader goes without an
#: index while video arrives, and past the 20 s lease the standby needs to take over.
STALLED_LEAD_TICKS: Final = 6
GAP: Final = timedelta(seconds=1)
#: The largest offset from the API's clock a recorder accepts (spec §6.9): segment times
#: come from the recorder's clock, print times from the API's.
MAX_CLOCK_OFFSET: Final = 2.0
#: The clock samples the check decides on: the smallest offset of the last three.
CLOCK_SAMPLES: Final = 3
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
LAST_SEGMENT_AGE = Gauge(
    "scadbuddy_camera_last_segment_age_seconds",
    "Seconds since this recorder's last segment of the printer (since the path started, before"
    " the first one), set each tick (every 5 s).",
    ["printer"],
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
    #: No ``acquire`` before this time (`Recorder.release_all`).
    not_before: float
    #: Held by an upload pass: one at a time per printer.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Local segments not indexed yet (leader) or not pruned yet (standby), oldest first.
    pending: list[_Pending] = field(default_factory=list)
    #: The last segment this recorder recorded, for the gap rule.
    last: LocalSegment | None = None
    #: The next segment comes after a dropped one.
    gap: bool = False
    #: The last lease answer said held.
    held: bool = False
    #: When the last held answer's request was sent (the recorder's clock).
    renewed_at: float | None = None
    #: The end of the index as this recorder last left it.
    own_end: datetime | None = None
    #: The end of the index call on its way, if one is.
    indexing: datetime | None = None
    #: The ends this recorder's index calls left since the renewal on its way was sent: a
    #: stale lease answer can name only these, or the end before them.
    own_ends: list[datetime] = field(default_factory=list)
    #: Successful index calls, and the count at the last renewal (`Recorder._check_stall`).
    indexed_ok: int = 0
    stall_mark: int = 0
    #: Renewals in a row, while leading, with no index and the path ready.
    stall_ticks: int = 0
    seen: bool = False
    #: The feed was there at the first full reconcile, so it gates ``/readyz``.
    gates_ready: bool = True
    renewer: asyncio.Task[None] | None = None
    uploader: asyncio.Task[None] | None = None
    #: The stop (`Recorder._wind_down`) on its way.
    stopping: asyncio.Task[None] | None = None


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
        wall: Callable[[], float] = time.time,
        call_budget: float = CALL_BUDGET,
        put_timeout: float = PUT_TIMEOUT,
        retry_min: float = RETRY_MIN,
        retry_max: float = RETRY_MAX,
        hook_url: str = HOOK_URL,
    ) -> None:
        self._crud = crud
        self._mtx = mtx
        self._uploader = uploader
        self._hook_url = hook_url
        self._name = settings.camera_recorder_name
        self._relay_url = settings.camera_relay_rtsp_url
        self._read_user = settings.camera_relay_read_user
        self._read_pass = settings.camera_relay_read_pass
        self._disk_max = settings.camera_disk_max_bytes
        self._segments = segments
        self._tick_seconds = tick_seconds
        self._clock = clock
        self._wall = wall
        #: The last clock samples (this host's clock minus the API's), and the smallest of
        #: them: the offset the clock check decides on.
        self._clock_samples: deque[float] = deque(maxlen=CLOCK_SAMPLES)
        self.clock_offset: float | None = None
        self._call_budget = call_budget
        self._put_timeout = put_timeout
        self._feeds: dict[str, _Feed] = {}
        #: The conf this recorder last put for each path. MediaMTX lists a source with its
        #: password redacted, so it keeps its own copy.
        self._confs: dict[str, dict[str, Any]] = {}
        self._feed = FeedSender(
            crud, BASE, "recorder", recorder=self._name, retry_min=retry_min, retry_max=retry_max
        )
        self._not_before = -math.inf
        self._lock = asyncio.Lock()
        #: A full reconcile has put the API's paths in MediaMTX (`ready`).
        self._synced = False

    def __repr__(self) -> str:
        return f"Recorder(name={self._name!r}, printers={sorted(self._feeds)!r})"

    def wanted(self) -> set[str]:
        """The paths this recorder has put in MediaMTX and not removed: each one must be in
        MediaMTX's runtime list (`process.healthy`)."""
        return set(self._confs)

    def ready(self) -> bool:
        """``/readyz``: a full reconcile has put the API's paths, and each recorded printer
        has delivered a segment since this process started, or has delivered none for
        `STALL_SECONDS` (a printer that is off sends no video to any recorder). A rolling
        update stops the other recorder only when this one is ready, so this one holds
        video of every printer that sends any when it takes the leases. A printer added
        after the first full reconcile does not count: no rollout waits on it."""
        now = self._clock()
        return self._synced and all(
            feed.seen or now - feed.arrived >= STALL_SECONDS
            for feed in self._feeds.values()
            if feed.gates_ready
        )

    # Clock (spec §6.9)

    async def _timed(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One CRUD call, with its clock sample noted (`_note_clock`)."""
        sent = self._wall()
        response = await self._crud.request(method, path, **kwargs)
        self._note_clock(response, sent, self._wall())
        return response

    @staticmethod
    def _sample(response: httpx.Response, sent: float, received: float) -> float | None:
        """This host's clock minus the API's, from one answer's ``Date`` header. uvicorn
        writes that header in whole seconds and refreshes it once a second, so the API's time
        at the answer lies in [Date, Date + 2): its centre, Date + 1, is the estimate, against
        the middle of this host's send and receive. That gives ±1 s plus the call's latency."""
        value = response.headers.get("date")
        if value is None:
            return None
        try:
            date = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        return (sent + received) / 2 - (date.timestamp() + 1.0)

    def _clock_error(self) -> str | None:
        if self.clock_ok():
            return None
        return (
            f"the recorder's clock is {self.clock_offset:+.1f} s off the API's"
            f" (more than {MAX_CLOCK_OFFSET:g} s); check NTP on its node"
        )

    def clock_ok(self) -> bool:
        """No offset is known, or it is at most `MAX_CLOCK_OFFSET`."""
        return self.clock_offset is None or abs(self.clock_offset) <= MAX_CLOCK_OFFSET

    def _note_clock(self, response: httpx.Response, sent: float, received: float) -> None:
        """Keep the last `CLOCK_SAMPLES` samples; the offset is the smallest of them, so one
        slow answer does not trip the check."""
        sample = self._sample(response, sent, received)
        if sample is None:
            return
        was = self.clock_ok()
        self._clock_samples.append(sample)
        self.clock_offset = min(self._clock_samples, key=abs)
        error = self._clock_error()
        if error is not None and was:
            logger.warning("camera recorder: %s", error)
        elif error is None and not was:
            logger.info(
                "camera recorder: the clock is within %g s of the API's again", MAX_CLOCK_OFFSET
            )
        self._feed.set_error(error)

    async def check_clock(self) -> None:
        """At start: take `CLOCK_SAMPLES` samples, then raise `CameraRefusal` when this
        host's clock is more than `MAX_CLOCK_OFFSET` off the API's."""
        for _ in range(CLOCK_SAMPLES):
            await self._timed("GET", f"{BASE}/printers", budget=self._call_budget)
        error = self._clock_error()
        if error is not None:
            raise CameraRefusal([error])

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
                f"curl -fsS -X POST {self._hook_url}/$MTX_PATH"
                "?duration=$MTX_SEGMENT_DURATION&file=$MTX_SEGMENT_PATH"
            ),
        }

    async def _existing(self) -> dict[str, dict[str, Any]]:
        try:
            return await self._mtx.paths()
        except httpx.HTTPError as error:
            raise MediaMtxError(f"MediaMTX did not list its paths: {why(error)}") from None

    async def _sources(self) -> set[str]:
        response = await self._crud.request("GET", f"{BASE}/printers")
        try:
            return {s.printer_id for s in _SOURCES.validate_python(response.json())}
        except (ValueError, ValidationError):
            raise SourceError(
                "the API sent a camera source list the recorder cannot read"
            ) from None

    async def release_all(self) -> None:
        """At start: release every lease held under this recorder's name, so the standby,
        which holds the buffer, takes over; and acquire none for `START_HOLDOFF` seconds,
        so the standby gets the lease first. The name (the pod's) survives a container
        restart, and the API lets a holder take its own lease again."""
        self._not_before = self._clock() + START_HOLDOFF
        for printer_id in sorted(await self._sources()):
            try:
                await self._lease(printer_id, "release")
            except Exception as error:
                logger.warning(
                    "camera recorder: the release of %s at start failed (%s)",
                    printer_id,
                    type(error).__name__,
                )

    async def reconcile_all(self) -> None:
        """Make MediaMTX's paths match the API's list. A path that has not changed is left
        alone, so a resync after a quiet run restarts no recording."""
        async with self._lock:
            wanted = await self._sources()
            existing = await self._existing()
            for name in sorted((set(existing) | set(self._feeds)) - wanted):
                await self._stop(name, name in existing)
            for name in sorted(wanted):
                await self._start(name, name in existing)
            self._synced = True

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
        if feed is not None and feed.stopping is not None:
            # Added again while its stop flushes: the stop ends first.
            await asyncio.gather(feed.stopping, return_exceptions=True)
            present = False
            feed = self._feeds.get(printer_id)
        if feed is None:
            now = self._clock()
            feed = self._feeds[printer_id] = _Feed(
                arrived=now, not_before=self._not_before, gates_ready=not self._synced
            )
            # Segments a previous run of this container left (`adopt`).
            await self.adopt(printer_id)
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
        every = self._tick_seconds
        if every is not None:
            if feed.renewer is None:
                feed.renewer = asyncio.create_task(self._renew_loop(printer_id, every))
            if feed.uploader is None:
                feed.uploader = asyncio.create_task(self._upload_loop(printer_id, every))

    async def _stop(self, printer_id: str, present: bool) -> None:
        """Stop a printer's recording. With a feed, the flush runs in a task of its own
        (`_wind_down`), so a flush of up to `STOP_FLUSH_SECONDS` does not hold `_lock`, and
        other printers' changes go on meanwhile."""
        feed = self._feeds.get(printer_id)
        if feed is None:
            await self._remove(printer_id, None, present)
        elif feed.stopping is None:
            await _cancel(feed.uploader)
            feed.uploader = None
            feed.stopping = asyncio.create_task(self._wind_down(printer_id, feed, present))

    async def _wind_down(self, printer_id: str, feed: _Feed, present: bool) -> None:
        """Flush (when leading, with the renewals going on, for at most
        `STOP_FLUSH_SECONDS`), release, delete the path, report ``stopped``."""
        try:
            async with feed.lock:
                if self._leads(feed):
                    deadline = self._clock() + STOP_FLUSH_SECONDS
                    await self._flush(printer_id, feed, deadline=deadline)
            await _cancel(feed.renewer)
            feed.renewer = None
            if feed.held:
                await self._release(printer_id, feed)
            await self._remove(printer_id, feed, present or printer_id in self._confs)
        except Exception as error:
            # The next resync stops the path again.
            logger.warning(
                "camera recorder: the stop of %s failed (%s)", printer_id, type(error).__name__
            )
            feed.stopping = None
            await _cancel(feed.renewer)
            feed.renewer = None

    async def _remove(self, printer_id: str, feed: _Feed | None, present: bool) -> None:
        """Delete the path and the feed's files, report ``stopped``."""
        if present:
            try:
                await self._mtx.delete_path(printer_id)
            except httpx.HTTPError as error:
                raise MediaMtxError(
                    f"MediaMTX did not delete path {printer_id}: {why(error)}"
                ) from None
        self._confs.pop(printer_id, None)
        if feed is not None and self._feeds.get(printer_id) is feed:
            for item in feed.pending:
                item.seg.path.unlink(missing_ok=True)
            del self._feeds[printer_id]
            shutil.rmtree(self._segments / printer_id, ignore_errors=True)
            for gauge, labels in (
                (BACKLOG, (printer_id,)),
                (LEADER, (printer_id, self._name)),
                (LAST_SEGMENT_AGE, (printer_id,)),
            ):
                with suppress(KeyError):
                    gauge.remove(*labels)
        if feed is not None or present or self._feed.desired(printer_id) not in (None, "stopped"):
            self._feed.want(printer_id, "stopped")

    async def _renew_loop(self, printer_id: str, every: float) -> None:
        """Renew at a fixed rate, whatever the uploads do."""
        loop = asyncio.get_running_loop()
        due = loop.time()
        while True:
            try:
                await self.renew(printer_id)
            except Exception as error:
                logger.warning(
                    "camera recorder: a renewal of %s failed (%s)", printer_id, type(error).__name__
                )
            due = max(due + every, loop.time())
            await asyncio.sleep(due - loop.time())

    async def _upload_loop(self, printer_id: str, every: float) -> None:
        while True:
            try:
                await self.upload(printer_id)
            except Exception as error:
                logger.warning(
                    "camera recorder: an upload pass of %s failed (%s)",
                    printer_id,
                    type(error).__name__,
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
            seg = await asyncio.to_thread(_read, printer_id, path, max(1, round(duration_s * 1000)))
        except (OSError, ValueError) as error:
            logger.warning(
                "camera recorder: dropped an unreadable segment of %s (%s)",
                printer_id,
                type(error).__name__,
            )
            path.unlink(missing_ok=True)
            feed.gap = True
            return
        if self._feeds.get(printer_id) is not feed or any(
            item.seg.path == path for item in feed.pending
        ):
            if self._feeds.get(printer_id) is not feed:
                path.unlink(missing_ok=True)
            return
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
        if self._leads(feed):
            self._feed.want(printer_id, "pulling")
        self.enforce_disk_cap()

    async def adopt(self, printer_id: str) -> None:
        """Take the printer's segment files that no hook named: those a previous run of this
        container left, or whose hook was lost. A file written to in the last `ADOPT_AGE`
        seconds may still be open in MediaMTX: it waits for its hook or a later pass. An
        adopted segment ends at the next file's start or, for the newest, at its last write.
        The files are read in a thread; the pending list changes on the event loop only."""
        feed = self._feeds.get(printer_id)
        if feed is None:
            return
        known = {item.seg.path for item in feed.pending}
        found = await asyncio.to_thread(self._scan, printer_id, known)
        if self._feeds.get(printer_id) is not feed:
            return
        known = {item.seg.path for item in feed.pending}
        adopted = False
        for seg in found:
            if seg.path in known or not seg.path.exists():
                continue  # its hook came, or it was dropped, while the scan ran
            # The gap rule against the held segment just before it, hooked or adopted.
            at = bisect.bisect_left(
                feed.pending, seg.started_at, key=lambda item: item.seg.started_at
            )
            previous = feed.pending[at - 1].seg if at else None
            gap = (
                previous is None
                or seg.started_at - previous.ends_at > GAP
                or seg.init_sha256 != previous.init_sha256
            )
            feed.pending.insert(at, _Pending(seg, gap))
            adopted = True
            if feed.last is None or seg.started_at > feed.last.started_at:
                feed.last = seg
        if adopted:
            self.enforce_disk_cap()

    def _scan(self, printer_id: str, known: set[Path]) -> list[LocalSegment]:
        """Read the complete files of the printer's folder that ``known`` does not hold. A
        complete file whose bytes cannot be read (`ValueError`) is deleted; one an
        `OSError` stops is kept for a later pass. Runs in a thread: it touches files only."""
        files = sorted((self._segments / printer_id).glob("*.mp4"))
        now = time.time()
        found: list[LocalSegment] = []
        for at, path in enumerate(files):
            if path in known:
                continue
            try:
                written = path.stat().st_mtime
                if now - written < ADOPT_AGE:
                    continue
                start = segment_start(path)
                end = datetime.fromtimestamp(written, UTC)
                if at + 1 < len(files):
                    with suppress(ValueError):
                        end = min(end, segment_start(files[at + 1]))
                duration_ms = max(1, round((end - start).total_seconds() * 1000))
                found.append(_read(printer_id, path, duration_ms))
            except ValueError as error:
                logger.warning(
                    "camera recorder: dropped an unreadable leftover segment of %s (%s)",
                    printer_id,
                    type(error).__name__,
                )
                path.unlink(missing_ok=True)
            except OSError as error:
                # Not about the file's bytes (EMFILE, EIO, or gone meanwhile): a later
                # pass tries again.
                logger.warning(
                    "camera recorder: could not read a leftover segment of %s (%s)",
                    printer_id,
                    type(error).__name__,
                )
        return found

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

    # Lease

    async def _lease(
        self, printer_id: str, action: Literal["acquire", "renew", "release"]
    ) -> LeaseAnswer:
        response = await self._timed(
            "POST",
            f"{BASE}/printers/{quote(printer_id, safe='')}/lease",
            json={"recorder": self._name, "action": action},
            budget=self._call_budget,
        )
        return LeaseAnswer.model_validate(response.json())

    def _leads(self, feed: _Feed) -> bool:
        """The last answer said held, and came less than a lease period ago."""
        return (
            feed.held
            and feed.renewed_at is not None
            and self._clock() - feed.renewed_at < LEASE_SECONDS
        )

    def _lead(self, printer_id: str, feed: _Feed, held: bool, sent: float | None = None) -> None:
        if feed.held and not held:
            # Only the leader reports: the new leader's report must not be overwritten.
            self._feed.forget(printer_id)
        feed.held = held
        if held:
            feed.renewed_at = sent
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

    async def renew(self, printer_id: str) -> None:
        """Acquire or renew the lease, and act on the answer. Not holding it: drop the
        segments that end before the index's end. Holding it after another recorder
        indexed: the handover (D14)."""
        feed = self._feeds.get(printer_id)
        if feed is None:
            return
        sent = self._clock()
        LAST_SEGMENT_AGE.labels(printer_id).set(max(0.0, sent - feed.arrived))
        if not feed.held and sent < feed.not_before:
            return
        before = feed.own_end
        feed.own_ends.clear()
        try:
            async with asyncio.timeout(self._tick_seconds or TICK_SECONDS):
                answer = await self._lease(printer_id, "renew" if feed.held else "acquire")
        except Exception as error:
            if isinstance(error, CrudRefusedError) and error.status == 404:
                # The printer was deleted: lead it no more. Its change event stops the path.
                if feed.held:
                    self._lead(printer_id, feed, False)
                    logger.warning(
                        "camera recorder: the API has no printer %s; standby now", printer_id
                    )
                return
            # No answer: keep every segment. Past the lease period, lead no more.
            logger.warning(
                "camera recorder: the lease call for %s failed (%s); keeping %d segments",
                printer_id,
                type(error).__name__,
                len(feed.pending),
            )
            if feed.held and not self._leads(feed):
                self._lead(printer_id, feed, False)
                logger.warning("camera recorder: the lease of %s ran out; standby now", printer_id)
            return
        if self._feeds.get(printer_id) is not feed:
            return
        self._lead(printer_id, feed, answer.held, sent)
        if not answer.held:
            self._prune(feed, answer.indexed_until)
            return
        # A stale answer names an end this recorder's own index calls left (`own_ends`);
        # any other end is another recorder's: the handover (D14).
        mine = {before, feed.own_end, feed.indexing, *feed.own_ends}
        if answer.indexed_until not in mine:
            self._handover(feed, answer.indexed_until)
        self._report(printer_id, feed)
        await self._check_stall(printer_id, feed)

    async def _check_stall(self, printer_id: str, feed: _Feed) -> None:
        """#7407: a leader that has indexed nothing for `STALLED_LEAD_TICKS` renewals while
        its MediaMTX says the path is ready (video comes in) gives up the lease, and takes
        none for `START_HOLDOFF` seconds, so the standby takes over. A path that is not ready
        (the printer is off) is no stall."""
        if feed.indexed_ok != feed.stall_mark:
            feed.stall_mark = feed.indexed_ok
            feed.stall_ticks = 0
            return
        try:
            async with asyncio.timeout(CHECK_BUDGET):
                ready = (await self._mtx.ready()).get(printer_id, False)
        except (httpx.HTTPError, TimeoutError):
            return
        if not ready:
            feed.stall_ticks = 0
            return
        feed.stall_ticks += 1
        if feed.stall_ticks < STALLED_LEAD_TICKS or not self._leads(feed):
            return
        logger.warning(
            "camera recorder: indexed no segment of %s in %d renewals while its path is ready;"
            " releasing the lease",
            printer_id,
            feed.stall_ticks,
        )
        feed.stall_ticks = 0
        feed.not_before = self._clock() + START_HOLDOFF
        await self._release(printer_id, feed)

    async def upload(self, printer_id: str) -> None:
        """One upload pass: adopt unnamed files, then, while leading, upload and index the
        pending segments oldest first."""
        feed = self._feeds.get(printer_id)
        if feed is None:
            return
        await self.adopt(printer_id)
        async with feed.lock:
            if self._feeds.get(printer_id) is feed and self._leads(feed):
                await self._flush(printer_id, feed, deadline=None)

    async def tick(self, printer_id: str) -> None:
        """One renewal, then one upload pass."""
        await self.renew(printer_id)
        await self.upload(printer_id)

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

    # Upload, index

    async def _flush(self, printer_id: str, feed: _Feed, *, deadline: float | None) -> None:
        """Upload, then index, while this recorder leads. Each index call names at most
        `BATCH` segments and comes after at most `INDEX_EVERY` seconds of puts. An upload
        that fails ends the flush; what was put before it is indexed."""
        while self._leads(feed) and feed.pending:
            if deadline is not None and self._clock() >= deadline:
                logger.warning(
                    "camera recorder: the flush of %s ran out of time with %d segments left",
                    printer_id,
                    len(feed.pending),
                )
                return
            batch: list[_Pending] = []
            stop = False
            window = self._clock()
            for item in list(feed.pending[:BATCH]):
                if item not in feed.pending:
                    continue  # dropped by the disk cap or pruned meanwhile
                if not self._leads(feed):
                    stop = True
                    break
                if item.key is None:
                    try:
                        async with asyncio.timeout(self._put_timeout):
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
                now = self._clock()
                if now - window >= INDEX_EVERY or (deadline is not None and now >= deadline):
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
        feed.indexing = max(item.seg.ends_at for item in batch)
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
        finally:
            feed.indexing = None
        for item in batch:
            if item in feed.pending:
                feed.pending.remove(item)
            item.seg.path.unlink(missing_ok=True)
            end = item.seg.ends_at
            feed.own_end = end if feed.own_end is None else max(feed.own_end, end)
        if feed.own_end is not None:
            feed.own_ends.append(feed.own_end)
        feed.indexed_ok += 1
        SEGMENTS_INDEXED.labels(printer_id).inc(len(batch))
        lag = datetime.now(UTC) - batch[-1].seg.ends_at
        UPLOAD_LAG.labels(printer_id).set(max(0.0, lag.total_seconds()))
        BACKLOG.labels(printer_id).set(sum(item.seg.bytes for item in feed.pending))
        return True

    # Process

    async def flushed(self) -> None:
        """Wait until every stop on its way has ended and every desired feed state has
        landed."""
        while stops := [t for f in self._feeds.values() if (t := f.stopping) and not t.done()]:
            await asyncio.wait(stops)
        await self._feed.flushed()

    async def shutdown(self) -> None:
        """Before exit, within `SHUTDOWN_SECONDS` in all: flush each printer this recorder
        leads, all at once and with the renewals going on, then release. The paths stay and
        nothing is reported, since the other recorder goes on."""
        feeds = list(self._feeds.items())

        async def flush(printer_id: str, feed: _Feed) -> None:
            if feed.stopping is not None:
                # A stop on its way flushes and releases by itself.
                await asyncio.gather(feed.stopping, return_exceptions=True)
                return
            await _cancel(feed.uploader)
            feed.uploader = None
            async with feed.lock:
                if self._leads(feed):
                    await self._flush(printer_id, feed, deadline=None)

        try:
            async with asyncio.timeout(SHUTDOWN_SECONDS - RELEASE_SECONDS):
                await asyncio.gather(*(flush(pid, feed) for pid, feed in feeds))
        except TimeoutError:
            logger.warning("camera recorder: the flush at shutdown ran out of time")
        for _, feed in feeds:
            await _cancel(feed.renewer)
            feed.renewer = None
        try:
            async with asyncio.timeout(RELEASE_SECONDS):
                await asyncio.gather(
                    *(self._release(pid, feed) for pid, feed in feeds if feed.held)
                )
        except TimeoutError:
            logger.warning("camera recorder: the releases at shutdown ran out of time")

    async def aclose(self) -> None:
        for feed in list(self._feeds.values()):
            await _cancel(feed.stopping)
            await _cancel(feed.uploader)
            feed.uploader = None
            await _cancel(feed.renewer)
            feed.renewer = None
        await self._feed.aclose()


async def _cancel(task: asyncio.Task[None] | None) -> None:
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _read(printer_id: str, path: Path, duration_ms: int) -> LocalSegment:
    init_bytes, init_sha256 = init_span(path)
    return LocalSegment(
        path=path,
        printer_id=printer_id,
        started_at=segment_start(path),
        duration_ms=duration_ms,
        bytes=path.stat().st_size,
        init_bytes=init_bytes,
        init_sha256=init_sha256,
    )


async def serve_hooks(recorder: Recorder, host: str, port: int) -> asyncio.Server:
    """``POST /segment/<printer_id>?duration=<s>&file=<path>``, from MediaMTX's
    ``runOnRecordSegmentComplete`` command: 204, or 404 for any other request."""

    def route(method: str, target: str) -> Callable[[], Awaitable[None]] | None:
        parts = urlsplit(target)
        names = parts.path.split("/")
        if method != "POST" or len(names) != 3 or names[1] != "segment":
            return None
        printer_id = unquote(names[2])
        query = parse_qs(parts.query)
        try:
            duration = float(query["duration"][0])
            path = Path(query["file"][0])
        except (KeyError, IndexError, ValueError):
            return None
        if not math.isfinite(duration) or duration <= 0 or not recorder.owns(printer_id, path):
            return None
        return lambda: recorder.on_segment(printer_id, path, duration)

    return await serve_local(route, host, port)


async def run(settings: RecorderSettings, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    auth = ServiceAuth(exchange_source("camera", settings))
    crud = CrudClient(settings.api_internal_url, "camera", auth)
    mtx = MediaMtx(settings.camera_mediamtx_api_url)
    uploader = Uploader(settings)
    recorder = Recorder(crud, mtx, uploader, settings)
    hooks: asyncio.Server | None = None
    follower: asyncio.Task[None] | None = None

    async def alive() -> bool:
        return await healthy(mtx, follower, recorder.wanted()) and recorder.clock_ok()

    async def ready() -> bool:
        return follower is not None and recorder.ready() and await alive()

    health = await serve_health(HEALTH_PORT, alive, ready)
    try:
        if not await wait_for_areas(crud, "camera", AREAS, stop):
            logger.info("stopped before the CRUD service served the camera areas")
            return
        try:
            await recorder.check_clock()
        except CameraRefusal:
            raise
        except Exception as error:
            # Each lease answer checks the clock again.
            logger.warning(
                "camera recorder: the clock check at start failed (%s)", type(error).__name__
            )
        hooks = await serve_hooks(recorder, HOOK_HOST, HOOK_PORT)
        try:
            await recorder.release_all()
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
