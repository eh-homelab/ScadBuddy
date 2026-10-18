"""The fake LAN printer (``e2e-stack/fake-printer/printer.py``, #3818 tier A, Task 12)
answers a LAN client as a Bambu printer does: MQTT over TLS on its broker, implicit
FTPS, and the print states a ``project_file`` runs through.

It runs here in-process, with its own mosquitto (``mosquitto`` on PATH, or
``SCADBUDDY_TEST_MQTT`` naming the binary or a script that runs one), and skips without
one. The client below speaks the protocol the way 3a's ``LanPrinter``
(``scadbuddy/printers/lan.py`` on ``feat/printer-registry``) does: aiomqtt over TLS,
``pushing.pushall`` then the reports, and ha-bambulab's implicit
FTPS (the control socket wrapped at connect, each data connection wrapped with the
control connection's session, no ``unwrap`` at the end of a ``STOR``). That module is not
on main yet; once it is, this test drives it instead (its ports are fixed at 8883/990,
so the fake's are configurable for now). One difference: this client verifies the
fake's certificate, which names ``127.0.0.1``, where ``LanPrinter`` pins a printer's
SHA-256 (``test_the_certificate_survives_a_restart`` keeps that pin meaningful).
"""

import asyncio
import ftplib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import socket
import ssl
import sys
import urllib.request
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, BinaryIO

import aiomqtt
import pytest

PRINTER_PY = Path(__file__).resolve().parents[3] / "e2e-stack" / "fake-printer" / "printer.py"
MOSQUITTO = os.environ.get("SCADBUDDY_TEST_MQTT") or shutil.which("mosquitto")
SERIAL = "01P00FAKE000001"
ACCESS_CODE = "12345678"

pytestmark = [
    pytest.mark.skipif(MOSQUITTO is None, reason="needs mosquitto on PATH or SCADBUDDY_TEST_MQTT"),
    pytest.mark.skipif(not PRINTER_PY.is_file(), reason=f"{PRINTER_PY} is not in this tree"),
]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fake_printer", PRINTER_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fake_printer"] = module
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _trusting(cert: Path) -> ssl.SSLContext:
    """A client context that trusts the fake's own self-signed certificate (it names
    ``127.0.0.1``), and nothing else: verified, unlike a real printer's, which names no
    host and is pinned instead."""
    return ssl.create_default_context(cafile=str(cert))


class _ImplicitFtpTls(ftplib.FTP_TLS):
    """As ``LanPrinter``'s ``ImplicitFtpTls`` (after ha-bambulab's ``ImplicitFTP_TLS``),
    except that it checks the host name, which the fake's certificate carries."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._sock: Any = None
        super().__init__(*args, **kwargs)

    @property
    def sock(self) -> Any:
        return self._sock

    @sock.setter
    def sock(self, value: Any) -> None:
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(
        self, cmd: str, rest: int | str | None = None
    ) -> tuple[socket.socket, int | None]:
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:  # type: ignore[attr-defined]
            session = self.sock.session if isinstance(self.sock, ssl.SSLSocket) else None
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=session)
        return conn, size

    def store_no_unwrap(
        self, cmd: str, data: BinaryIO, callback: Callable[[bytes], None] | None = None
    ) -> str:
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd) as conn:
            while block := data.read(64 * 1024):
                conn.sendall(block)
                if callback is not None:
                    callback(block)
        return self.voidresp()


@dataclass
class _Lan:
    mqtt_port: int
    ftps_port: int
    control_port: int
    cert: Path
    access_code: str = ACCESS_CODE

    def _client(self) -> aiomqtt.Client:
        return aiomqtt.Client(
            "127.0.0.1",
            self.mqtt_port,
            username="bblp",
            password=self.access_code,
            tls_context=_trusting(self.cert),
            timeout=10.0,
        )

    async def reports(self) -> AsyncIterator[dict[str, Any]]:
        merged: dict[str, Any] = {}
        async with self._client() as client:
            await client.subscribe(f"device/{SERIAL}/report")
            pushall = {"pushing": {"sequence_id": "0", "command": "pushall"}}
            await client.publish(f"device/{SERIAL}/request", json.dumps(pushall))
            async for message in client.messages:
                assert isinstance(message.payload, bytes)
                merged = {**merged, **json.loads(message.payload)["print"]}
                yield merged

    async def probe(self) -> dict[str, Any]:
        async with asyncio.timeout(10):
            async for report in self.reports():
                return report
        raise AssertionError("no report")

    async def publish(self, command: dict[str, Any]) -> None:
        async with self._client() as client:
            await client.publish(f"device/{SERIAL}/request", json.dumps(command))

    def upload(self, data: bytes, remote_name: str) -> str:
        md5 = hashlib.md5(usedforsecurity=False)
        ftp = _ImplicitFtpTls(context=_trusting(self.cert), timeout=10)
        try:
            ftp.connect("127.0.0.1", self.ftps_port)
            ftp.login("bblp", self.access_code)
            ftp.prot_p()
            ftp.store_no_unwrap(f"STOR {remote_name}", io.BytesIO(data), md5.update)
        finally:
            ftp.close()
        return md5.hexdigest()

    def control(self, method: str, path: str) -> Any:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.control_port}{path}", method=method
        )
        with urllib.request.urlopen(request, timeout=5) as answer:
            body = answer.read()
        return json.loads(body) if body else None


@pytest.fixture
async def lan(tmp_path: Path) -> AsyncIterator[_Lan]:
    module = _load()
    lan = _Lan(_free_port(), _free_port(), _free_port(), cert=tmp_path / "state" / "cert.pem")
    config = module.Config(
        serial=SERIAL,
        access_code=ACCESS_CODE,
        print_seconds=1.0,
        host="127.0.0.1",
        mqtt_port=lan.mqtt_port,
        ftps_port=lan.ftps_port,
        control_port=lan.control_port,
        sdcard=tmp_path / "sdcard",
        state=tmp_path / "state",
        mosquitto=MOSQUITTO,
    )
    printer = module.FakePrinter(config)
    ready = asyncio.Event()
    task = asyncio.create_task(printer.run(ready))
    done, _ = await asyncio.wait(
        [task, asyncio.create_task(ready.wait())], timeout=60, return_when=asyncio.FIRST_COMPLETED
    )
    if task in done:
        task.result()  # the printer failed to start: raise its error
    assert ready.is_set(), "the fake printer did not come up"
    try:
        yield lan
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def _project_file(md5: str) -> dict[str, Any]:
    return {
        "print": {
            "command": "project_file",
            "sequence_id": "7",
            "param": "Metadata/plate_1.gcode",
            "url": "ftp://job.3mf",
            "subtask_name": "job",
            "md5": md5,
            "use_ams": True,
            "ams_mapping": [0, -1],
            "ams_mapping2": [{"ams_id": 0, "slot_id": 0}, {"ams_id": 255, "slot_id": 255}],
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
        }
    }


async def _states_until(lan: _Lan, final: str, command: dict[str, Any]) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    async with asyncio.timeout(30):
        async for report in lan.reports():
            if not seen:
                await lan.publish(command)
            seen.append(report)
            if report["gcode_state"] == final:
                return seen
    raise AssertionError(f"never reached {final}")


async def test_a_probe_gets_the_whole_report(lan: _Lan) -> None:
    """An H2C's report: two extruders and a rack (nozzle ids from 16), as 3a's
    ``status_from_report`` reads ``device`` (``scadbuddy/printers/status.py`` on
    ``feat/printer-registry``); once that is on main, parse it with that instead."""
    report = await lan.probe()
    assert report["command"] == "push_status"
    assert report["gcode_state"] == "IDLE"
    trays = [tray for unit in report["ams"]["ams"] for tray in unit["tray"]]
    tagged = [tray for tray in trays if tray.get("tray_uuid", "0" * 32) != "0" * 32]
    assert len(tagged) == 1 and tagged[0]["tag_uid"] != "0" * 16
    device = report["device"]
    assert [e["id"] for e in device["extruder"]["info"]] == [0, 1]
    nozzle_ids = [n["id"] for n in device["nozzle"]["info"]]
    assert {0, 1} <= set(nozzle_ids) and any(i >= 16 for i in nozzle_ids)


async def test_a_print_is_uploaded_recorded_and_runs_to_finish(lan: _Lan) -> None:
    data = os.urandom(200_000)
    md5 = await asyncio.to_thread(lan.upload, data, "/job.3mf")
    assert md5 == hashlib.md5(data, usedforsecurity=False).hexdigest()

    states = await _states_until(lan, "FINISH", _project_file(md5))
    assert {"command": "project_file", "url": "ftp://job.3mf", "result": "SUCCESS"}.items() <= (
        states[1].items()
    )
    names = [report["gcode_state"] for report in states]
    assert names[0] == "IDLE"
    assert names.index("PREPARE") < names.index("RUNNING") < names.index("FINISH")
    assert states[-1]["mc_percent"] == 100
    assert states[-1]["subtask_name"] == "job"
    assert states[-1]["gcode_file"] == "job.3mf"

    received = lan.control("GET", "/__received")
    assert received["ftps"] == [{"name": "/job.3mf", "size": len(data), "md5": md5}]
    commands = [entry.get("print", {}).get("command") for entry in received["mqtt"]]
    assert "project_file" in commands
    sent = next(e for e in received["mqtt"] if e.get("print", {}).get("command") == "project_file")
    assert sent == _project_file(md5)

    assert lan.control("POST", "/__reset") is None
    assert lan.control("GET", "/__received") == {"mqtt": [], "ftps": [], "refused": []}


async def test_a_stop_fails_the_print(lan: _Lan) -> None:
    await asyncio.to_thread(lan.upload, b"x", "/job.3mf")
    stop = {"print": {"command": "stop", "sequence_id": "8", "param": ""}}
    seen: list[str] = []
    async with asyncio.timeout(30):
        async for report in lan.reports():
            seen.append(report["gcode_state"])
            if len(seen) == 1:
                await lan.publish(_project_file(""))
            elif report["gcode_state"] == "RUNNING" and "stop" not in seen:
                seen.append("stop")
                await lan.publish(stop)
            elif report["gcode_state"] == "FAILED":
                break
    assert "FINISH" not in seen


async def test_the_upload_dir_takes_a_file(lan: _Lan, tmp_path: Path) -> None:
    module = sys.modules["fake_printer"]
    upload_dir = module.DEFAULT_UPLOAD_DIR.rstrip("/")
    await asyncio.to_thread(lan.upload, b"x", f"{upload_dir}/a.3mf")
    assert (tmp_path / "sdcard" / upload_dir.lstrip("/") / "a.3mf").read_bytes() == b"x"


async def test_a_wrong_access_code_is_refused(lan: _Lan) -> None:
    wrong = _Lan(
        lan.mqtt_port, lan.ftps_port, lan.control_port, cert=lan.cert, access_code="00000000"
    )
    with pytest.raises(aiomqtt.MqttError):
        await wrong.probe()
    with pytest.raises(ftplib.error_perm):
        await asyncio.to_thread(wrong.upload, b"x", "/x.3mf")


async def test_plain_ftp_is_refused(lan: _Lan) -> None:
    """Implicit FTPS: TLS from the first byte, so a plain client gets no banner."""
    ftp = ftplib.FTP(timeout=2)
    with pytest.raises((TimeoutError, EOFError, ftplib.Error)):
        await asyncio.to_thread(ftp.connect, "127.0.0.1", lan.ftps_port)
    ftp.close()


async def test_each_upload_is_its_own_session(lan: _Lan) -> None:
    first = await asyncio.to_thread(lan.upload, b"one", "/one.3mf")
    second = await asyncio.to_thread(lan.upload, b"two", "/two.3mf")
    received = lan.control("GET", "/__received")["ftps"]
    assert [(e["name"], e["md5"]) for e in received] == [("/one.3mf", first), ("/two.3mf", second)]


@pytest.mark.parametrize(
    ("upload", "url", "reason"),
    [
        (None, "ftp://job.3mf", "never uploaded"),
        ("/x/job.3mf", "ftp://job.3mf", "never uploaded"),
        ("/x/job.3mf", "ftp:///x/job.3mf", "not in the upload directory"),
        (None, "https://elsewhere/job.3mf", "names no file"),
    ],
    ids=["nothing uploaded", "uploaded elsewhere", "outside the upload dir", "no file url"],
)
async def test_a_project_file_naming_no_uploaded_file_fails(
    lan: _Lan, tmp_path: Path, upload: str | None, url: str, reason: str
) -> None:
    (tmp_path / "sdcard" / "x").mkdir()
    if upload is not None:
        await asyncio.to_thread(lan.upload, b"x", upload)
    command = _project_file("")
    command["print"]["url"] = url
    states = await _states_until(lan, "FAILED", command)
    assert states[1]["result"] == "FAIL" and reason in states[1]["reason"]
    assert states[-1]["print_error"] != 0
    assert "RUNNING" not in [s["gcode_state"] for s in states]
    refused = lan.control("GET", "/__received")["refused"]
    assert [r["url"] for r in refused] == [url] and reason in refused[0]["reason"]


@pytest.mark.parametrize(
    ("url", "file", "path"),
    [
        ("ftp://job.3mf", "", "/job.3mf"),
        ("ftp:///cache/job.3mf", "", "/cache/job.3mf"),
        ("file:///mnt/sdcard/job.3mf", "", "/job.3mf"),
        ("file:///sdcard/a/job.3mf", "", "/a/job.3mf"),
        ("", "job.3mf", "/job.3mf"),
        ("", "", None),
        ("ftp://", "", None),
        ("https://x/job.3mf", "", None),
    ],
)
def test_resolve_url(url: str, file: str, path: str | None) -> None:
    assert _load().resolve_url(url, file) == path


async def test_bblp_may_use_only_its_printers_topics(lan: _Lan) -> None:
    """The broker's ACL: a topic other than the printer's two is neither delivered nor
    published to; the report that follows it is."""
    async with lan._client() as client:
        await client.subscribe("elsewhere/topic")
        await client.subscribe(f"device/{SERIAL}/report")
        await client.publish("elsewhere/topic", b"{}")
        pushall = {"pushing": {"sequence_id": "0", "command": "pushall"}}
        await client.publish(f"device/{SERIAL}/request", json.dumps(pushall))
        async with asyncio.timeout(10):
            async for message in client.messages:
                assert str(message.topic) == f"device/{SERIAL}/report"
                break


async def test_the_certificate_survives_a_restart(tmp_path: Path) -> None:
    module = _load()
    state = tmp_path / "state"
    state.mkdir()
    printer = module.FakePrinter(
        module.Config(serial=SERIAL, access_code=ACCESS_CODE, state=state, sdcard=tmp_path)
    )
    printer._broker_files()
    first = (state / "cert.pem").read_bytes()
    printer._broker_files()
    assert (state / "cert.pem").read_bytes() == first
