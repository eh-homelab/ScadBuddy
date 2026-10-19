from __future__ import annotations

import base64
from typing import cast

import pytest

from scadbuddy.core.events import Event, EventBus, PrinterEvent
from scadbuddy.core.secrets import Kek, kek_from_base64
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool

KEK = kek_from_base64(base64.b64encode(b"\x5a" * 32).decode())


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
