"""The camera store: which printers record, the segment index, the feed leases and the
feed state (plan 2026-11-23 camera recording, phase 1).

A printer qualifies when its LAN link has a sealed code, its last report names an
``rtsps://`` camera (`models.rtsp_url`) and ``printers.camera_recording`` is on. One
recorder at a time holds a printer's lease. The lease is one statement, so two recorders
that ask at once cannot both get it."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from psycopg import Connection, errors
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import SecretStr

from scadbuddy.camera.models import (
    CameraSource,
    CameraSourceWithCode,
    FeedReport,
    LeaseAnswer,
    SegmentIn,
    rtsp_url,
)
from scadbuddy.printers.store import PrinterNotFoundError, PrinterStore, printer_uuid

LEASE_SECONDS = 20

#: The end of a printer's index. It reads only the rows that start in the 5 minutes before
#: the newest start (found through ``camera_segments_printer_started``), not the printer's
#: whole index, on every lease call. The result is the full scan's: MediaMTX cuts a segment
#: every 4 s at a key frame (``recordSegmentDuration``), so an older segment that ends after
#: the newest one would have to be more than 5 minutes long.
INDEXED_UNTIL = (
    "(SELECT max(started_at + duration_ms * interval '1 millisecond')"
    " FROM camera_segments WHERE printer_id = %(pid)s AND started_at >="
    " (SELECT max(started_at) FROM camera_segments WHERE printer_id = %(pid)s)"
    " - interval '5 minutes')"
)


class NotLeaderError(Exception):
    """The recorder holds no live lease for the printer."""


class CameraStore:
    def __init__(
        self,
        pool: ConnectionPool[Connection[DictRow]],
        printers: PrinterStore | None = None,
    ) -> None:
        self._pool = pool
        self._printers = printers

    def sources(self, printer_id: str | None = None) -> list[CameraSource]:
        return [CameraSource(**s) for s in self._sources(printer_id)]

    def sources_with_codes(self, printer_id: str | None = None) -> list[CameraSourceWithCode]:
        if self._printers is None:
            raise RuntimeError("CameraStore needs a PrinterStore to open access codes")
        return [
            CameraSourceWithCode(
                **s, access_code=SecretStr(self._printers.open_access_code(s["printer_id"]))
            )
            for s in self._sources(printer_id)
        ]

    def _sources(self, printer_id: str | None) -> list[dict[str, Any]]:
        where = ""
        args: tuple[object, ...] = ()
        if printer_id is not None:
            key = printer_uuid(printer_id)
            if key is None:
                return []
            where = " AND p.id = %s"
            args = (key,)
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT p.id, p.lan_host, p.lan_cert_sha256, r.report FROM printers p"
                " JOIN printer_reports r ON r.printer_id = p.id"
                " WHERE p.camera_recording AND p.lan_host IS NOT NULL"
                " AND p.access_code_sealed IS NOT NULL AND p.lan_cert_sha256 IS NOT NULL"
                f"{where} ORDER BY p.created_at, p.id",
                args,
            ).fetchall()
        found: list[dict[str, Any]] = []
        for row in rows:
            url = rtsp_url(row["report"])
            if url is None:
                continue
            parts = urlsplit(url)
            try:
                port = 322 if parts.port is None else parts.port
            except ValueError:
                continue
            if not 1 <= port <= 65535:
                continue
            found.append(
                {
                    "printer_id": str(row["id"]),
                    "host": row["lan_host"],
                    "port": port,
                    "path": parts.path,
                    "cert_sha256": row["lan_cert_sha256"],
                }
            )
        return found

    def index(
        self, printer_id: str, recorder: str, segments: list[SegmentIn], now: datetime
    ) -> int:
        key = printer_uuid(printer_id)
        if key is None:
            raise NotLeaderError(printer_id)
        with self._pool.connection() as conn, conn.transaction():
            held = conn.execute(
                "SELECT 1 FROM camera_leases WHERE printer_id = %s AND holder = %s"
                " AND expires_at > %s FOR SHARE",
                (key, recorder, now),
            ).fetchone()
            if held is None:
                raise NotLeaderError(printer_id)
            inserted = 0
            for s in segments:
                cur = conn.execute(
                    "INSERT INTO camera_segments (printer_id, started_at, duration_ms,"
                    " object_key, bytes, init_bytes, init_sha256, recorder, discontinuity)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (object_key) DO NOTHING",
                    (
                        key,
                        s.started_at,
                        s.duration_ms,
                        s.object_key,
                        s.bytes,
                        s.init_bytes,
                        s.init_sha256,
                        recorder,
                        s.discontinuity,
                    ),
                )
                inserted += cur.rowcount
        return inserted

    def lease(
        self,
        printer_id: str,
        recorder: str,
        action: Literal["acquire", "renew", "release"],
        now: datetime,
    ) -> LeaseAnswer:
        key = printer_uuid(printer_id)
        if key is None:
            return LeaseAnswer(held=False)
        args = {
            "pid": key,
            "rec": recorder,
            "now": now,
            "until": now + timedelta(seconds=LEASE_SECONDS),
        }
        try:
            return self._lease(args, action)
        except errors.ForeignKeyViolation:
            raise PrinterNotFoundError(printer_id) from None

    def _lease(
        self, args: dict[str, Any], action: Literal["acquire", "renew", "release"]
    ) -> LeaseAnswer:
        with self._pool.connection() as conn, conn.transaction():
            if action == "release":
                conn.execute(
                    "DELETE FROM camera_leases WHERE printer_id = %(pid)s AND holder = %(rec)s",
                    args,
                )
                won = False
            else:
                won = (
                    conn.execute(
                        "INSERT INTO camera_leases AS l (printer_id, holder, expires_at)"
                        " VALUES (%(pid)s, %(rec)s, %(until)s)"
                        " ON CONFLICT (printer_id) DO UPDATE SET holder = EXCLUDED.holder,"
                        " expires_at = EXCLUDED.expires_at"
                        " WHERE l.holder = EXCLUDED.holder OR l.expires_at < %(now)s"
                        " RETURNING holder",
                        args,
                    ).fetchone()
                    is not None
                )
            current = conn.execute(
                "SELECT holder, expires_at, " + INDEXED_UNTIL + " AS indexed_until"
                " FROM (SELECT 1) x LEFT JOIN camera_leases ON printer_id = %(pid)s",
                args,
            ).fetchone()
        assert current is not None
        return LeaseAnswer(
            held=won,
            holder=current["holder"],
            expires_at=current["expires_at"],
            indexed_until=current["indexed_until"],
        )

    def report_feed(self, printer_id: str, report: FeedReport) -> bool:
        """Store the part's feed state; true when its state changed. Raises
        `PrinterNotFoundError` when the printer is gone."""
        key = printer_uuid(printer_id)
        if key is None:
            return False
        try:
            return self._report_feed(key, report)
        except errors.ForeignKeyViolation:
            raise PrinterNotFoundError(printer_id) from None

    def _report_feed(self, key: UUID, report: FeedReport) -> bool:
        with self._pool.connection() as conn, conn.transaction():
            old = conn.execute(
                "SELECT report->>'state' AS state FROM camera_feeds"
                " WHERE printer_id = %s AND part = %s FOR UPDATE",
                (key, report.part),
            ).fetchone()
            conn.execute(
                "INSERT INTO camera_feeds (printer_id, part, report) VALUES (%s, %s, %s)"
                " ON CONFLICT (printer_id, part) DO UPDATE SET report = EXCLUDED.report,"
                " updated_at = now()",
                (key, report.part, Jsonb(report.model_dump(mode="json"))),
            )
        return old is None or old["state"] != report.state
