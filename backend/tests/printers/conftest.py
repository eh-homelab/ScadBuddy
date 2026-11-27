from __future__ import annotations

import base64
from typing import Any, cast

import pytest

from scadbuddy.core.events import Event, EventBus, PrinterEvent
from scadbuddy.core.secrets import Kek, kek_from_base64
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool

KEK = kek_from_base64(base64.b64encode(b"\x5a" * 32).decode())


def unkeyed(report: Any) -> Any:
    """``report`` with each recorded tray's RFID keys zeroed. The recorded fixtures keep
    ``REDACTED`` there, which reads as a valid key (``EDACED``), so a report stored through
    the real store would make a spool for it (#7143). A test that is not about capture
    stores this."""
    if isinstance(report, dict):
        zeros = {"tag_uid": "0" * 16, "tray_uuid": "0" * 32}
        return {k: zeros[k] if k in zeros else unkeyed(v) for k, v in report.items()}
    if isinstance(report, list):
        return [unkeyed(v) for v in report]
    return report


@pytest.fixture
def kek() -> Kek:
    return KEK


class RecordingBus:
    """An `EventBus` that only records what is published."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)

    def changed(self) -> list[str]:
        return [e.printer_id for e in self.events if isinstance(e, PrinterEvent)]


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def printers(pg_pool: PgPool, bus: RecordingBus) -> PrinterStore:
    return PrinterStore(pg_pool, None, events=cast(EventBus, bus))


@pytest.fixture
def printers_with_kek(pg_pool: PgPool, kek: Kek) -> PrinterStore:
    return PrinterStore(pg_pool, kek)
