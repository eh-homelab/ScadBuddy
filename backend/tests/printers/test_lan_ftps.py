"""`LanPrinter.upload` over implicit FTPS (#3823 Task 4; the fake-printer agent's finding
on PR #4177): a data connection closed without a TLS ``unwrap`` under TLS 1.3 is cut
short (200,000 bytes sent, 98,304 arrived). The upload unwraps, and its context stops
at TLS 1.2, as a printer's FTPS does (Bambuddy
``backend/app/services/virtual_printer/ftp_server.py:702-713`` at maziggy/bambuddy
``0d427510``, read 2026-10-18).

The server here is the test's own: stdlib sockets, TLS 1.3 allowed, implicit TLS on the
control connection, passive data connections, ``STOR``, and a ``RETR`` that never
sends (``stall``)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scadbuddy.printers import lan as lan_module
from scadbuddy.printers.lan import FileTooLargeError, LanPrinter
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
    none; ``silent`` keeps it open and never answers (#4186 review I5).

    ``stall`` is where a session stops answering: ``handshake`` takes the TCP connection
    and never answers the ``ClientHello`` (#6055); ``login`` never answers ``PASS``;
    ``retr`` opens a ``RETR``'s data connection and never sends on it. ``stalled`` is set
    there, and ``ended`` once the client has closed the control connection (#6049)."""

    def __init__(
        self, context: ssl.SSLContext, close: str = "clean", stall: str | None = None
    ) -> None:
        self.context = context
        self.close_mode = close
        self.stall = stall
        self.stalled = threading.Event()
        self.ended = threading.Event()
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
        if self.stall == "handshake":
            self._never_answer(raw)
            return
        try:
            with self.context.wrap_socket(raw, server_side=True) as tls:
                self.received.versions.append(str(tls.version()))
                self._session(tls)
        except BaseException as error:
            self.received.errors.append(error)
        finally:
            self.ended.set()

    def _never_answer(self, raw: socket.socket) -> None:
        """Read the ``ClientHello`` and never answer it. ``ended`` is set only when the
        client closes; a timeout or a reset is an error."""
        with raw:
            raw.settimeout(5)
            try:
                if raw.recv(65536):
                    self.stalled.set()
                while raw.recv(65536):
                    pass
            except OSError as error:
                self.received.errors.append(error)
                return
            self.ended.set()

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
                    if self.stall == "login":
                        self.stalled.set()
                        continue  # never answered
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
                case "RETR" if self.stall == "retr":
                    assert data_listener is not None
                    reply("150 sending")
                    conn, _ = data_listener.accept()
                    data_listener.close()
                    self.held.append(self.context.wrap_socket(conn, server_side=True))
                    self.stalled.set()  # and never a byte on it
                case "QUIT":
                    reply("221 bye")
                    for held in self.held:
                        held.close()
                    return
                case _:
                    reply("502 no")
        for held in self.held:
            held.close()

    def close(self) -> None:
        self.listener.close()
        self.thread.join(timeout=10)


def _serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close: str, stall: str | None = None
) -> tuple[FtpsServer, str]:
    cert, key, sha = _certificate(tmp_path, "printer")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    context.maximum_version = ssl.TLSVersion.TLSv1_3  # what the client must not rely on
    server = FtpsServer(context, close, stall)
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


async def test_a_transfer_lets_go_of_its_data_connection_when_it_ends(
    ftps: tuple[FtpsServer, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6055: after the ``226`` the session holds no data connection, so a later `cut`
    touches only live sockets."""
    server, sha = ftps
    printer = LanPrinter("127.0.0.1", "01P00A000000001", CODE, sha)
    sessions: list[lan_module.ImplicitFtpTls] = []
    held_at_close: list[socket.socket | None] = []
    make = printer._session

    def session() -> lan_module.ImplicitFtpTls:
        ftp = make()
        close = ftp.close

        def closing() -> None:
            held_at_close.append(ftp._data)
            close()

        monkeypatch.setattr(ftp, "close", closing)
        sessions.append(ftp)
        return ftp

    monkeypatch.setattr(printer, "_session", session)
    await printer.upload(io.BytesIO(b"x" * 1000), "scadbuddy-probe.3mf")

    assert server.received.files["scadbuddy-probe.3mf"] == b"x" * 1000
    assert len(sessions) == 1
    assert held_at_close == [None]  # let go at the 226, before the close
    assert sessions[0]._data is None


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


class _Retr:
    """An FTPS session whose ``RETR`` sends ten blocks of 1,000 bytes."""

    def __init__(self) -> None:
        self.closed = False
        self.is_cut = False

    def retrbinary(self, command: str, callback: Callable[[bytes], object]) -> str:
        for _ in range(10):
            callback(b"x" * 1000)
        return "226 done"

    def close(self) -> None:
        self.closed = True


def test_a_download_stops_past_its_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """#4699 phase 4: the print watcher reads a job's file within the library download
    cap, never a file of any size into memory."""
    printer = LanPrinter("10.0.0.9", "01P00A000000001", CODE, "ab" * 32)
    session = _Retr()
    monkeypatch.setattr(printer, "_ftps", lambda address, ftp=None: session)
    with pytest.raises(FileTooLargeError):
        printer._download("10.0.0.9", "/cache/x.3mf", 2500)
    assert session.closed
    assert printer._download("10.0.0.9", "/cache/x.3mf", 10_000) == b"x" * 10_000


class _Endless:
    """An FTPS session whose ``RETR`` sends a 1-byte block every 10 ms, for about 5 s."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.closed = threading.Event()
        self.blocks = 0
        self.is_cut = False

    def cut(self) -> None:
        self.is_cut = True  # it ignores the cut sockets, as a printer's late block would

    def retrbinary(self, command: str, callback: Callable[[bytes], object]) -> str:
        self.started.set()
        for _ in range(500):
            callback(b"x")
            self.blocks += 1
            time.sleep(0.01)
        return "226 done"

    def close(self) -> None:
        self.closed.set()


async def test_a_cancelled_download_closes_its_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#6045: a caller's bound that runs out (the print watcher's ``CAPTURE_TIMEOUT``)
    stops the FTPS thread at its next block, not when the file ends."""
    printer = LanPrinter("10.0.0.9", "01P00A000000001", CODE, "ab" * 32)
    session = _Endless()
    monkeypatch.setattr(printer, "_session", lambda: session)
    monkeypatch.setattr(printer, "_ftps", lambda address, ftp=None: ftp)

    async def address(host: str) -> str:
        return host

    monkeypatch.setattr(lan_module, "lan_address", address)
    task = asyncio.create_task(printer.download("/cache/x.3mf"))
    assert await asyncio.to_thread(session.started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(session.closed.wait, 2)
    assert session.blocks < 500


@pytest.mark.parametrize("stall", ["handshake", "login", "retr"])
async def test_a_cancel_ends_a_stalled_download_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stall: str
) -> None:
    """#6049: a printer that stops answering mid-login or mid-``RETR`` does not hold the
    thread and its sockets for `FTP_TIMEOUT` after a cancel: the cancel cuts the
    session, and the thread ends and closes the connection at once. ``handshake``
    (#6055) is a printer that takes the TCP connection and never answers the
    ``ClientHello``: the path ``do_handshake_on_connect=False`` is for."""
    assert lan_module.FTP_TIMEOUT >= 30  # the bound the cancel must not wait for
    server, sha = _serving(tmp_path, monkeypatch, "clean", stall)
    printer = LanPrinter("127.0.0.1", "01P00A000000001", CODE, sha)
    ended = threading.Event()
    download = printer._download

    def spy(*args: object) -> bytes:
        try:
            return download(*args)  # type: ignore[arg-type]
        finally:
            ended.set()

    monkeypatch.setattr(printer, "_download", spy)
    try:
        task = asyncio.create_task(printer.download("/cache/x.3mf"))
        assert await asyncio.to_thread(server.stalled.wait, 5)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(ended.wait, 5)
        assert await asyncio.to_thread(server.ended.wait, 5)
        assert time.monotonic() - started < 5
    finally:
        server.close()
    assert not server.thread.is_alive()
    if stall == "handshake":
        assert not server.received.errors
