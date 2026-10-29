"""The fake LAN printer (``e2e-stack/fake-printer/printer.py``) run in-process for a test,
with `LanPrinter` pointed at it: its ports, and its loopback address, which
`address.lan_address` refuses for a real printer. It needs a mosquitto (``mosquitto`` on
PATH, or ``SCADBUDDY_TEST_MQTT`` naming the binary or a script that runs one), and a test
using it skips without one (`MOSQUITTO`)."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import importlib.util
import json
import os
import shutil
import socket
import sys
import urllib.request
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from scadbuddy.printers import lan as lan_module
from scadbuddy.printers.lan import fetch_cert_sha256

PRINTER_PY = Path(__file__).resolve().parents[3] / "e2e-stack" / "fake-printer" / "printer.py"
MOSQUITTO = os.environ.get("SCADBUDDY_TEST_MQTT") or shutil.which("mosquitto")
SERIAL = "01P00FAKE000001"
ACCESS_CODE = "12345678"

needs_fake_lan = [
    pytest.mark.skipif(MOSQUITTO is None, reason="needs mosquitto on PATH or SCADBUDDY_TEST_MQTT"),
    pytest.mark.skipif(not PRINTER_PY.is_file(), reason=f"{PRINTER_PY} is not in this tree"),
]


def load_fake_printer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fake_printer", PRINTER_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fake_printer"] = module
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@dataclass
class FakeLan:
    """A running fake printer: where it is, its pin, and its record of what it got."""

    host: str
    serial: str
    access_code: str
    cert_sha256: str
    control_port: int
    sdcard: Path
    printer: Any

    def received(self) -> dict[str, Any]:
        url = f"http://127.0.0.1:{self.control_port}/__received"
        with urllib.request.urlopen(url, timeout=5) as answer:
            body: dict[str, Any] = json.loads(answer.read())
        return body

    def project_files(self) -> list[dict[str, Any]]:
        return [
            entry["print"]
            for entry in self.received()["mqtt"]
            if entry.get("print", {}).get("command") == "project_file"
        ]


async def _loopback(host: str) -> str:
    return host


@dataclass(frozen=True)
class Ports:
    mqtt: int
    ftps: int
    control: int


async def start_fake_printer(
    module: ModuleType, config_for: Callable[[Ports], Any], *, attempts: int = 3
) -> tuple[Any, asyncio.Task[None], Ports]:
    """Start the fake printer on ports `free_port` picks, and wait until it is ready.

    A port `free_port` picked can be taken by a parallel worker before the fake binds it
    ("Address already in use"): start again on new ports, up to ``attempts`` times. The
    MQTT port's clash is the broker's exit; the FTPS and control ports' is the bind's
    EADDRINUSE (#4765, #4944)."""
    for attempt in range(attempts):
        ports = Ports(free_port(), free_port(), free_port())
        printer = module.FakePrinter(config_for(ports))
        ready = asyncio.Event()
        task = asyncio.create_task(printer.run(ready))
        waiting = asyncio.create_task(ready.wait())
        done, _ = await asyncio.wait(
            [task, waiting], timeout=60, return_when=asyncio.FIRST_COMPLETED
        )
        waiting.cancel()
        if task in done:
            try:
                task.result()  # the printer failed to start: raise its error
            except (OSError, RuntimeError) as error:
                clash = getattr(error, "errno", None) == errno.EADDRINUSE or (
                    "mosquitto exited" in str(error)
                )
                if not clash or attempt == attempts - 1:
                    raise
                continue
        if not ready.is_set():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise AssertionError("the fake printer did not come up")
        return printer, task, ports
    raise AssertionError("unreachable")


@asynccontextmanager
async def running_fake_lan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, print_seconds: float = 1.0
) -> AsyncIterator[FakeLan]:
    """Start the fake printer and point `scadbuddy.printers.lan` at it until the end."""
    assert MOSQUITTO is not None
    module = load_fake_printer()
    printer, task, ports = await start_fake_printer(
        module,
        lambda ports: module.Config(
            serial=SERIAL,
            access_code=ACCESS_CODE,
            print_seconds=print_seconds,
            host="127.0.0.1",
            mqtt_port=ports.mqtt,
            ftps_port=ports.ftps,
            control_port=ports.control,
            sdcard=tmp_path / "sdcard",
            state=tmp_path / "state",
            mosquitto=MOSQUITTO,
        ),
    )
    monkeypatch.setattr(lan_module, "MQTT_PORT", ports.mqtt)
    monkeypatch.setattr(lan_module, "FTPS_PORT", ports.ftps)
    monkeypatch.setattr(lan_module, "lan_address", _loopback)
    try:
        yield FakeLan(
            host="127.0.0.1",
            serial=SERIAL,
            access_code=ACCESS_CODE,
            cert_sha256=await asyncio.to_thread(fetch_cert_sha256, "127.0.0.1", ports.mqtt),
            control_port=ports.control,
            sdcard=tmp_path / "sdcard",
            printer=printer,
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
