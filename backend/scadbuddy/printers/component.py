"""`PRINTERS`: the printer registry (#3823, plan 2026-10-16-printer-identity-native-path,
Task 3).

Building it does no I/O: the rows' pool is made closed and opened on first use, as
`print_library/component.py`'s is. The KEK is the settings' own (`settings_kek`, read
once per process), so a key file that is set but unreadable has already failed the
start. ``probe`` is replaced by tests on the built component."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.events import PrinterEvent, emit
from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE
from scadbuddy.core.secrets import Kek
from scadbuddy.library.settings_store import settings_kek
from scadbuddy.printers.address import lan_address
from scadbuddy.printers.lan import LanPrinter, fetch_cert_sha256
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.workflows.follow import RPC_TIMEOUT
from scadbuddy.workflows.printer_watch import ensure_watch, restart_watch, stop_watch

logger = logging.getLogger(__name__)

ROW_POOL_SIZE = 4
CONNECT_TIMEOUT = 5.0
#: The LAN link's whole probe: the certificate read, the MQTT connect and the first
#: report (plan Task 3: "within 10 s").
PROBE_TIMEOUT = 10.0

#: A watch call's whole bound: the API's Temporal client is lazy, and its first connect
#: retries for minutes when Temporal is down (`follow.follow` bounds it the same way).
WATCH_RPC_BOUND = RPC_TIMEOUT.total_seconds() + 2

#: ``(host, serial, access_code)`` -> the first report and the certificate's SHA-256.
Probe = Callable[[str, str, str], Awaitable[tuple[LanReport, str]]]


async def lan_probe(host: str, serial: str, access_code: str) -> tuple[LanReport, str]:
    """Pin the certificate the printer presents now, then ask it for its state over
    that pin: the moment the user types the access code is the trust-on-first-use."""
    sha256 = await asyncio.to_thread(fetch_cert_sha256, await lan_address(host))
    report = await LanPrinter(host, serial, access_code, sha256).probe(timeout=PROBE_TIMEOUT)
    return report, sha256


class Watches:
    """Each LAN printer's `PrinterWatch`, started, restarted and stopped on the print
    queue through the API's (lazy) Temporal client. Best effort, and bounded by
    `WATCH_RPC_BOUND`: a failure is logged and the housekeeping prune Schedule
    (`ensure_watches`) starts what is missing."""

    def __init__(self, core: Core) -> None:
        self.core = core

    async def ensure(self, printer_id: str, serial: str) -> None:
        def call() -> Awaitable[None]:
            commands = self.core.print_runs
            return ensure_watch(commands.client, commands.task_queue, printer_id, serial)

        await self._bounded("start", printer_id, call)

    async def restart(self, printer_id: str, serial: str) -> None:
        def call() -> Awaitable[None]:
            commands = self.core.print_runs
            return restart_watch(commands.client, commands.task_queue, printer_id, serial)

        await self._bounded("restart", printer_id, call)

    async def stop(self, printer_id: str, serial: str) -> None:
        await self._bounded(
            "stop", printer_id, lambda: stop_watch(self.core.print_runs.client, serial)
        )

    async def _bounded(
        self, what: str, printer_id: str, call: Callable[[], Awaitable[None]]
    ) -> None:
        try:
            async with asyncio.timeout(WATCH_RPC_BOUND):
                await call()
        except Exception:
            logger.warning("could not %s a printer watch", what, extra={"printer_id": printer_id})


class Printers:
    def __init__(self, core: Core) -> None:
        self.core = core
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
        self._store = PrinterStore(
            self._pool, settings_kek(core.settings.secret_key_file), events=core.events
        )
        self._reports = PrinterReportStore(self._pool)
        self._opened = False
        self.probe: Probe = lan_probe
        self.watches = Watches(core)

    @property
    def store(self) -> PrinterStore:
        self._open()
        return self._store

    def _open(self) -> None:
        if not self._opened:
            self._pool.open(wait=False)
            self._opened = True

    @property
    def reports(self) -> PrinterReportStore:
        self._open()
        return self._reports

    def changed(self, printer_id: str) -> None:
        """Announce a write to ``printer_id``'s row (``printer.changed``)."""
        emit(self.core.events, PrinterEvent(kind="printer.changed", printer_id=printer_id))

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
