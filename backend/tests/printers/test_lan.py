"""The LAN client's pieces that need no printer (#3823, plan 2026-10-16, Task 4): the
command shapes (OpenBambuAPI `mqtt.md`), the report merge, the implicit-FTPS socket
wrapping and the certificate pin."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import hashlib
import json
import logging
import socket
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import aiomqtt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from scadbuddy.printers.lan import (
    UPLOAD_DIR,
    CertificateMismatchError,
    ImplicitFtpTls,
    LanPrinter,
    fetch_cert_sha256,
    file_url,
    merge_report,
    pinned_context,
    project_file_command,
    pushall_command,
)
from scadbuddy.printers.lan_models import ProjectFile


def test_the_measured_upload_place_and_url() -> None:
    """Task 7, on an H2C (`lan.py`'s docstring): the root, named ``ftp://<name>``."""
    assert (UPLOAD_DIR, file_url("scadbuddy-1-p1.3mf")) == ("/", "ftp://scadbuddy-1-p1.3mf")


def test_pushall_command() -> None:
    assert pushall_command() == {"pushing": {"sequence_id": "0", "command": "pushall"}}


def test_project_file_command_fields() -> None:
    cmd = project_file_command(
        ProjectFile(
            url="file:///sdcard/x.3mf",
            param="Metadata/plate_1.gcode",
            subtask_name="x",
            use_ams=True,
            ams_mapping=[0, -1],
            md5="abc",
        ),
        "7",
    )
    body = cmd["print"]
    assert body["command"] == "project_file" and body["ams_mapping"] == [0, -1]
    assert body["bed_type"] == "auto" and body["project_id"] == "0"
    assert (body["profile_id"], body["task_id"], body["subtask_id"]) == ("0", "0", "0")
    assert body["sequence_id"] == "7" and body["url"] == "file:///sdcard/x.3mf"
    assert body["param"] == "Metadata/plate_1.gcode" and body["md5"] == "abc"
    assert (body["use_ams"], body["flow_cali"], body["timelapse"], body["layer_inspect"]) == (
        True,
        False,
        False,
        True,
    )
    # No rack slot: no `nozzle_mapping` at all, as Bambu Studio sends (Task 8).
    assert "nozzle_mapping" not in body


def test_project_file_command_carries_a_nozzle_mapping() -> None:
    wire = [18, 1, 16] + [-1] * 29
    cmd = project_file_command(
        ProjectFile(
            url="ftp://x.3mf",
            param="Metadata/plate_1.gcode",
            subtask_name="x",
            use_ams=True,
            ams_mapping=[0, 1, 2],
            nozzle_mapping=wire,
            md5="abc",
        ),
        "7",
    )
    assert cmd["print"]["nozzle_mapping"] == wire


def test_merge_report_keeps_unmentioned_keys_and_lists_the_deltas_units() -> None:
    previous = {
        "gcode_state": "RUNNING",
        "mc_percent": 10,
        "ams": {"ams": [{"id": "0", "tray": [{"id": "0"}, {"id": "1"}]}]},
    }
    merged = merge_report(previous, {"mc_percent": 11, "ams": {"ams": [{"id": "1"}]}})
    # The delta lists unit 1 only, so unit 0 is gone: the H2C sends every unit in every
    # report (`test_lan_live.py`'s docstring), so a unit it leaves out is unplugged.
    assert merged == {
        "gcode_state": "RUNNING",
        "mc_percent": 11,
        "ams": {"ams": [{"id": "1"}]},
    }
    assert previous["mc_percent"] == 10
    assert merge_report(None, {"mc_percent": 3}) == {"mc_percent": 3}


#: A loaded RFID tray in the H2C's whole-report shape (`lan-report-h2c-idle.json`), with
#: the tag of spool #39 (#6897).
TAG_UID = "0E9E689F00000100"
TRAY_UUID = "3F7981AB88A54374ADDC4B46DFF3ED7E"
ZERO_UID = "0000000000000000"
ZERO_UUID = "00000000000000000000000000000000"


def _tray(**fields: Any) -> dict[str, Any]:
    return {
        "id": "0",
        "state": 11,
        "tray_type": "PLA",
        "tray_color": "9D432CFF",
        "tray_info_idx": "GFA00",
        "tray_id_name": "A00-N0",
        "remain": 78,
        "tag_uid": TAG_UID,
        "tray_uuid": TRAY_UUID,
        **fields,
    }


def _report(*trays: dict[str, Any], bits: str = "1", **unit: Any) -> dict[str, Any]:
    return {
        "gcode_state": "IDLE",
        "ams": {
            "ams": [{"id": "0", "humidity_raw": "29", "tray": list(trays), **unit}],
            "tray_exist_bits": bits,
        },
    }


def _tray_of(report: dict[str, Any], unit: int = 0, tray: int = 0) -> dict[str, Any]:
    found: dict[str, Any] = report["ams"]["ams"][unit]["tray"][tray]
    return found


@pytest.mark.parametrize(
    "tag",
    [(ZERO_UID, ZERO_UUID), (None, None), ("", "")],
    ids=["zeroed", "absent", "empty"],
)
def test_a_slot_configure_keeps_the_trays_tag(tag: tuple[str | None, str | None]) -> None:
    """#6897: after Bambuddy's ``ams_filament_setting`` on an RFID tray the report shows
    the tray configured (a custom preset, ``remain`` -1, no ``tray_id_name``, as the
    configured trays of `lan-report-h2c-idle.json`) and its tag zeroed or gone. The spool
    is the same, so the tag is kept while the tray stays filled with the same material."""
    before = merge_report(None, _report(_tray()))
    after = _tray(tray_info_idx="OFDSrzZ8", tray_id_name="", remain=-1)
    for key, value in zip(("tag_uid", "tray_uuid"), tag, strict=True):
        if value is None:
            del after[key]
        else:
            after[key] = value
    merged = _tray_of(merge_report(before, _report(after)))
    assert (merged["tag_uid"], merged["tray_uuid"]) == (TAG_UID, TRAY_UUID)
    assert (merged["tray_info_idx"], merged["remain"], merged["tray_id_name"]) == (
        "OFDSrzZ8",
        -1,
        "",
    )


def test_a_new_tag_replaces_the_old_one() -> None:
    before = merge_report(None, _report(_tray()))
    other = ("1122334455667788", "AABBCCDDEEFF00112233445566778899")
    merged = merge_report(before, _report(_tray(tag_uid=other[0], tray_uuid=other[1])))
    assert (_tray_of(merged)["tag_uid"], _tray_of(merged)["tray_uuid"]) == other


@pytest.mark.parametrize("material", ["PETG", "PLA-CF"])
def test_an_untagged_tray_of_another_material_drops_the_tag(material: str) -> None:
    """Another material with no tag is another spool, so the old tag goes."""
    before = merge_report(None, _report(_tray()))
    zeroed = merge_report(
        before, _report(_tray(tray_type=material, tag_uid=ZERO_UID, tray_uuid=ZERO_UUID))
    )
    assert (_tray_of(zeroed)["tag_uid"], _tray_of(zeroed)["tray_uuid"]) == (ZERO_UID, ZERO_UUID)
    absent = _tray(tray_type=material)
    del absent["tag_uid"], absent["tray_uuid"]
    dropped = _tray_of(merge_report(before, _report(absent)))
    assert "tag_uid" not in dropped and "tray_uuid" not in dropped


@pytest.mark.parametrize(
    "bare",
    [{"id": "0", "state": 10}, {"id": "0"}, {"id": "0", "state": 11, "tray_type": ""}],
    ids=["pulled", "id-only", "emptied"],
)
def test_an_emptied_tray_keeps_nothing_of_the_old_spool(bare: dict[str, Any]) -> None:
    """The H2C sends a pulled spool's tray as ``{id, state}`` (`lan-tray-reinsert-h2c.json`
    at 9.8 s): a tray with no material is taken as sent, so no material, colour or tag
    survives it."""
    before = merge_report(None, _report(_tray()))
    merged = merge_report(before, _report(bare, bits="0"))
    assert _tray_of(merged) == bare
    assert merged["ams"]["tray_exist_bits"] == "0"
    # And a tray filled again after it keeps nothing of the spool before the pull.
    again = _tray(tag_uid=ZERO_UID, tray_uuid=ZERO_UUID)
    refilled = _tray_of(merge_report(merged, _report(again)))
    assert (refilled["tag_uid"], refilled["tray_uuid"]) == (ZERO_UID, ZERO_UUID)


def test_a_partial_tray_list_keeps_the_trays_and_fields_it_leaves_out() -> None:
    before = merge_report(None, _report(_tray(), _tray(id="1", tag_uid="1", tray_uuid="2")))
    merged = merge_report(before, {"ams": {"ams": [{"id": "0", "tray": [_tray(remain=50)]}]}})
    trays = merged["ams"]["ams"][0]["tray"]
    assert [(t["id"], t["remain"], t["tag_uid"]) for t in trays] == [
        ("0", 50, TAG_UID),
        ("1", 78, "1"),
    ]
    assert merged["ams"]["tray_exist_bits"] == "1"
    assert merged["ams"]["ams"][0]["humidity_raw"] == "29"


def test_an_ams_block_without_units_keeps_them() -> None:
    before = merge_report(None, _report(_tray()))
    merged = merge_report(before, {"ams": {"tray_now": "0"}})
    assert _tray_of(merged)["tag_uid"] == TAG_UID
    assert merged["ams"]["tray_now"] == "0"


def test_an_ams_ht_unit_merges_by_id_and_the_holders_are_replaced() -> None:
    """An AMS-HT (``ams_id`` 128+) is merged as any unit; the external holders
    (``vt_tray``, ``vir_slot``) are top-level keys, taken as sent."""
    ht = {"id": "128", "tray": [_tray(tray_type="PETG")]}
    holder = {"id": "254", "tray_type": "PLA", "tag_uid": ZERO_UID}
    before = merge_report(None, {"ams": {"ams": [ht]}, "vt_tray": holder, "vir_slot": [holder]})
    configured = _tray(tray_type="PETG", tag_uid=ZERO_UID, tray_uuid=ZERO_UUID, remain=-1)
    empty = {"id": "254", "tray_type": "", "tray_color": "00000000"}
    merged = merge_report(
        before,
        {
            "ams": {"ams": [{"id": "128", "tray": [configured]}]},
            "vt_tray": empty,
            "vir_slot": [empty],
        },
    )
    assert _tray_of(merged)["tag_uid"] == TAG_UID and _tray_of(merged)["remain"] == -1
    assert merged["vt_tray"] == empty and merged["vir_slot"] == [empty]
    pulled = merge_report(
        merged, {"ams": {"ams": [{"id": "128", "tray": [{"id": "0", "state": 9}]}]}}
    )
    assert _tray_of(pulled) == {"id": "0", "state": 9}


def test_merge_report_leaves_previous_alone() -> None:
    before = merge_report(None, _report(_tray()))
    snapshot = copy.deepcopy(before)
    merged = merge_report(before, _report(_tray(remain=10, tag_uid=ZERO_UID), humidity_raw="40"))
    _tray_of(merged)["remain"] = 1
    merge_report(before, _report({"id": "0", "state": 10}))
    assert before == snapshot


REINSERT = Path(__file__).parent / "fixtures" / "lan-tray-reinsert-h2c.json"
IDLE = Path(__file__).parent / "fixtures" / "lan-report-h2c-idle.json"


def test_the_recorded_pull_and_push_back_through_the_merge() -> None:
    """`lan-tray-reinsert-h2c.json` (AMS 1 slot 1 pulled and pushed back on the live H2C),
    each step a whole report over the recorded idle one: the tray is bare from the pull
    until the RFID read, and has its tag back after it."""
    idle = json.loads(IDLE.read_text(encoding="utf-8"))["print"]
    merged = merge_report(None, idle)
    seen: list[tuple[str | None, str | None]] = []
    for step in json.loads(REINSERT.read_text(encoding="utf-8"))["steps"]:
        delta = copy.deepcopy(idle)
        delta["ams"]["tray_exist_bits"] = step["tray_exist_bits"]
        delta["ams"]["ams"][1]["tray"][1] = step["ams_1_tray_1"]
        merged = merge_report(merged, delta)
        tray = _tray_of(merged, 1, 1)
        seen.append((tray.get("tray_type"), tray.get("tag_uid")))
    assert seen[0] == ("PLA", "REDACTED")
    assert seen[1:5] == [(None, None)] * 4
    assert seen[-1] == ("PLA", "REDACTED")


@dataclass
class _Wrapped:
    sock: socket.socket
    handshakes: int = 0

    def do_handshake(self) -> None:
        self.handshakes += 1


@dataclass
class _Recorder:
    wrapped: int = 0

    def wrap_socket(self, sock: socket.socket, **_: Any) -> Any:
        self.wrapped += 1
        return _Wrapped(sock)


def test_implicit_ftp_tls_wraps_socket_once() -> None:
    recorder = _Recorder()
    ftp = ImplicitFtpTls(context=recorder)
    plain, other = socket.socketpair()
    try:
        ftp.sock = plain
        assert ftp.sock == _Wrapped(plain, handshakes=1) and recorder.wrapped == 1
        ftp.sock = None
        assert ftp.sock is None and recorder.wrapped == 1
        already = pinned_context("ab" * 32).wrap_socket(
            socket.socket(), do_handshake_on_connect=False
        )
        ftp.sock = already
        assert ftp.sock is already and recorder.wrapped == 1
        already.close()
    finally:
        plain.close()
        other.close()


def _certificate(tmp_path: Path, name: str) -> tuple[Path, Path, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / f"{name}.pem"
    key_path = tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return (
        cert_path,
        key_path,
        hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest(),
    )


@dataclass
class TlsServer:
    port: int
    sha256: str
    other_sha256: str


@pytest.fixture
def tls_server_pair(tmp_path: Path) -> Iterator[TlsServer]:
    """A local TLS server presenting certificate A, and certificate B's fingerprint."""
    cert, key, sha_a = _certificate(tmp_path, "a")
    _, _, sha_b = _certificate(tmp_path, "b")
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(5)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                with server_context.wrap_socket(conn, server_side=True) as tls:
                    tls.sendall(b"hello")
            except (OSError, ssl.SSLError):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield TlsServer(listener.getsockname()[1], sha_a, sha_b)
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=5)


def _connect(port: int, context: ssl.SSLContext) -> bytes:
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        context.wrap_socket(raw) as tls,
    ):
        return tls.recv(5)


def test_pinned_context_refuses_other_cert(tls_server_pair: TlsServer) -> None:
    with pytest.raises(CertificateMismatchError):
        _connect(tls_server_pair.port, pinned_context(tls_server_pair.other_sha256))


def test_pinned_context_accepts_its_cert(tls_server_pair: TlsServer) -> None:
    assert _connect(tls_server_pair.port, pinned_context(tls_server_pair.sha256)) == b"hello"


def test_fetch_cert_sha256_reads_the_leaf(tls_server_pair: TlsServer) -> None:
    assert fetch_cert_sha256("127.0.0.1", tls_server_pair.port) == tls_server_pair.sha256


def test_a_pin_is_hex_sha256() -> None:
    with pytest.raises(ValueError):
        pinned_context("nope")


class _Message:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


class FakeMqtt:
    """``aiomqtt.Client``'s surface `LanPrinter` uses, over a list of payloads."""

    instances: ClassVar[list[FakeMqtt]] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host, self.port, self.kwargs = host, port, kwargs
        self.subscribed: list[str] = []
        self.published: list[tuple[str, Any]] = []
        FakeMqtt.instances.append(self)

    async def __aenter__(self) -> FakeMqtt:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    async def publish(self, topic: str, payload: Any) -> None:
        self.published.append((topic, payload))

    @property
    def messages(self) -> Any:
        async def stream() -> Any:
            for payload in PAYLOADS:
                yield _Message(payload)

        return stream()


PAYLOADS: list[bytes] = []


@pytest.fixture
def fake_mqtt(monkeypatch: pytest.MonkeyPatch) -> type[FakeMqtt]:
    FakeMqtt.instances = []
    monkeypatch.setattr(aiomqtt, "Client", FakeMqtt)
    return FakeMqtt


async def test_reports_merge_and_skip_what_is_not_a_print_report(
    fake_mqtt: type[FakeMqtt],
) -> None:
    PAYLOADS[:] = [
        b'{"print": {"gcode_state": "RUNNING", "mc_percent": 1}}',
        b"not json",
        b'{"info": {"command": "get_version"}}',
        b'{"print": {"mc_percent": 2}}',
    ]
    printer = LanPrinter("10.0.0.9", "01P00A000000001", "12345678", "ab" * 32)
    reports = [report async for report in printer.reports()]
    assert [r.print for r in reports] == [
        {"gcode_state": "RUNNING", "mc_percent": 1},
        {"gcode_state": "RUNNING", "mc_percent": 2},
    ]
    [client] = fake_mqtt.instances
    assert (client.host, client.port) == ("10.0.0.9", 8883)
    assert client.kwargs["username"] == "bblp" and client.kwargs["password"] == "12345678"
    # #4186 review I3: paho proves the link itself, well inside the 90 s offline window.
    assert client.kwargs["keepalive"] == 10
    # paho's DEBUG lines name the SUBSCRIBE topic, so its logger stays at WARNING.
    assert client.kwargs["logger"].getEffectiveLevel() >= logging.WARNING
    assert isinstance(client.kwargs["tls_context"], ssl.SSLContext)
    assert client.subscribed == ["device/01P00A000000001/report"]
    assert client.published == [
        (
            "device/01P00A000000001/request",
            '{"pushing": {"sequence_id": "0", "command": "pushall"}}',
        )
    ]
    assert "12345678" not in repr(printer) and "12345678" not in repr(reports[0])


async def test_probe_returns_the_first_report(fake_mqtt: type[FakeMqtt]) -> None:
    PAYLOADS[:] = [b'{"print": {"gcode_state": "IDLE"}}', b'{"print": {"x": 1}}']
    printer = LanPrinter("10.0.0.9", "01P00A000000001", "12345678", "ab" * 32)
    assert (await printer.probe()).print == {"gcode_state": "IDLE"}


async def test_probe_without_a_report_fails(fake_mqtt: type[FakeMqtt]) -> None:
    PAYLOADS[:] = []
    printer = LanPrinter("10.0.0.9", "01P00A000000001", "12345678", "ab" * 32)
    with pytest.raises(ConnectionError):
        await printer.probe()


async def test_connected_is_set_once_subscribed(fake_mqtt: type[FakeMqtt]) -> None:
    """#4186 review I3: the watch takes ``connected`` as proof the printer is there."""
    PAYLOADS[:] = []
    printer = LanPrinter("10.0.0.9", "01P00A000000001", "12345678", "ab" * 32)
    seen: list[bool] = []
    connected = asyncio.Event()
    async for _ in printer.reports(connected=connected):
        pass
    seen.append(connected.is_set())
    assert seen == [True]
