"""`PRINTERS`: the printer registry (#3823, plan 2026-10-16-printer-identity-native-path,
Task 3).

Building it does no I/O: the rows' pool is made closed and opened on first use, as
`print_library/component.py`'s is. The KEK is the settings' own (`settings_kek`, read
once per process), so a key file that is set but unreadable has already failed the
start. ``probe`` is replaced by tests on the built component."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE
from scadbuddy.core.secrets import Kek
from scadbuddy.library.settings_store import settings_kek
from scadbuddy.printers.address import lan_address
from scadbuddy.printers.lan import LanPrinter, fetch_cert_sha256
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.store import PrinterStore

ROW_POOL_SIZE = 4
CONNECT_TIMEOUT = 5.0
#: The LAN link's whole probe: the certificate read, the MQTT connect and the first
#: report (plan Task 3: "within 10 s").
PROBE_TIMEOUT = 10.0

#: ``(host, serial, access_code)`` -> the first report and the certificate's SHA-256.
Probe = Callable[[str, str, str], Awaitable[tuple[LanReport, str]]]


async def lan_probe(host: str, serial: str, access_code: str) -> tuple[LanReport, str]:
    """Pin the certificate the printer presents now, then ask it for its state over
    that pin: the moment the user types the access code is the trust-on-first-use."""
    sha256 = await asyncio.to_thread(fetch_cert_sha256, await lan_address(host))
    report = await LanPrinter(host, serial, access_code, sha256).probe(timeout=PROBE_TIMEOUT)
    return report, sha256


class Printers:
    def __init__(self, core: Core) -> None:
        self._pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            core.settings.database_url,
            min_size=1,
            max_size=ROW_POOL_SIZE,
            open=False,
            timeout=CONNECT_TIMEOUT,
            connection_class=Connection[DictRow],
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "connect_timeout": max(1, int(CONNECT_TIMEOUT)),
                **TCP_KEEPALIVE,
            },
            name="scadbuddy-printers",
        )
        self._store = PrinterStore(self._pool, settings_kek(core.settings.secret_key_file))
        self._opened = False
        self.probe: Probe = lan_probe

    @property
    def store(self) -> PrinterStore:
        if not self._opened:
            self._pool.open(wait=False)
            self._opened = True
        return self._store

    @property
    def kek(self) -> Kek | None:
        return self._store.kek

    def close(self) -> None:
        self._pool.close()


PRINTERS: Key[Printers] = Key("printers")


def _build(core: Core, components: Components) -> Printers:
    return Printers(core)


@asynccontextmanager
async def _run(printers: Printers) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await asyncio.to_thread(printers.close)


COMPONENT = Component(PRINTERS, build=_build, run=_run)

PrintersDep = Annotated[Printers, component_dep(PRINTERS)]
