"""`LanPrinter.upload` over implicit FTPS (#3823 Task 4; the fake-printer agent's finding
on PR #4177): a data connection closed without a TLS ``unwrap`` under TLS 1.3 is cut
short (200,000 bytes sent, 98,304 arrived). The upload unwraps, and its context stops
at TLS 1.2, as a printer's FTPS does (Bambuddy
``backend/app/services/virtual_printer/ftp_server.py:702-713`` at maziggy/bambuddy
``0d427510``, read 2026-10-18).

The server here is the test's own: stdlib sockets, TLS 1.3 allowed, implicit TLS on the
control connection, passive data connections, ``STOR`` only."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scadbuddy.printers import lan as lan_module
from scadbuddy.printers.lan import LanPrinter
from tests.printers.test_lan import _certificate

CODE = "12345678"


@dataclass
class Received:
    files: dict[str, bytes] = field(default_factory=dict)
    versions: list[str] = field(default_factory=list)
    errors: list[BaseException] = field(default_factory=list)


class FtpsServer:
    """``close`` is how the data connection ends once the client's bytes are in:
    ``clean`` answers the client's ``close_notify``; ``tcp`` drops the connection with
    none; ``silent`` keeps it open and never answers (#4186 review I5)."""

    def __init__(self, context: ssl.SSLContext, close: str = "clean") -> None:
        self.context = context
        self.close_mode = close
        self.held: list[ssl.SSLSocket] = []
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.received = Received()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            raw, _ = self.listener.accept()
        except OSError:
            return
        try:
            with self.context.wrap_socket(raw, server_side=True) as tls:
                self.received.versions.append(str(tls.version()))
                self._session(tls)
        except BaseException as error:
            self.received.errors.append(error)

    def _session(self, tls: ssl.SSLSocket) -> None:
        reader = tls.makefile("rb")

        def reply(line: str) -> None:
            tls.sendall(f"{line}\r\n".encode())

        reply("220 fake printer")
        data_listener: socket.socket | None = None
        while line := reader.readline():
            command, _, argument = line.decode().strip().partition(" ")
            match command.upper():
                case "USER":
                    reply("331 password")
                case "PASS":
                    reply("230 in" if argument == CODE else "530 no")
                case "PBSZ" | "TYPE":
                    reply("200 ok")
                case "PROT":
                    reply("200 private")
                case "PASV":
                    data_listener = socket.create_server(("127.0.0.1", 0))
                    port = data_listener.getsockname()[1]
                    reply(f"227 passive (127,0,0,1,{port >> 8},{port & 0xFF})")
                case "STOR":
                    assert data_listener is not None
                    reply("150 send")
                    conn, _ = data_listener.accept()
                    data_listener.close()
                    data = self.context.wrap_socket(conn, server_side=True)
                    self.received.versions.append(str(data.version()))
                    chunks = []
                    while chunk := data.recv(65536):
                        chunks.append(chunk)
                    self.received.files[argument] = b"".join(chunks)
                    if self.close_mode == "clean":
                        with suppress(OSError):  # its close_notify, if the client sent one
                            data.unwrap()
                        data.close()
                    elif self.close_mode == "tcp":
                        data.close()  # no close_notify
                    else:
                        self.held.append(data)  # never answered, never closed
                    reply("226 done")
                case "QUIT":
                    reply("221 bye")
                    for held in self.held:
                        held.close()
                    return
                case _:
                    reply("502 no")

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=10)


def _serving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close: str) -> tuple[FtpsServer, str]:
    cert, key, sha = _certificate(tmp_path, "printer")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    context.maximum_version = ssl.TLSVersion.TLSv1_3  # what the client must not rely on
    server = FtpsServer(context, close)
    monkeypatch.setattr(lan_module, "FTPS_PORT", server.port)
    monkeypatch.setattr(lan_module, "UNWRAP_TIMEOUT", 0.5)

    async def loopback(host: str) -> str:  # the test's server is on loopback
        return host

    monkeypatch.setattr(lan_module, "lan_address", loopback)
    return server, sha


@pytest.fixture
def ftps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FtpsServer, str]]:
    server, sha = _serving(tmp_path, monkeypatch, "clean")
    try:
        yield server, sha
    finally:
        server.close()


async def test_an_upload_over_128_kib_arrives_whole(ftps: tuple[FtpsServer, str]) -> None:
    server, sha = ftps
    body = os.urandom(200_000)
    printer = LanPrinter("127.0.0.1", "01P00A000000001", CODE, sha)

    md5 = await printer.upload(io.BytesIO(body), "scadbuddy-probe.3mf")

    server.thread.join(timeout=10)
    assert not server.received.errors
    arrived = server.received.files["scadbuddy-probe.3mf"]
    assert len(arrived) == len(body)
    assert md5 == hashlib.md5(body, usedforsecurity=False).hexdigest()
    assert hashlib.md5(arrived, usedforsecurity=False).hexdigest() == md5
    # Control and data both at 1.2, though the server would take 1.3.
    assert server.received.versions == ["TLSv1.2", "TLSv1.2"]


async def test_an_upload_to_another_certificate_is_refused(
    ftps: tuple[FtpsServer, str],
) -> None:
    server, _ = ftps
    printer = LanPrinter("127.0.0.1", "01P00A000000001", CODE, "ef" * 32)
    with pytest.raises(lan_module.CertificateMismatchError):
        await printer.upload(io.BytesIO(b"x"), "x.3mf")
    assert server.received.files == {}


@pytest.mark.parametrize("close", ["tcp", "silent"])
async def test_a_printer_that_never_answers_close_notify_still_took_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, close: str
) -> None:
    """#4186 review I5: the file is whole once the 226 arrives, however the printer ends
    the data connection; a close that times out or hits EOF is logged, not a failure."""
    server, sha = _serving(tmp_path, monkeypatch, close)
    body = os.urandom(150_000)
    printer = LanPrinter("127.0.0.1", "01P00A000000001", CODE, sha)
    try:
        with caplog.at_level(logging.INFO, logger="scadbuddy.printers.lan"):
            md5 = await printer.upload(io.BytesIO(body), "scadbuddy-probe.3mf")
    finally:
        server.close()
    assert not server.received.errors
    assert server.received.files["scadbuddy-probe.3mf"] == body
    assert md5 == hashlib.md5(body, usedforsecurity=False).hexdigest()
    assert "close_notify" in caplog.text
