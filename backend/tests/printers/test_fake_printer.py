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

The K-profile and AMS commands (#7276) are sent by `LanPrinter.request` itself, pinned,
through `fake_lan.running_fake_lan`: the one test of it on the wire (plan 2026-11-25 P28).
"""

import asyncio
import errno
import ftplib
import hashlib
import io
import json
import os
import socket
import ssl
import sys
import threading
import urllib.request
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import aiomqtt
import pytest

from scadbuddy.printers import lan as lan_module
from scadbuddy.printers.lan import LanPrinter
from scadbuddy.printers.lan_commands import (
    KProfileRow,
    WireSlot,
    ams_change_filament_load,
    ams_change_filament_unload,
    ams_filament_setting,
    ams_get_rfid,
    ams_reset,
    extrusion_cali_del,
    extrusion_cali_get,
    extrusion_cali_sel,
    extrusion_cali_set,
    extrusion_cali_set_slot,
    refusal,
    wire_slot,
)
from tests.support import fake_lan
from tests.support.fake_lan import (
    ACCESS_CODE,
    MOSQUITTO,
    SERIAL,
    needs_fake_lan,
    start_fake_printer,
)

pytestmark = needs_fake_lan


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
    module = fake_lan.load_fake_printer()
    cert = tmp_path / "state" / "cert.pem"
    _, task, ports = await start_fake_printer(
        module,
        lambda ports: module.Config(
            serial=SERIAL,
            access_code=ACCESS_CODE,
            print_seconds=1.0,
            host="127.0.0.1",
            mqtt_port=ports.mqtt,
            ftps_port=ports.ftps,
            control_port=ports.control,
            sdcard=tmp_path / "sdcard",
            state=tmp_path / "state",
            mosquitto=MOSQUITTO,
        ),
    )
    lan = _Lan(ports.mqtt, ports.ftps, ports.control, cert=cert)
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


async def test_a_started_upload_moves_to_the_cache(lan: _Lan, tmp_path: Path) -> None:
    """As the H2C (plan 3a Task 7): once a print starts its file is
    ``/cache/<name>.gcode.3mf``, and a start that names the upload again fails."""
    data = os.urandom(1000)
    md5 = await asyncio.to_thread(lan.upload, data, "/job.3mf")
    await _states_until(lan, "FINISH", _project_file(md5))
    card = tmp_path / "sdcard"
    assert not (card / "job.3mf").exists()
    assert (card / "cache" / "job.3mf.gcode.3mf").read_bytes() == data
    states = await _states_until(lan, "FAILED", _project_file(md5))
    assert states[1]["result"] == "FAIL" and "never uploaded" in states[1]["reason"]


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


async def test_a_start_while_a_print_runs_is_refused_error_state(lan: _Lan) -> None:
    """#6871, as the H2C answered on 2026-11-21: a ``project_file`` sent while the printer
    runs a print is refused ``ERROR STATE`` (its HMS said "Device is busy and cannot
    start new task"), and the print it runs carries on to its end."""
    await asyncio.to_thread(lan.upload, b"x", "/job.3mf")
    await asyncio.to_thread(lan.upload, b"y", "/other.3mf")
    other = _project_file("")
    other["print"].update(url="ftp://other.3mf", subtask_name="other")
    seen: list[dict[str, Any]] = []
    async with asyncio.timeout(30):
        async for report in lan.reports():
            seen.append(report)
            if len(seen) == 1:
                await lan.publish(_project_file(""))
            elif report.get("gcode_state") == "RUNNING" and other not in seen:
                seen.append(other)
                await lan.publish(other)
            elif report.get("gcode_state") == "FINISH":
                break
    echoes = [
        r for r in seen if r.get("command") == "project_file" and r.get("url") == "ftp://other.3mf"
    ]
    assert [(e["result"], e["reason"]) for e in echoes] == [("FAIL", "ERROR STATE")]
    assert seen[-1]["subtask_name"] == "job"
    states = [r["gcode_state"] for r in seen if "gcode_state" in r]
    assert "FAILED" not in states
    refused = lan.control("GET", "/__received")["refused"]
    assert refused == [{"url": "ftp://other.3mf", "reason": "ERROR STATE"}]


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
    # pyftpdlib answers 226 before it calls `on_file_received` (2.2.0,
    # `handlers/ftp/data.py` `close`), so the client can ask before the fake has
    # recorded the last upload (#4991): wait for it, a bounded while.
    async with asyncio.timeout(10):
        while len(received := lan.control("GET", "/__received")["ftps"]) < 2:
            await asyncio.sleep(0.05)
    assert [(e["name"], e["md5"]) for e in received] == [("/one.3mf", first), ("/two.3mf", second)]


@pytest.mark.parametrize(
    ("upload", "url", "reason"),
    [
        (None, "ftp://job.3mf", "never uploaded"),
        ("/x/job.3mf", "ftp://job.3mf", "never uploaded"),
        ("/x/job.3mf", "ftp:///x/job.3mf", "not in the upload directory"),
        (None, "https://elsewhere/job.3mf", "names no file"),
        # Refused as naming nothing, not by the upload-dir check (#4205).
        ("/x/job.3mf", "ftp:///x/../x/job.3mf", "names no file"),
    ],
    ids=[
        "nothing uploaded",
        "uploaded elsewhere",
        "outside the upload dir",
        "no file url",
        "a path that climbs",
    ],
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
        # A `..` climbs off the card: nothing is named (#4205).
        ("ftp:///cache/../../etc/passwd", "", None),
        ("file:///mnt/sdcard/../job.3mf", "", None),
        ("", "../job.3mf", None),
        # A card root that only starts like one names nothing.
        ("file:///mnt/sdcardX/job.3mf", "", None),
        ("file:///sdcard2/job.3mf", "", None),
        ("file:///mnt/sdcard", "", None),
        ("ftp:///a/./b//job.3mf", "", "/a/b/job.3mf"),
    ],
)
def test_resolve_url(url: str, file: str, path: str | None) -> None:
    assert fake_lan.load_fake_printer().resolve_url(url, file) == path


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
    module = fake_lan.load_fake_printer()
    state = tmp_path / "state"
    state.mkdir()
    printer = module.FakePrinter(
        module.Config(serial=SERIAL, access_code=ACCESS_CODE, state=state, sdcard=tmp_path)
    )
    printer._broker_files()
    first = (state / "cert.pem").read_bytes()
    printer._broker_files()
    assert (state / "cert.pem").read_bytes() == first


async def test_a_taken_control_port_fails_the_start_with_its_bind_error(tmp_path: Path) -> None:
    """The bind's own error, which the fixtures retry on new ports: the cleanup must not
    replace it by joining the threads that never started (#4541's CI job hit it)."""
    module = fake_lan.load_fake_printer()
    assert MOSQUITTO is not None
    ftps_port = fake_lan.free_port()
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        printer = module.FakePrinter(
            module.Config(
                serial=SERIAL,
                access_code=ACCESS_CODE,
                host="127.0.0.1",
                mqtt_port=fake_lan.free_port(),
                ftps_port=ftps_port,
                control_port=taken.getsockname()[1],
                sdcard=tmp_path / "sdcard",
                state=tmp_path / "state",
                mosquitto=MOSQUITTO,
            )
        )
        with pytest.raises(OSError) as raised:
            await asyncio.wait_for(printer.run(), 60)
    assert raised.value.errno == errno.EADDRINUSE
    # The FTPS server, whose thread never ran, was closed: its port binds again.
    with socket.socket() as again:
        again.bind(("127.0.0.1", ftps_port))


async def test_a_taken_ftps_port_fails_the_start_with_its_bind_error(tmp_path: Path) -> None:
    """pyftpdlib wraps a failed bind in an OSError with no errno; the fake raises the
    bind's own, so the fixtures see EADDRINUSE and retry (#4944)."""
    module = fake_lan.load_fake_printer()
    assert MOSQUITTO is not None
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        printer = module.FakePrinter(
            module.Config(
                serial=SERIAL,
                access_code=ACCESS_CODE,
                host="127.0.0.1",
                mqtt_port=fake_lan.free_port(),
                ftps_port=taken.getsockname()[1],
                control_port=fake_lan.free_port(),
                sdcard=tmp_path / "sdcard",
                state=tmp_path / "state",
                mosquitto=MOSQUITTO,
            )
        )
        with pytest.raises(OSError) as raised:
            await asyncio.wait_for(printer.run(), 60)
    assert raised.value.errno == errno.EADDRINUSE
    # Raised from pyftpdlib's wrapper, which carries the same error and no errno.
    assert isinstance(raised.value.__cause__, OSError)
    assert raised.value.__cause__.args == (raised.value,)


async def test_the_start_moves_past_a_taken_ftps_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first FTPS port picked is taken by the time the fake binds it (#4944's CI
    job): the start picks new ports and comes up."""
    module = fake_lan.load_fake_printer()
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        # mqtt, ftps, control: the first start's FTPS port is the taken one.
        free_port = fake_lan.free_port  # the real picker, before it is replaced below
        picks = iter([free_port(), taken.getsockname()[1], free_port()])
        monkeypatch.setattr(fake_lan, "free_port", lambda: next(picks, None) or free_port())
        starts: list[fake_lan.Ports] = []

        def config_for(ports: fake_lan.Ports) -> Any:
            starts.append(ports)
            return module.Config(
                serial=SERIAL,
                access_code=ACCESS_CODE,
                host="127.0.0.1",
                mqtt_port=ports.mqtt,
                ftps_port=ports.ftps,
                control_port=ports.control,
                sdcard=tmp_path / "sdcard",
                state=tmp_path / "state",
                mosquitto=MOSQUITTO,
            )

        _, task, ports = await start_fake_printer(module, config_for)
        try:
            # Two starts: the first on the taken FTPS port, the second on new ports.
            assert [start.ftps == taken.getsockname()[1] for start in starts] == [True, False]
            assert ports == starts[-1]
            lan = _Lan(ports.mqtt, ports.ftps, ports.control, cert=tmp_path / "state" / "cert.pem")
            assert (
                lan.upload(b"G28\n", "probe.gcode")
                == hashlib.md5(b"G28\n", usedforsecurity=False).hexdigest()
            )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


# The K-profile and AMS commands (#7276, plan 2026-11-25 Task 3), sent by `LanPrinter.request`
# over the fake's real broker: the only test of `request` on the wire (P28).


@dataclass
class _Linked:
    printer: LanPrinter
    fake: fake_lan.FakeLan


@pytest.fixture
async def linked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Linked]:
    async with fake_lan.running_fake_lan(tmp_path, monkeypatch) as fake:
        printer = LanPrinter(fake.host, fake.serial, fake.access_code, fake.cert_sha256)
        yield _Linked(printer, fake)


async def _ask(
    linked: _Linked, payload: dict[str, Any], *, wait: float = 5.0
) -> dict[str, Any] | None:
    body = payload["print"]
    return await linked.printer.request(
        payload, name=body["command"], seq=body["sequence_id"], wait=wait
    )


async def _result(linked: _Linked, payload: dict[str, Any]) -> str | None:
    reply = await _ask(linked, payload)
    return None if reply is None else str(reply.get("result"))


def _row(cali_idx: int, k_value: str, *, filament_id: str = "GFA00") -> KProfileRow:
    return KProfileRow(
        cali_idx=cali_idx,
        extruder_id=0,
        nozzle_id="HS01-0.4",
        filament_id=filament_id,
        name=f"PLA {k_value}",
        k_value=k_value,
        setting_id="PF00000000000000001",
    )


async def _profiles(linked: _Linked, diameter: str, seq: str) -> list[dict[str, Any]]:
    reply = await _ask(linked, extrusion_cali_get(diameter, seq))
    assert reply is not None
    assert reply["result"] == "success" and reply["nozzle_diameter"] == diameter
    filaments: list[dict[str, Any]] = reply["filaments"]
    return filaments


def _tray(report: dict[str, Any], unit: str, tray: str) -> dict[str, Any]:
    trays = next(u["tray"] for u in report["ams"]["ams"] if u["id"] == unit)
    found: dict[str, Any] = next(t for t in trays if t["id"] == tray)
    return found


async def test_the_report_has_k_values_and_an_idle_ams(linked: _Linked) -> None:
    """As the H2C's report (``lan-report-h2c-idle.json``): each AMS tray has a
    ``cali_idx``, and ``ams_status`` is at the top of ``print`` (P7)."""
    report = (await linked.printer.probe()).print
    assert report["ams_status"] == 0 and "ams_status" not in report["ams"]
    tray = _tray(report, "0", "0")
    assert (tray["cali_idx"], tray["k"]) == (-1, "0.020000")


async def test_a_k_profile_set_then_get_lists_it(linked: _Linked) -> None:
    assert await _profiles(linked, "0.4", "1") == []
    made = await _ask(linked, extrusion_cali_set([_row(-1, "0.025000")], "0.4", "2"))
    assert made is not None
    assert (made["command"], made["sequence_id"], made["result"]) == (
        "extrusion_cali_set",
        "2",
        "success",
    )
    other = _row(-1, "0.030000", filament_id="GFG00")
    assert await _result(linked, extrusion_cali_set([other], "0.4", "3")) == "success"
    listed = await _profiles(linked, "0.4", "4")
    assert [(p["cali_idx"], p["filament_id"], p["k_value"]) for p in listed] == [
        (1, "GFA00", "0.025000"),
        (2, "GFG00", "0.030000"),
    ]
    # As Bambuddy parses a reply (`_parse_kprofile_entries`): the diameter is the envelope's.
    assert set(listed[0]) == {
        "cali_idx",
        "extruder_id",
        "nozzle_id",
        "filament_id",
        "name",
        "k_value",
        "n_coef",
        "setting_id",
    }

    # Another index replaces that profile; a delete removes one; diameters are apart.
    assert await _result(linked, extrusion_cali_set([_row(1, "0.021000")], "0.4", "5")) == (
        "success"
    )
    deleted = extrusion_cali_del(
        cali_idx=2,
        filament_id="GFG00",
        nozzle_id="HS01-0.4",
        nozzle_diameter="0.4",
        extruder_id=0,
        setting_id=None,
        dual_nozzle=True,
        seq="6",
    )
    assert await _result(linked, deleted) == "success"
    listed = await _profiles(linked, "0.4", "7")
    assert [(p["cali_idx"], p["k_value"]) for p in listed] == [(1, "0.021000")]
    assert await _profiles(linked, "0.6", "8") == []

    linked.fake.post("/__reset")
    assert await _profiles(linked, "0.4", "9") == []


async def test_a_slot_configure_shows_in_the_next_report(linked: _Linked) -> None:
    slot = wire_slot(0, 2, external_count=1)
    setting = ams_filament_setting(
        slot,
        tray_info_idx="GFL99",
        tray_type="PETG",
        tray_sub_brands="PETG HF",
        tray_color="#00ae42ff",
        nozzle_temp_min=230,
        nozzle_temp_max=260,
        setting_id="GFSL99",
        seq="11",
    )
    assert await _result(linked, setting) == "success"
    profiles = [_row(3, "0.031000"), _row(4, "0.032000"), _row(5, "0.033000")]
    assert await _result(linked, extrusion_cali_set(profiles, "0.4", "10")) == "success"
    select = extrusion_cali_sel(
        0, 2, external_count=1, cali_idx=3, filament_id="GFL99", nozzle_diameter="0.4", seq="12"
    )
    assert await _result(linked, select) == "success"
    tray = _tray((await linked.printer.probe()).print, "0", "2")
    assert {
        "tray_info_idx": "GFL99",
        "tray_type": "PETG",
        "tray_sub_brands": "PETG HF",
        "tray_color": "00AE42FF",
        "cols": ["00AE42FF"],
        "nozzle_temp_min": "230",
        "nozzle_temp_max": "260",
        "cali_idx": 3,
        "k": "0.031000",
    }.items() <= tray.items()

    # The external holder, and an AMS-HT's one slot.
    external = extrusion_cali_sel(
        255, 0, external_count=1, cali_idx=4, filament_id="", nozzle_diameter="0.4", seq="13"
    )
    ht = extrusion_cali_sel(
        128, 0, external_count=1, cali_idx=5, filament_id="", nozzle_diameter="0.4", seq="14"
    )
    assert await _result(linked, external) == "success"
    assert await _result(linked, ht) == "success"
    report = (await linked.printer.probe()).print
    assert (report["vt_tray"]["cali_idx"], report["vt_tray"]["k"]) == (4, "0.032000")
    assert _tray(report, "128", "0")["cali_idx"] == 5

    # Empty values reset the slot.
    assert await _result(linked, ams_reset(slot, "15")) == "success"
    tray = _tray((await linked.printer.probe()).print, "0", "2")
    assert (tray["tray_type"], tray["tray_info_idx"], tray["tray_color"]) == ("", "", "00000000")
    assert (tray["nozzle_temp_min"], tray["nozzle_temp_max"]) == ("0", "0")


async def test_a_slot_the_printer_lacks_is_refused(linked: _Linked) -> None:
    """A second external holder: this fake reports one (``report.json``)."""
    lacking = ams_filament_setting(
        WireSlot(255, 0, 0),
        tray_info_idx="GFL99",
        tray_type="PLA",
        tray_sub_brands="",
        tray_color="FFFFFFFF",
        nozzle_temp_min=190,
        nozzle_temp_max=240,
        setting_id=None,
        seq="16",
    )
    reply = await _ask(linked, lacking)
    assert reply is not None and refusal(reply) is not None


async def test_a_select_of_a_missing_profile_is_refused(linked: _Linked) -> None:
    """Index 1 exists for 0.4 only: selecting it on 0.6, or index 2 on 0.4, is refused
    and the slot keeps its K."""
    assert await _result(linked, extrusion_cali_set([_row(1, "0.025000")], "0.4", "17")) == (
        "success"
    )
    for seq, (index, diameter) in enumerate([(2, "0.4"), (1, "0.6")], start=18):
        select = extrusion_cali_sel(
            0,
            0,
            external_count=1,
            cali_idx=index,
            filament_id="GFA00",
            nozzle_diameter=diameter,
            seq=str(seq),
        )
        reply = await _ask(linked, select)
        assert reply is not None and refusal(reply) is not None
    tray = _tray((await linked.printer.probe()).print, "0", "0")
    assert (tray["cali_idx"], tray["k"]) == (-1, "0.020000")


async def test_a_slot_k_set_changes_the_tray_not_the_table(linked: _Linked) -> None:
    """``extrusion_cali_set_slot`` (``cali_idx`` -1, ``n_coef`` 1.4, an ``HS00-`` nozzle,
    the flat tray index) sets that slot's K and lists no profile."""
    payload = extrusion_cali_set_slot(
        0,
        3,
        k_value=0.027,
        nozzle_diameter="0.4",
        filament_id="GFG99",
        setting_id="GFSG99",
        name="PETG",
        seq="19",
    )
    assert await _result(linked, payload) == "success"
    tray = _tray((await linked.printer.probe()).print, "0", "3")
    assert (tray["cali_idx"], tray["k"]) == (-1, "0.027000")
    assert await _profiles(linked, "0.4", "20") == []


@pytest.mark.parametrize(
    "command",
    [
        {"command": "extrusion_cali_sel", "cali_idx": "x", "ams_id": 0, "tray_id": 0},
        {"command": "extrusion_cali_sel", "nozzle_diameter": "0.4", "ams_id": 0, "slot_id": 0},
        {"command": "ams_filament_setting", "ams_id": "zero", "tray_id": 0, "slot_id": 0},
        {"command": "ams_filament_setting", "tray_id": 0, "slot_id": 0},
        {"command": "ams_change_filament", "ams_id": 0, "slot_id": 0, "target": "one"},
        {"command": "ams_change_filament", "ams_id": 0, "slot_id": 0},
        {"command": "extrusion_cali_del", "nozzle_diameter": "0.4", "filament_id": "GFA00"},
        {"command": "extrusion_cali_set", "nozzle_diameter": "0.4", "filaments": [{"x": 1}]},
        {"command": "extrusion_cali_get"},
    ],
    ids=[
        "sel index not a number",
        "sel no index",
        "setting unit not a number",
        "setting no unit",
        "change target not a number",
        "change no target",
        "del no index",
        "set entry no index",
        "get no diameter",
    ],
)
async def test_a_malformed_command_is_refused_and_the_printer_carries_on(
    linked: _Linked, command: dict[str, Any]
) -> None:
    payload = {"print": {**command, "sequence_id": "90"}}
    reply = await _ask(linked, payload)
    assert reply is not None and refusal(reply) is not None
    assert await _result(linked, ams_get_rfid(0, 0, "91")) == "success"


async def test_a_refused_k_set_changes_nothing(linked: _Linked) -> None:
    """Every entry is checked before any is written: a second entry with no index fails
    the command, and the first one is not listed."""
    assert await _result(linked, extrusion_cali_set([_row(1, "0.025000")], "0.4", "94")) == (
        "success"
    )
    payload = extrusion_cali_set([_row(1, "0.040000"), _row(2, "0.041000")], "0.4", "95")
    del payload["print"]["filaments"][1]["cali_idx"]
    reply = await _ask(linked, payload)
    assert reply is not None and refusal(reply) is not None
    listed = await _profiles(linked, "0.4", "96")
    assert [(p["cali_idx"], p["k_value"]) for p in listed] == [(1, "0.025000")]


async def test_a_reset_restores_the_ams(linked: _Linked) -> None:
    before = (await linked.printer.probe()).print
    slot = wire_slot(0, 0, external_count=1)
    assert await _result(linked, ams_reset(slot, "92")) == "success"
    load = ams_change_filament_load(1, external_count=1, extruder_id=0, right_temp=0, seq="93")
    assert await _result(linked, load) == "success"
    assert (await linked.printer.probe()).print["ams"]["tray_now"] == "1"
    linked.fake.post("/__reset")
    after = (await linked.printer.probe()).print
    assert after["ams"] == before["ams"]
    assert after["vt_tray"] == before["vt_tray"]
    assert after["ams_status"] == 0


async def test_a_load_moves_tray_now_and_unload_returns_255(linked: _Linked) -> None:
    load = ams_change_filament_load(1, external_count=1, extruder_id=0, right_temp=0, seq="21")
    seen: list[dict[str, Any]] = []
    async with asyncio.timeout(10):
        async for report in linked.printer.reports(command=load):
            seen.append(dict(report.print))
            if report.print["ams"]["tray_now"] == "1":
                break
    replies = [r for r in seen if r.get("result") == "success"]
    assert replies and replies[0]["command"] == "ams_change_filament"
    assert replies[0]["sequence_id"] == "21"
    # The report the change pushed reads busy; the next one is idle again.
    assert seen[-1]["ams_status"] == 0x0100
    after = (await linked.printer.probe()).print
    assert (after["ams"]["tray_now"], after["ams_status"]) == ("1", 0)

    unload = ams_change_filament_unload(1, external_count=1, nozzle_temp=220, seq="22")
    assert await _result(linked, unload) == "success"
    assert (await linked.printer.probe()).print["ams"]["tray_now"] == "255"


async def test_a_tag_read_is_answered(linked: _Linked) -> None:
    assert await _result(linked, ams_get_rfid(0, 1, "31")) == "success"


async def test_a_refused_command_answers_fail_with_the_reason(linked: _Linked) -> None:
    linked.fake.post("/__refuse", {"command": "extrusion_cali_set", "reason": "invalid tray_id"})
    reply = await _ask(linked, extrusion_cali_set([_row(-1, "0.025000")], "0.4", "41"))
    assert reply is not None
    assert (reply["command"], reply["sequence_id"]) == ("extrusion_cali_set", "41")
    assert refusal(reply) == "invalid tray_id"
    # Refused: nothing was written; and only the next such command is refused.
    assert await _profiles(linked, "0.4", "42") == []
    again = await _ask(linked, extrusion_cali_set([_row(-1, "0.025000")], "0.4", "43"))
    assert again is not None and refusal(again) is None


async def test_a_silent_command_answers_nothing(linked: _Linked) -> None:
    linked.fake.post("/__silent", {"command": "ams_get_rfid"})
    assert await _ask(linked, ams_get_rfid(0, 0, "51"), wait=1.0) is None
    assert await _result(linked, ams_get_rfid(0, 0, "52")) == "success"
    sent = [
        e["print"]["sequence_id"]
        for e in linked.fake.received()["mqtt"]
        if e.get("print", {}).get("command") == "ams_get_rfid"
    ]
    assert sent == ["51", "52"]


async def test_a_reset_clears_the_refusals_and_silences(linked: _Linked) -> None:
    linked.fake.post("/__refuse", {"command": "ams_get_rfid", "reason": "no"})
    linked.fake.post("/__silent", {"command": "extrusion_cali_get"})
    linked.fake.post("/__reset")
    assert await _result(linked, ams_get_rfid(0, 0, "61")) == "success"
    assert await _profiles(linked, "0.4", "62") == []


# A cut connection is shut by force (P31): an aiomqtt release that renames what
# `lan._force_close` reaches into fails here, instead of leaving a printer's client slot held.


def _spy_force_close(monkeypatch: pytest.MonkeyPatch) -> list[aiomqtt.Client]:
    """The clients `lan._force_close` shuts, so a test proves the cut path ran."""
    shut: list[aiomqtt.Client] = []
    force_close = lan_module._force_close

    def spy(client: aiomqtt.Client) -> None:
        shut.append(client)
        force_close(client)

    monkeypatch.setattr(lan_module, "_force_close", spy)
    return shut


async def test_a_cut_connect_is_shut(linked: _Linked, monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker takes the connection, and the client waits for its CONNACK past
    `MQTT_TIMEOUT`: the connect is cut inside ``__aenter__`` with nothing entered, and its
    socket is closed at once, not left open until the broker's keepalive."""
    shut = _spy_force_close(monkeypatch)
    held: list[Any] = []

    class HeldAtConnack(aiomqtt.Client):
        async def _wait_for(self, fut: Any, timeout: float | None) -> Any:
            if fut is self._connected:
                held.append(self._client.socket())
                await asyncio.Event().wait()
            return await super()._wait_for(fut, timeout)

    monkeypatch.setattr(aiomqtt, "Client", HeldAtConnack)
    monkeypatch.setattr(lan_module, "MQTT_TIMEOUT", 1.0)
    with pytest.raises(TimeoutError):
        await _ask(linked, ams_get_rfid(0, 0, "71"))
    assert len(shut) == 1
    assert len(held) == 1 and held[0] is not None and held[0].fileno() == -1


async def test_a_late_executor_connect_is_shut(
    linked: _Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """aiomqtt connects in an executor thread, and a cut does not stop that thread (#7616).
    The connect is held past `MQTT_TIMEOUT`, then let go: it opens a socket after the cut,
    and that socket is closed when the thread ends, not left open."""
    shut = _spy_force_close(monkeypatch)
    go = threading.Event()
    done = threading.Event()
    held: list[Any] = []

    class HeldInExecutor(aiomqtt.Client):
        def _client_connect(self) -> None:
            go.wait()
            super()._client_connect()
            held.append(self._client.socket())

    loop = asyncio.get_running_loop()
    run_in_executor = loop.run_in_executor

    def tracked(executor: Any, func: Callable[..., Any], *args: Any) -> Any:
        def run() -> Any:
            try:
                return func(*args)
            finally:
                done.set()

        return run_in_executor(executor, run)

    monkeypatch.setattr(loop, "run_in_executor", tracked)
    monkeypatch.setattr(aiomqtt, "Client", HeldInExecutor)
    monkeypatch.setattr(lan_module, "MQTT_TIMEOUT", 0.5)
    try:
        with pytest.raises(TimeoutError):
            await _ask(linked, ams_get_rfid(0, 0, "91"))
        assert len(shut) == 1
    finally:
        go.set()
    assert await asyncio.to_thread(done.wait, 30.0)
    assert len(held) == 1 and held[0] is not None
    try:
        assert held[0].fileno() == -1
    finally:
        held[0].close()


async def test_a_cut_close_is_shut(linked: _Linked, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reply is in, then the close never ends (``__aexit__`` hangs before it sends a
    DISCONNECT, so paho never closes the socket itself): `MQTT_TIMEOUT` cuts the close, the
    reply stands, and `lan._force_close` closes the socket at once."""
    shut = _spy_force_close(monkeypatch)
    held: list[Any] = []

    class HangsOnClose(aiomqtt.Client):
        async def __aexit__(self, *exc: Any) -> None:
            held.append(self._client.socket())
            await asyncio.Event().wait()

    monkeypatch.setattr(aiomqtt, "Client", HangsOnClose)
    monkeypatch.setattr(lan_module, "MQTT_TIMEOUT", 1.0)
    reply = await _ask(linked, ams_get_rfid(0, 0, "81"))
    assert reply is not None and reply["result"] == "success"
    assert len(shut) == 1
    assert len(held) == 1 and held[0] is not None and held[0].fileno() == -1
