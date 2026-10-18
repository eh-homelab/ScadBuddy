from __future__ import annotations

import base64

import pytest

from scadbuddy.core.secrets import Kek, kek_from_base64
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool

KEK = kek_from_base64(base64.b64encode(b"\x5a" * 32).decode())


@pytest.fixture
def kek() -> Kek:
    return KEK


@pytest.fixture
def printers(pg_pool: PgPool) -> PrinterStore:
    return PrinterStore(pg_pool, None)


@pytest.fixture
def printers_with_kek(pg_pool: PgPool, kek: Kek) -> PrinterStore:
    return PrinterStore(pg_pool, kek)
