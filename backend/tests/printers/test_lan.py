"""The LAN client's pieces that need no printer (#3823, plan 2026-10-16, Task 4): the
command shapes (OpenBambuAPI `mqtt.md`), the report merge, the implicit-FTPS socket
wrapping and the certificate pin."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
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
    CertificateMismatchError,
    ImplicitFtpTls,
    LanPrinter,
    fetch_cert_sha256,
    merge_report,
    pinned_context,
    project_file_command,
    pushall_command,
)
from scadbuddy.printers.lan_models import ProjectFile


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


def test_merge_report_keeps_unmentioned_keys_and_replaces_ams() -> None:
    previous = {
        "gcode_state": "RUNNING",
        "mc_percent": 10,
        "ams": {"ams": [{"id": "0", "tray": [{"id": "0"}, {"id": "1"}]}]},
    }
    merged = merge_report(previous, {"mc_percent": 11, "ams": {"ams": [{"id": "1"}]}})
    assert merged == {
        "gcode_state": "RUNNING",
        "mc_percent": 11,
        "ams": {"ams": [{"id": "1"}]},
    }
    assert previous["mc_percent"] == 10
    assert merge_report(None, {"mc_percent": 3}) == {"mc_percent": 3}


@dataclass
class _Recorder:
    wrapped: int = 0

    def wrap_socket(self, sock: socket.socket, **_: Any) -> Any:
        self.wrapped += 1
        return ("wrapped", sock)


def test_implicit_ftp_tls_wraps_socket_once() -> None:
    recorder = _Recorder()
    ftp = ImplicitFtpTls(context=recorder)
    plain, other = socket.socketpair()
    try:
        ftp.sock = plain
        assert ftp.sock == ("wrapped", plain) and recorder.wrapped == 1
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
