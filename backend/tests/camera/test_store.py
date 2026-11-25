"""The camera store: sources, the segment index, leases and feed state."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.camera.models import FeedReport, SegmentIn
from scadbuddy.camera.store import CameraStore, NotLeaderError
from scadbuddy.core.secrets import Kek
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

REPORT: dict[str, Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "h2c-report.json").read_text()
)
SERIAL = "01P00A000000001"
CODE = "12345678"
CERT = "ab" * 32
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def printers(pg_pool: PgPool, kek: Kek) -> PrinterStore:
    return PrinterStore(pg_pool, kek)


@pytest.fixture
def camera(pg_pool: PgPool, printers: PrinterStore) -> CameraStore:
    return CameraStore(pg_pool, printers)


def make_printer(
    printers: PrinterStore,
    pool: PgPool,
    *,
    serial: str = SERIAL,
    code: bool = True,
    report: dict[str, Any] | None = REPORT,
) -> str:
    row = printers.create(PrinterCreate(serial=serial, name="H2C", model="H2C"))
    if code:
        printers.link_lan(row.id, LanLink(host="10.0.0.9", access_code=CODE), cert_sha256=CERT)
    if report is not None:
        PrinterReportStore(pool).put(row.id, report, NOW)
    return row.id


def seg(start: datetime, ms: int = 4000, key: str | None = None) -> SegmentIn:
    return SegmentIn(
        started_at=start,
        duration_ms=ms,
        object_key=key or f"camera/{start.isoformat()}.mp4",
        bytes=1000,
        init_bytes=100,
        init_sha256="0" * 64,
    )


def test_sources_find_the_qualifying_printer(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    pid = make_printer(printers, pg_pool)
    [source] = camera.sources()
    assert (source.printer_id, source.host, source.port) == (pid, "10.0.0.9", 322)
    assert (source.path, source.cert_sha256) == ("/streaming/live/1", CERT)
    assert camera.sources(pid) == [source]
    assert camera.sources(str(uuid.uuid4())) == []
    assert camera.sources("nope") == []
    [coded] = camera.sources_with_codes(pid)
    assert coded.access_code.get_secret_value() == CODE
    assert "access_code" not in source.model_dump()


def test_sources_skip_unqualified(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    no_code = make_printer(printers, pg_pool, serial="01P00A000000002", code=False)
    plain = make_printer(
        printers,
        pg_pool,
        serial="01P00A000000003",
        report={"ipcam": {"rtsp_url": "rtsp://172.16.0.187/streaming/live/1"}},
    )
    no_report = make_printer(printers, pg_pool, serial="01P00A000000004", report=None)
    off = make_printer(printers, pg_pool, serial="01P00A000000005")
    with pg_pool.connection() as conn:
        conn.execute("UPDATE printers SET camera_recording = false WHERE id = %s", (off,))
    assert camera.sources() == []
    for pid in (no_code, plain, no_report, off):
        assert camera.sources(pid) == []
        assert camera.sources_with_codes(pid) == []


def test_acquire_is_exclusive(camera: CameraStore, printers: PrinterStore, pg_pool: PgPool) -> None:
    pid = make_printer(printers, pg_pool)
    barrier = threading.Barrier(8)
    answers: list[tuple[str, bool]] = []

    def go(name: str) -> None:
        barrier.wait()
        answers.append((name, camera.lease(pid, name, "acquire", NOW).held))

    threads = [threading.Thread(target=go, args=(f"r{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [n for n, held in answers if held] == [camera.lease(pid, "r0", "renew", NOW).holder]
    assert sum(held for _, held in answers) == 1


def test_acquire_by_the_holder_renews(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    pid = make_printer(printers, pg_pool)
    first = camera.lease(pid, "a", "acquire", NOW)
    assert first.held and first.holder == "a"
    assert first.expires_at == NOW + timedelta(seconds=20)
    later = NOW + timedelta(seconds=5)
    renewed = camera.lease(pid, "a", "renew", later)
    assert renewed.held and renewed.expires_at == later + timedelta(seconds=20)
    other = camera.lease(pid, "b", "acquire", later)
    assert not other.held and other.holder == "a"


def test_lease_expires_after_20s(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    pid = make_printer(printers, pg_pool)
    assert camera.lease(pid, "a", "acquire", NOW).held
    assert not camera.lease(pid, "b", "acquire", NOW + timedelta(seconds=19)).held
    taken = camera.lease(pid, "b", "acquire", NOW + timedelta(seconds=21))
    assert taken.held and taken.holder == "b"
    assert not camera.lease(pid, "a", "renew", NOW + timedelta(seconds=22)).held


def test_release_only_by_holder(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    pid = make_printer(printers, pg_pool)
    camera.lease(pid, "a", "acquire", NOW)
    kept = camera.lease(pid, "b", "release", NOW)
    assert not kept.held and kept.holder == "a"
    assert not camera.lease(pid, "b", "acquire", NOW).held
    gone = camera.lease(pid, "a", "release", NOW)
    assert not gone.held and gone.holder is None
    assert camera.lease(pid, "b", "acquire", NOW).held


def test_index_refuses_non_leader(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    pid = make_printer(printers, pg_pool)
    now = datetime.now(UTC)
    with pytest.raises(NotLeaderError):
        camera.index(pid, "a", [seg(now)], now)
    camera.lease(pid, "a", "acquire", now)
    with pytest.raises(NotLeaderError):
        camera.index(pid, "b", [seg(now)], now)
    camera.lease(pid, "c", "acquire", now - timedelta(minutes=5))
    camera.lease(pid, "a", "release", now)
    camera.lease(pid, "a", "acquire", now - timedelta(minutes=5))
    with pytest.raises(NotLeaderError):
        camera.index(pid, "a", [seg(now)], now)


def test_index_is_idempotent_by_key(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    pid = make_printer(printers, pg_pool)
    now = datetime.now(UTC)
    camera.lease(pid, "a", "acquire", now)
    batch = [seg(NOW), seg(NOW + timedelta(seconds=4))]
    assert camera.index(pid, "a", batch, now) == 2
    assert camera.index(pid, "a", [*batch, seg(NOW + timedelta(seconds=8))], now) == 1
    assert camera.index(pid, "a", [], now) == 0
    with pg_pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM camera_segments").fetchone()
    assert row is not None and row["n"] == 3


def test_indexed_until_is_last_end(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    pid = make_printer(printers, pg_pool)
    now = datetime.now(UTC)
    assert camera.lease(pid, "a", "acquire", now).indexed_until is None
    camera.index(pid, "a", [seg(NOW), seg(NOW + timedelta(seconds=4), 3500)], now)
    answer = camera.lease(pid, "a", "renew", now)
    assert answer.indexed_until == NOW + timedelta(seconds=7.5)
    assert camera.lease(pid, "b", "acquire", now).indexed_until == NOW + timedelta(seconds=7.5)


def test_indexed_until_reads_only_the_newest_rows(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    """Each lease call answers the index's end without reading the printer's whole index:
    the same end as the full scan, from the rows of the last minutes only."""
    from scadbuddy.camera.store import INDEXED_UNTIL

    pid = make_printer(printers, pg_pool)
    rows = [(pid, NOW + timedelta(seconds=4 * i), 4000, f"camera/{i}.mp4") for i in range(3000)]
    last = NOW + timedelta(seconds=4 * 2999)
    # A longer segment just before the newest one ends after it.
    rows.append((pid, last - timedelta(seconds=30), 40_000, "camera/long.mp4"))
    with pg_pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO camera_segments (printer_id, started_at, duration_ms, object_key,"
                " bytes, init_bytes, init_sha256, recorder) VALUES (%s, %s, %s, %s, 1, 1, '', 'a')",
                rows,
            )
        conn.execute("ANALYZE camera_segments")
        full = conn.execute(
            "SELECT max(started_at + duration_ms * interval '1 millisecond') AS t"
            " FROM camera_segments WHERE printer_id = %s",
            (pid,),
        ).fetchone()
        with conn.transaction():
            conn.execute("SET LOCAL enable_seqscan = off")
            [[plan]] = (
                conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) SELECT " + INDEXED_UNTIL, {"pid": pid})
                .fetchall()[0]
                .values()
            )
    assert full is not None and full["t"] == last + timedelta(seconds=10)
    assert camera.lease(pid, "a", "acquire", datetime.now(UTC)).indexed_until == full["t"]

    def read(node: dict[str, Any]) -> int:
        here = 0
        if node.get("Relation Name") == "camera_segments" or "camera_segments" in str(
            node.get("Index Name", "")
        ):
            here = int(node["Actual Rows"]) + int(node.get("Rows Removed by Filter", 0))
        return here + sum(read(child) for child in node.get("Plans", []))

    assert read(plan["Plan"]) < 200


def test_report_feed_says_changed(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    pid = make_printer(printers, pg_pool)
    relay = FeedReport(part="relay", state="pulling")
    assert camera.report_feed(pid, relay)
    assert not camera.report_feed(pid, FeedReport(part="relay", state="pulling", error="x"))
    assert camera.report_feed(pid, FeedReport(part="relay", state="retrying"))
    assert camera.report_feed(pid, FeedReport(part="recorder", state="pulling"))
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT report FROM camera_feeds WHERE printer_id = %s AND part = 'relay'", (pid,)
        ).fetchone()
    assert row is not None and row["report"]["state"] == "retrying"


def test_index_uses_the_callers_clock(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    pid = make_printer(printers, pg_pool)
    t = datetime(2030, 1, 1, tzinfo=UTC)
    assert camera.lease(pid, "a", "acquire", t).held
    assert camera.index(pid, "a", [seg(t)], t + timedelta(seconds=19)) == 1
    with pytest.raises(NotLeaderError):
        camera.index(pid, "a", [seg(t, key="late")], t + timedelta(seconds=21))


def test_sources_use_the_linked_host(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool
) -> None:
    report = {"ipcam": {"rtsp_url": "rtsps://203.0.113.9:8322/other/path"}}
    pid = make_printer(printers, pg_pool, report=report)
    [source] = camera.sources(pid)
    assert (source.host, source.port, source.path) == ("10.0.0.9", 8322, "/other/path")


@pytest.mark.parametrize("port", ["0", "70000"])
def test_sources_skip_a_bad_port(
    camera: CameraStore, printers: PrinterStore, pg_pool: PgPool, port: str
) -> None:
    report = {"ipcam": {"rtsp_url": f"rtsps://10.0.0.9:{port}/streaming/live/1"}}
    pid = make_printer(printers, pg_pool, report=report)
    assert camera.sources(pid) == []
