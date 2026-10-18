"""`printer_reports`: each LAN printer's latest merged MQTT report (#3823, plan
2026-10-16, Task 6), written by its `PrinterWatch` and read by ``/printers/now``, the
print dialog and the native follow. ``seen_at`` is when the printer was last heard
from: a report, or the connection still being up (`touch`)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.printers.store import printer_uuid


class PrinterReportStore:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self.pool = pool

    def put(self, printer_id: str, report: dict[str, Any], seen_at: datetime) -> None:
        """Write the report. A printer gone from the registry (deleted while it was
        watched) or no longer LAN-linked (an attempt still running after the unlink)
        writes nothing."""
        key = printer_uuid(printer_id)
        if key is None:
            return
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO printer_reports (printer_id, report, seen_at)"
                " SELECT id, %s, %s FROM printers WHERE id = %s AND lan_host IS NOT NULL"
                " ON CONFLICT (printer_id) DO UPDATE"
                " SET report = EXCLUDED.report, seen_at = EXCLUDED.seen_at",
                (Jsonb(report), seen_at, printer_id),
            )

    def touch(self, printer_id: str, seen_at: datetime) -> None:
        """Heard from as late as ``seen_at``, with nothing new to say; never earlier than
        what the row already says."""
        key = printer_uuid(printer_id)
        if key is None:
            return
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE printer_reports SET seen_at = GREATEST(seen_at, %s) WHERE printer_id = %s",
                (seen_at, printer_id),
            )

    def get(self, printer_id: str) -> tuple[dict[str, Any], datetime] | None:
        key = printer_uuid(printer_id)
        if key is None:
            return None
        with self.pool.connection() as conn:
            data = conn.execute(
                "SELECT report, seen_at FROM printer_reports WHERE printer_id = %s",
                (printer_id,),
            ).fetchone()
        return None if data is None else (dict(data["report"]), data["seen_at"])

    def all(self) -> dict[str, tuple[dict[str, Any], datetime]]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT printer_id, report, seen_at FROM printer_reports"
            ).fetchall()
        return {str(row["printer_id"]): (dict(row["report"]), row["seen_at"]) for row in rows}
