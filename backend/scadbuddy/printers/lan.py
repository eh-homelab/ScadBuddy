"""ScadBuddy's own LAN connection to a Bambu printer (#3823, plan 2026-10-16, Task 4).

The protocol facts, each cited:

- MQTT over TLS on ``<host>:8883``, user ``bblp``, password the LAN access code; the
  printer publishes on ``device/<serial>/report`` and takes commands on
  ``device/<serial>/request``; ``pushing.pushall`` asks for the whole state, and the P1
  series otherwise reports only what changed; ``print.project_file`` starts a 3MF
  already on the printer (OpenBambuAPI `mqtt.md`,
  https://github.com/Doridian/OpenBambuAPI/blob/main/mqtt.md, read 2026-10-18).
- The file goes over implicit FTPS on ``<host>:990``, user ``bblp`` (OpenBambuAPI
  `ftp.md`, same date). ha-bambulab's ``ImplicitFTP_TLS`` wraps the control socket at
  connect and wraps each data connection reusing the control connection's TLS session
  (https://github.com/greghesp/ha-bambulab, ``pybambu/bambu_client.py``, read
  2026-10-18). Unlike ha-bambulab, the data connection is unwrapped before it closes,
  and the FTPS context stops at TLS 1.2: a printer serves its FTPS at 1.2 only
  (Bambuddy ``backend/app/services/virtual_printer/ftp_server.py:702-713`` at
  maziggy/bambuddy ``0d427510``, read 2026-10-18), and a data connection closed without
  ``close_notify`` under 1.3 is cut short (measured on PR #4177's fake printer and in
  ``tests/printers/test_lan_ftps.py``: 200,000 bytes sent, 98,304 arrived).

Which directory a 3MF goes in and the ``url`` the printer then accepts are not
documented there; Task 7 measured them (`UPLOAD_DIR`, `file_url`) on an H2C, firmware
01.02.00.00, on 2026-10-24: a 3MF stored at ``/<name>`` and named ``ftp://<name>`` in
`project_file_command`, with the four ids ``"0"`` and no ``ams_mapping2``, was answered
``result: SUCCESS`` and ``RUNNING`` 1.8 s later (`tests/printers/test_lan_live.py`).
Bambuddy does the same (``print_scheduler.py`` "Upload to root directory (not
/cache/)", ``bambu_mqtt.py`` ``start_print``: ``"url": f"ftp://{filename}"``, read in
the deployed image 2026-10-24).

The printer's certificate is self-signed and names no host, so verification is by pin,
never off: `fetch_cert_sha256` reads the leaf certificate's SHA-256 once, when the user
types the access code, and every later connection checks the presented certificate
against it right after the handshake (`pinned_context`). Every connection dials the
address `address.lan_address` checks at that moment (a private LAN address). Nothing here connects
unpinned except `fetch_cert_sha256`, which sends nothing. No log record, exception or
``repr`` here carries the access code.
"""

from __future__ import annotations

import asyncio
import ftplib
import hashlib
import hmac
import json
import logging
import re
import socket
import ssl
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, BinaryIO

import aiomqtt

from scadbuddy.printers.address import lan_address
from scadbuddy.printers.lan_models import LanReport, ProjectFile

MQTT_PORT = 8883
FTPS_PORT = 990
USERNAME = "bblp"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
#: How long an FTPS command or block may wait.
FTP_TIMEOUT = 30.0
FTP_BLOCK = 64 * 1024
#: Where a 3MF is stored for ``print.project_file`` (Task 7, measured; module docstring).
UPLOAD_DIR = "/"
#: How long an upload waits for the printer's answer to its ``close_notify``. Some
#: printers never send one (ha-bambulab skips the unwrap for that reason); the ``226``
#: reply, not the close, says the file arrived.
UNWRAP_TIMEOUT = 5.0
#: MQTT keepalive: paho sends a PINGREQ after this long with no traffic and closes the
#: link at its next check without a PINGRESP, so a live link has had inbound traffic
#: within two keepalives (paho 2.1 ``_check_keepalive``). The watch stamps its touch
#: with that bound (`watch.LINK_PROOF`).
KEEPALIVE = 10

logger = logging.getLogger(__name__)
#: paho's own logger, for aiomqtt: at DEBUG it logs the SUBSCRIBE topic, which names the
#: serial, so it never goes below WARNING whatever the process's level.
_MQTT_LOGGER = logging.getLogger("scadbuddy.printers.lan.mqtt")
_MQTT_LOGGER.setLevel(logging.WARNING)


class CertificateMismatchError(ssl.SSLError):
    """The printer presented a certificate other than the one pinned when it was linked
    (a firmware reset, or another device at that address). Re-link it to pin again."""

    def __init__(self) -> None:
        super().__init__(
            "the printer's certificate is not the one pinned when it was linked;"
            " link it again to trust the new one"
        )


def cert_sha256(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def fetch_cert_sha256(host: str, port: int = MQTT_PORT, timeout: float = 5.0) -> str:
    """The SHA-256 (hex) of the DER leaf certificate ``host:port`` presents, read with
    verification off for this one read: what the LAN link pins."""
    pem = ssl.get_server_certificate((host, port), timeout=timeout)
    return cert_sha256(ssl.PEM_cert_to_DER_cert(pem))


def _pinned_socket_class(expected: str) -> type[ssl.SSLSocket]:
    class PinnedSocket(ssl.SSLSocket):
        def do_handshake(self, block: bool = False) -> None:
            super().do_handshake(block)
            der = self.getpeercert(binary_form=True)
            if der is None or not hmac.compare_digest(cert_sha256(der), expected):
                self.close()
                raise CertificateMismatchError()

    return PinnedSocket


def pinned_context(cert_sha256_hex: str) -> ssl.SSLContext:
    """A client context that trusts exactly the certificate whose SHA-256 is given.

    The TLS layer verifies nothing (``CERT_NONE``, no hostname check: the certificate is
    self-signed and names no host); the socket class it makes checks the fingerprint as
    soon as each handshake completes, and a mismatch closes the connection and raises
    `CertificateMismatchError` before anything is sent. paho and ftplib both wrap
    through the context, so the MQTT connection and every FTPS connection are pinned."""
    expected = cert_sha256_hex.lower()
    if not _SHA256.match(expected):
        raise ValueError("a certificate pin is a SHA-256 in hex")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.sslsocket_class = _pinned_socket_class(expected)
    return context


def ftps_context(cert_sha256_hex: str) -> ssl.SSLContext:
    """`pinned_context`, at most TLS 1.2: a printer's FTPS is 1.2 only (Bambuddy
    ``virtual_printer/ftp_server.py:702-713`` at ``0d427510``), and under 1.3 a data
    connection's unread session tickets cut an upload short."""
    context = pinned_context(cert_sha256_hex)
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


def file_url(remote_name: str) -> str:
    """The ``url`` ``print.project_file`` takes for a file in `UPLOAD_DIR` (Task 7)."""
    return f"ftp://{remote_name}"


def pushall_command() -> dict[str, Any]:
    return {"pushing": {"sequence_id": "0", "command": "pushall"}}


def project_file_command(file: ProjectFile, sequence_id: str) -> dict[str, Any]:
    """``print.project_file``; the four ids are ``"0"`` for a local print (`mqtt.md`)."""
    return {
        "print": {
            "command": "project_file",
            "sequence_id": sequence_id,
            "url": file.url,
            "param": file.param,
            "subtask_name": file.subtask_name,
            "use_ams": file.use_ams,
            "ams_mapping": list(file.ams_mapping),
            "bed_type": file.bed_type,
            "flow_cali": file.flow_cali,
            "timelapse": file.timelapse,
            "layer_inspect": file.layer_inspect,
            "md5": file.md5,
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
        }
    }


def merge_report(previous: dict[str, Any] | None, delta: dict[str, Any]) -> dict[str, Any]:
    """``delta``'s ``print`` keys over ``previous``'s, a new dict: a shallow merge, so a
    key a delta leaves out keeps its last value, and ``ams`` (a nested object the
    printer re-sends whole) is replaced whole when present."""
    return {**(previous or {}), **delta}


class ImplicitFtpTls(ftplib.FTP_TLS):
    """FTPS with TLS from the first byte (port 990), as ha-bambulab's
    ``ImplicitFTP_TLS``: the control socket is wrapped in the (pinned) context as soon
    as it is set, and each data connection reuses its TLS session."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._sock: Any = None
        super().__init__(*args, **kwargs)

    @property
    def sock(self) -> Any:
        return self._sock

    @sock.setter
    def sock(self, value: Any) -> None:
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(
        self, cmd: str, rest: int | str | None = None
    ) -> tuple[socket.socket, int | None]:
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:  # type: ignore[attr-defined]
            session = self.sock.session if isinstance(self.sock, ssl.SSLSocket) else None
            conn = self.context.wrap_socket(conn, session=session)
        return conn, size


class LanPrinter:
    """One printer over the LAN. Each call opens its own connection; reconnecting after
    a dropped one is the caller's (the watch's activity retries)."""

    def __init__(self, host: str, serial: str, access_code: str, cert_sha256: str) -> None:
        self.host = host
        self.serial = serial
        self._access_code = access_code
        self.cert_sha256 = cert_sha256

    def __repr__(self) -> str:
        return f"LanPrinter(host={self.host!r})"

    @property
    def report_topic(self) -> str:
        return f"device/{self.serial}/report"

    @property
    def request_topic(self) -> str:
        return f"device/{self.serial}/request"

    async def _client(self) -> aiomqtt.Client:
        """A client for the address `lan_address` checks now: every connect is checked
        again, and dials that address, never the name."""
        return aiomqtt.Client(
            await lan_address(self.host),
            MQTT_PORT,
            username=USERNAME,
            password=self._access_code,
            tls_context=pinned_context(self.cert_sha256),
            tls_insecure=True,  # the pin is the check; the certificate names no host
            timeout=10.0,
            keepalive=KEEPALIVE,
            logger=_MQTT_LOGGER,
        )

    async def probe(self, timeout: float = 10.0) -> LanReport:
        """Connect, ask for the whole state, and return the first report."""
        async with asyncio.timeout(timeout):
            async for report in self.reports():
                return report
        raise ConnectionError("the printer closed the connection before it reported")

    async def reports(self, connected: asyncio.Event | None = None) -> AsyncIterator[LanReport]:
        """Each report, merged over the ones before, for as long as the connection
        lasts. A message that is not a JSON object with a ``print`` object is skipped.
        ``connected`` is set once the printer has taken the login and the subscription:
        the evidence it is there, before it has said anything."""
        merged: dict[str, Any] | None = None
        async with await self._client() as client:
            await client.subscribe(self.report_topic)
            if connected is not None:
                connected.set()
            await client.publish(self.request_topic, json.dumps(pushall_command()))
            async for message in client.messages:
                delta = _print_of(message.payload)
                if delta is None:
                    continue
                merged = merge_report(merged, delta)
                yield LanReport(serial=self.serial, received_at=datetime.now(UTC), print=merged)

    async def publish(self, command: dict[str, Any]) -> None:
        async with await self._client() as client:
            await client.publish(self.request_topic, json.dumps(command))

    async def upload(self, data: BinaryIO, remote_name: str) -> str:
        """Send ``data`` over implicit FTPS as ``remote_name`` (a path on the printer;
        in `UPLOAD_DIR`, Task 7) and return the md5 (hex) of what was
        sent, which ``print.project_file`` names."""
        address = await lan_address(self.host)
        return await asyncio.to_thread(self._upload, address, data, remote_name)

    def _upload(self, address: str, data: BinaryIO, remote_name: str) -> str:
        md5 = hashlib.md5(usedforsecurity=False)
        ftp = ImplicitFtpTls(context=ftps_context(self.cert_sha256), timeout=FTP_TIMEOUT)
        try:
            ftp.connect(address, FTPS_PORT)
            ftp.login(USERNAME, self._access_code)
            ftp.prot_p()
            ftp.voidcmd("TYPE I")
            conn = ftp.transfercmd(f"STOR {remote_name}")
            try:
                while block := data.read(FTP_BLOCK):
                    conn.sendall(block)
                    md5.update(block)
                _close_data(conn)
            finally:
                conn.close()
            ftp.voidresp()  # the 226: the printer has the whole file
        finally:
            ftp.close()
        return md5.hexdigest()


def _close_data(conn: socket.socket) -> None:
    """End the data connection's TLS cleanly, so the printer reads every byte and not
    a truncation (the >128 KiB fix). A printer that never answers the ``close_notify``,
    or drops TCP instead, is logged, not a failure: `UNWRAP_TIMEOUT` bounds the wait,
    and the ``226`` that follows decides (#4186 review I5)."""
    if not isinstance(conn, ssl.SSLSocket):
        return
    conn.settimeout(UNWRAP_TIMEOUT)
    try:
        conn.unwrap()
    except (OSError, ssl.SSLError) as error:
        logger.info(
            "the printer did not answer the upload's close_notify; its 226 decides",
            extra={"error": type(error).__name__},
        )


def _print_of(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, (bytes, bytearray, str)):
        return None
    try:
        body = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    report = body.get("print")
    return report if isinstance(report, dict) else None
