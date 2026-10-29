"""A fake Bambu printer on the LAN, for the hot-path stack (#3818 tier A, Task 12).

It is what a LAN client (3a's ``LanPrinter``) connects to, and it records what it is sent:

- **MQTT over TLS** on ``FAKE_PRINTER_MQTT_PORT`` (8883), user ``bblp``, password the
  access code. The printer *is* the broker a LAN client connects to (OpenBambuAPI
  ``mqtt.md`` at ``cc383a2c``; spec 2026-10-16 §2), so this starts a mosquitto 2 with a
  TLS listener, a self-signed certificate made at start and a password file holding
  ``bblp`` only, then connects to it as the printer: it subscribes
  ``device/<serial>/request`` and publishes on ``device/<serial>/report``.
  ``pushing.pushall`` is answered with the whole report (``report.json``, in the shape of
  ``mqtt.md``'s ``pushing.pushall`` example, one AMS tray tagged); every report is the
  whole object, as the X1 series sends (``mqtt.md``, ``print.push_status``).
  ``print.project_file`` is recorded and echoed on the report topic with its ``url``
  and a ``result``, as a printer echoes a dispatch there ~2 s before ``PREPARE``
  (Bambuddy ``bambu_mqtt.py:2067-2101`` at ``0d427510``, which reads only a
  ``result`` of ``SUCCESS``). Its ``url`` must name a file uploaded under the upload
  directory (``resolve_url``); then it moves the file to ``/cache/<name>.gcode.3mf``,
  as an H2C does once it starts a print (``/<name>`` then names nothing), and runs
  ``gcode_state`` ``PREPARE``, ``RUNNING``
  (``mc_percent`` 0 to 100 over ``FAKE_PRINT_SECONDS``) and ``FINISH``. Otherwise the echo
  says ``result: FAIL`` with a reason, the print goes ``FAILED`` with a non-zero
  ``print_error`` and the refusal is listed. The failure's values are made up: what a
  printer answers for a missing file is not measured. ``print.stop`` ends a print
  ``FAILED``. An ACL lets ``bblp`` use only ``device/<serial>/request`` and
  ``device/<serial>/report``: mosquitto 2 grants a ``#`` subscription but delivers it
  nothing else, and drops a publish to any other topic.
- **Implicit FTPS** on ``FAKE_PRINTER_FTPS_PORT`` (990), user ``bblp``, with the same
  certificate (OpenBambuAPI ``ftp.md``: "TLS: implicit"). pyftpdlib speaks explicit TLS
  (``AUTH TLS``); its handler is made implicit by securing the control connection as it
  is accepted, before the ``220`` banner, which then goes over TLS (the same switch
  pyftpdlib's ``ftp_AUTH`` makes after its ``234``). This is how ha-bambulab's client
  expects it: its ``ImplicitFTP_TLS`` wraps the socket at connect
  (``pybambu/bambu_client.py`` at ``0e027ff1``). TLS 1.2 only, as a printer's
  (``ImplicitTlsHandler.get_ssl_context`` says why it matters). Uploads land under
  ``FAKE_PRINTER_SDCARD`` (``/sdcard``), each recorded with its size and md5.
- **Control** on ``FAKE_PRINTER_CONTROL_PORT`` (8081), plain HTTP, never a printer path:
  ``GET /__received`` answers ``{"mqtt": [<command>...], "ftps": [{"name", "size",
  "md5"}...], "refused": [{"url", "reason"}...]}`` and ``POST /__reset`` clears it. It
  answers once the printer is up; ``python printer.py --health`` checks that.

The certificate is made at the first start and kept in ``FAKE_PRINTER_STATE``, as a
printer's is stable: a LAN client pins its SHA-256 when it is linked (3a's
``LanPrinter``), so a new one at each restart would break the pin.

The upload directory, ``FAKE_PRINTER_UPLOAD_DIR``, is made at start so a ``STOR`` into
it works. Its default, ``/``, and the ``ftp://<name>`` a print names are plan 3a Task
7's measurement on an H2C (firmware 01.02.00.00, 2026-10-24; ``scadbuddy/printers/lan.py``
``UPLOAD_DIR``, ``file_url``): a 3MF stored at ``/<name>`` and named ``ftp://<name>``
started, and the printer then moved it to ``/cache/<name>.gcode.3mf``, after which
``/<name>`` answered 550. Bambuddy uploads to the root too ("the start_print command
references files by name only (ftp://{filename}), so they must be in the root",
``backend/app/services/print_scheduler.py:7698-7701`` at maziggy/bambuddy ``0d427510``).

Other variables: ``FAKE_PRINTER_SERIAL``, ``FAKE_PRINTER_ACCESS_CODE`` (required),
``FAKE_PRINTER_HOST`` (the bind address and the certificate's name, ``0.0.0.0``),
``FAKE_PRINTER_STATE`` (the certificate, key, password file and broker config,
``/tmp/fake-printer``), ``FAKE_PRINTER_MOSQUITTO`` (the broker binary, ``mosquitto``),
``FAKE_PRINTER_PASSIVE_PORTS`` (``50000-50009``: FTPS data ports, to publish beside 990
when the container's ports are mapped).

Usage: ``python printer.py`` (the image's entrypoint). The process supervises its broker
and stops it on SIGTERM or SIGINT; it kills nothing else.
"""

import asyncio
import base64
import contextlib
import datetime
import hashlib
import ipaddress
import json
import logging
import os
import posixpath
import signal
import ssl
import sys
import threading
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

import aiomqtt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from OpenSSL import SSL
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, TLS_FTPHandler
from pyftpdlib.servers import FTPServer

log = logging.getLogger("fake-printer")

USERNAME = "bblp"
#: The SD card's root, where a print's 3MF is uploaded (plan 3a Task 7, measured; the
#: module docstring).
DEFAULT_UPLOAD_DIR = "/"
#: Where a started print's file is moved to (Task 7, measured): ``/cache/<name>.gcode.3mf``.
CACHE_DIR = "/cache"
REPORT = Path(__file__).with_name("report.json")
#: ``print_error`` for a ``project_file`` whose file is missing. Made up (not measured on
#: a printer); a client should only rely on it being non-zero.
FILE_NOT_FOUND = 0x0500_C010
#: Seconds between two RUNNING reports.
TICK = 0.25
#: mosquitto's PBKDF2-SHA512 password hash (``$7$``), with its default iterations.
_PBKDF2_ITERATIONS = 101


@dataclass
class Config:
    serial: str
    access_code: str
    print_seconds: float = 20.0
    host: str = "0.0.0.0"
    mqtt_port: int = 8883
    ftps_port: int = 990
    control_port: int = 8081
    sdcard: Path = Path("/sdcard")
    upload_dir: str = DEFAULT_UPLOAD_DIR
    state: Path = Path("/tmp/fake-printer")
    mosquitto: str = "mosquitto"
    passive_ports: range | None = None

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        passive = env.get("FAKE_PRINTER_PASSIVE_PORTS", "50000-50009")
        low, _, high = passive.partition("-")
        return cls(
            serial=env["FAKE_PRINTER_SERIAL"],
            access_code=env["FAKE_PRINTER_ACCESS_CODE"],
            print_seconds=float(env.get("FAKE_PRINT_SECONDS", "20")),
            host=env.get("FAKE_PRINTER_HOST", "0.0.0.0"),
            mqtt_port=int(env.get("FAKE_PRINTER_MQTT_PORT", "8883")),
            ftps_port=int(env.get("FAKE_PRINTER_FTPS_PORT", "990")),
            control_port=int(env.get("FAKE_PRINTER_CONTROL_PORT", "8081")),
            sdcard=Path(env.get("FAKE_PRINTER_SDCARD", "/sdcard")),
            upload_dir=env.get("FAKE_PRINTER_UPLOAD_DIR", DEFAULT_UPLOAD_DIR),
            state=Path(env.get("FAKE_PRINTER_STATE", "/tmp/fake-printer")),
            mosquitto=env.get("FAKE_PRINTER_MOSQUITTO", "mosquitto"),
            passive_ports=range(int(low), int(high or low) + 1) if passive else None,
        )


@dataclass
class Received:
    """What the printer was sent; written by the MQTT loop and the FTPS thread, read by
    the control thread."""

    mqtt: list[dict[str, Any]] = field(default_factory=list)
    ftps: list[dict[str, Any]] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_mqtt(self, command: dict[str, Any]) -> None:
        with self.lock:
            self.mqtt.append(command)

    def add_ftps(self, entry: dict[str, Any]) -> None:
        with self.lock:
            self.ftps.append(entry)

    def refuse(self, entry: dict[str, Any]) -> None:
        with self.lock:
            self.refused.append(entry)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        with self.lock:
            return {"mqtt": list(self.mqtt), "ftps": list(self.ftps), "refused": list(self.refused)}

    def reset(self) -> None:
        with self.lock:
            self.mqtt.clear()
            self.ftps.clear()
            self.refused.clear()


def mosquitto_password(password: str, salt: bytes | None = None) -> str:
    """A mosquitto 2 password-file hash: ``$7$<iterations>$<salt>$<hash>``, PBKDF2-HMAC-
    SHA512 with a 12-byte salt and a 64-byte hash, both base64, 101 iterations (eclipse
    mosquitto ``v2.0.21``: ``src/password_mosq.c`` ``pw__hash``, ``SALT_LEN``;
    ``src/password_mosq.h`` ``PW_DEFAULT_ITERATIONS``; ``apps/mosquitto_passwd/
    mosquitto_passwd.c`` ``output_new_password``)."""
    salt = salt if salt is not None else os.urandom(12)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, _PBKDF2_ITERATIONS)
    encode = base64.b64encode
    return f"$7${_PBKDF2_ITERATIONS}${encode(salt).decode()}${encode(digest).decode()}"


def _certificate(host: str, cert: Path, key: Path) -> None:
    """A self-signed certificate naming ``host`` (when it is not a wildcard bind),
    ``localhost`` and ``127.0.0.1``; a real printer's names no host at all."""
    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-bambu-printer")])
    alt: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    if host not in ("0.0.0.0", "::", "127.0.0.1", "localhost"):
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            alt.append(x509.DNSName(host))
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private, hashes.SHA256())
    )
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key.chmod(0o600)


def _ftp_server(config: Config, cert: Path, key: Path, received: Received) -> FTPServer:
    root = config.sdcard

    class ImplicitTlsHandler(TLS_FTPHandler):  # type: ignore[misc]
        """TLS from the first byte: secured as accepted, the banner sent once secure."""

        certfile = str(cert)
        keyfile = str(key)
        ssl_context = None
        tls_control_required = True

        @classmethod
        def get_ssl_context(cls) -> SSL.Context:
            """TLS 1.2 only, as a printer's FTPS is ("Real Bambu printers also serve their
            FTPS at 1.2 only", Bambuddy ``backend/app/services/virtual_printer/
            ftp_server.py:702-713`` at ``0d427510``). It matters to a client that closes
            a data connection without ``close_notify`` (``LanPrinter.upload``, after
            ha-bambulab): under TLS 1.3 the server's post-handshake session tickets sit
            unread in the client's socket, its close sends a RST, and the server drops
            what it had not read yet. Measured here with 1.3 allowed: a 200,000-byte
            ``STOR`` arrived as 98,304; Bambuddy saw the same "RST after ~80 KiB".

            The cap also hides that defect from this fake's clients: 3a's LAN client
            (``scadbuddy/printers/lan.py`` ``LanPrinter.upload``, ``pinned_context``,
            on ``feat/printer-registry``) needs the same fix on its side, a TLS 1.2
            maximum on its FTPS context or a drained ``unwrap`` before close, so that a
            printer that negotiates 1.3 does not truncate every upload."""
            built = cls.ssl_context is not None
            context: SSL.Context = super().get_ssl_context()
            if not built:  # a context is fixed once it has made a connection
                context.set_max_proto_version(SSL.TLS1_2_VERSION)
            return context

        def handle(self) -> None:
            self.secure_connection(self.ssl_context)

        def handle_ssl_established(self) -> None:
            FTPHandler.handle(self)

        def on_file_received(self, file: str) -> None:
            path = Path(file)
            data = path.read_bytes()
            received.add_ftps(
                {
                    "name": "/" + path.relative_to(root).as_posix(),
                    "size": len(data),
                    "md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
                }
            )

    authorizer = DummyAuthorizer()
    authorizer.add_user(USERNAME, config.access_code, str(root), perm="elradfmwMT")
    ImplicitTlsHandler.authorizer = authorizer
    ImplicitTlsHandler.banner = "fake Bambu printer ready"
    if config.passive_ports is not None:
        ImplicitTlsHandler.passive_ports = list(config.passive_ports)
    try:
        return FTPServer((config.host, config.ftps_port), ImplicitTlsHandler)
    except OSError as error:
        # pyftpdlib re-raises a failed bind as ``OSError(<the bind's OSError>)``, which has
        # no ``errno``: raise the bind's own, so a taken port reads as EADDRINUSE (#4944).
        cause = error.args[0] if len(error.args) == 1 else None
        if isinstance(cause, OSError):
            raise cause from error
        raise


def _control_server(config: Config, received: Received) -> ThreadingHTTPServer:
    class Control(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/__received":
                self.send_error(404)
                return
            body = json.dumps(received.snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/__reset":
                self.send_error(404)
                return
            received.reset()
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("control: " + format, *args)

    return ThreadingHTTPServer((config.host, config.control_port), Control)


def resolve_url(url: str, file: str = "") -> str | None:
    """The SD-card path a ``project_file`` names, or ``None``.

    The forms seen in the sources: ``ftp://<name>`` (Bambuddy's dispatch, a file in the
    root, ``bambu_mqtt.py:6120`` at ``0d427510``), ``ftp:///<path>`` and
    ``file:///mnt/sdcard/<path>`` (OpenBambuAPI ``mqtt.md``, ``print.project_file``, at
    ``cc383a2c``), plus ``file:///sdcard/<path>``. With no ``url``, the command's
    ``file``. Which form a real printer takes is plan 3a Task 7's measurement. A path with
    a ``..`` segment names nothing (#4205): a typo that climbed out of the card would
    otherwise "find" a file there and pass a test that should fail. So does a card root
    that only starts like one (``file:///mnt/sdcardX/...``)."""
    if not url:
        return _card_path(file)
    for root in ("file:///mnt/sdcard", "file:///sdcard"):
        if url == root or url.startswith(root + "/"):
            return _card_path(url[len(root) :])
    if url.startswith("ftp://"):
        return _card_path(url[len("ftp://") :])
    return None


def _card_path(rest: str) -> str | None:
    """``rest`` as an absolute path on the card, or ``None`` when it is empty or climbs."""
    rest = rest.lstrip("/")
    if not rest or ".." in rest.split("/"):
        return None
    return posixpath.normpath("/" + rest)


class FakePrinter:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.received = Received()
        report = json.loads(REPORT.read_text())["print"]
        self.state: dict[str, Any] = report
        self._printing: asyncio.Task[None] | None = None
        self._stopping = threading.Event()

    @property
    def request_topic(self) -> str:
        return f"device/{self.config.serial}/request"

    @property
    def report_topic(self) -> str:
        return f"device/{self.config.serial}/report"

    def _broker_files(self) -> tuple[Path, Path, Path]:
        state = self.config.state
        state.mkdir(parents=True, exist_ok=True)
        cert, key = state / "cert.pem", state / "key.pem"
        if not (cert.is_file() and key.is_file()):
            _certificate(self.config.host, cert, key)
        passwd = state / "passwd"
        passwd.write_text(f"{USERNAME}:{mosquitto_password(self.config.access_code)}\n")
        passwd.chmod(0o600)
        acl = state / "acl"
        acl.write_text(
            f"user {USERNAME}\n"
            f"topic readwrite {self.request_topic}\n"
            f"topic readwrite {self.report_topic}\n"
        )
        acl.chmod(0o600)
        conf = state / "mosquitto.conf"
        conf.write_text(
            "\n".join(
                [
                    "per_listener_settings false",
                    "allow_anonymous false",
                    f"password_file {passwd}",
                    f"acl_file {acl}",
                    "persistence false",
                    "log_dest stderr",
                    f"listener {self.config.mqtt_port} {self.config.host}",
                    f"certfile {cert}",
                    f"keyfile {key}",
                    "",
                ]
            )
        )
        return conf, cert, key

    async def _wait_for_broker(self, broker: asyncio.subprocess.Process) -> None:
        host = "127.0.0.1" if self.config.host in ("0.0.0.0", "::") else self.config.host
        async with asyncio.timeout(30):
            while True:
                if broker.returncode is not None:
                    raise RuntimeError(f"mosquitto exited with {broker.returncode}")
                with contextlib.suppress(OSError):
                    _, writer = await asyncio.open_connection(host, self.config.mqtt_port)
                    writer.close()
                    await writer.wait_closed()
                    return
                await asyncio.sleep(0.2)

    def _serve_ftp(self, server: FTPServer) -> None:
        try:
            while not self._stopping.is_set():
                # The IO loop itself: ``serve_forever(blocking=False)`` logs a start line
                # on every call.
                server.ioloop.loop(timeout=0.2, blocking=False)
        finally:
            server.close_all()

    async def run(self, ready: asyncio.Event | None = None) -> None:
        config = self.config
        (config.sdcard / config.upload_dir.strip("/")).mkdir(parents=True, exist_ok=True)
        conf, cert, key = self._broker_files()
        broker = await asyncio.create_subprocess_exec(config.mosquitto, "-c", str(conf))
        ftp = control = None
        ftp_thread: threading.Thread | None = None
        threads: list[threading.Thread] = []
        try:
            await self._wait_for_broker(broker)
            ftp = _ftp_server(config, cert, key, self.received)
            ftp_thread = threading.Thread(target=self._serve_ftp, args=(ftp,), daemon=True)
            threads.append(ftp_thread)
            context = ssl.create_default_context(cafile=str(cert))
            async with aiomqtt.Client(
                "127.0.0.1" if config.host in ("0.0.0.0", "::") else config.host,
                config.mqtt_port,
                username=USERNAME,
                password=config.access_code,
                tls_context=context,
                identifier=f"printer-{config.serial}",
            ) as client:
                await client.subscribe(self.request_topic)
                control = _control_server(config, self.received)
                threads.append(threading.Thread(target=control.serve_forever, daemon=True))
                for thread in threads:
                    thread.start()
                log.info("fake printer %s ready", config.serial)
                if ready is not None:
                    ready.set()
                async for message in client.messages:
                    await self._handle(client, message.payload)
        finally:
            self._stopping.set()
            if self._printing is not None:
                self._printing.cancel()
            if control is not None:
                control.shutdown()
                control.server_close()
            # A bind that failed (the control port taken) leaves them unstarted: joining
            # one would raise and hide the bind's OSError, which a caller retries on.
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=5)
            # The FTP server's own thread closes it once started; unstarted, close it here.
            if ftp is not None and (ftp_thread is None or ftp_thread.ident is None):
                ftp.close_all()
            if broker.returncode is None:
                broker.terminate()
                try:
                    await asyncio.wait_for(broker.wait(), 10)
                except TimeoutError:
                    broker.kill()
                    await broker.wait()

    async def _publish(self, client: aiomqtt.Client) -> None:
        await client.publish(self.report_topic, json.dumps({"print": self.state}))

    async def _handle(self, client: aiomqtt.Client, payload: Any) -> None:
        try:
            body = json.loads(payload)
        except (TypeError, ValueError):
            return
        if not isinstance(body, dict):
            return
        self.received.add_mqtt(body)
        if body.get("pushing", {}).get("command") == "pushall":
            await self._publish(client)
            return
        command = body.get("print")
        if not isinstance(command, dict):
            return
        if command.get("command") == "project_file":
            if self._printing is not None:
                self._printing.cancel()
                self._printing = None
            url = str(command.get("url") or "")
            path = resolve_url(url, str(command.get("file") or ""))
            reason = self._missing(path)
            await self._echo(client, command, url, reason)
            if reason is not None:
                self.received.refuse({"url": url, "reason": reason})
                self.state.update(gcode_state="FAILED", print_error=FILE_NOT_FOUND)
                await self._publish(client)
                return
            assert path is not None
            self._take(path)
            self._printing = asyncio.create_task(self._print(client, command, path))
        elif command.get("command") == "stop" and self._printing is not None:
            self._printing.cancel()
            self._printing = None
            self.state.update(gcode_state="FAILED", mc_remaining_time=0)
            await self._publish(client)

    def _missing(self, path: str | None) -> str | None:
        """Why ``path`` cannot be printed, or ``None`` when it can."""
        if path is None:
            return "the url names no file"
        upload_dir = "/" + self.config.upload_dir.strip("/")
        if PurePosixPath(path).parent != PurePosixPath(upload_dir):
            return f"{path} is not in the upload directory {upload_dir}"
        if not (self.config.sdcard / path.lstrip("/")).is_file():
            return f"{path} was never uploaded"
        return None

    def _take(self, path: str) -> str:
        """Move a started print's file to `CACHE_DIR` as ``<name>.gcode.3mf``, as an H2C
        does: the upload's own path then names nothing."""
        card = self.config.sdcard
        cached = f"{CACHE_DIR}/{PurePosixPath(path).name}.gcode.3mf"
        (card / CACHE_DIR.lstrip("/")).mkdir(parents=True, exist_ok=True)
        (card / path.lstrip("/")).replace(card / cached.lstrip("/"))
        return cached

    async def _echo(
        self, client: aiomqtt.Client, command: dict[str, Any], url: str, reason: str | None
    ) -> None:
        echo = {
            "command": "project_file",
            "sequence_id": command.get("sequence_id", ""),
            "param": command.get("param", ""),
            "url": url,
            "result": "SUCCESS" if reason is None else "FAIL",
        }
        if reason is not None:
            echo["reason"] = reason
        await client.publish(self.report_topic, json.dumps({"print": echo}))

    async def _print(self, client: aiomqtt.Client, command: dict[str, Any], path: str) -> None:
        seconds = self.config.print_seconds
        self.state.update(
            print_error=0,
            gcode_state="PREPARE",
            mc_percent=0,
            mc_remaining_time=max(1, round(seconds / 60)),
            subtask_name=command.get("subtask_name", ""),
            gcode_file=PurePosixPath(path).name,
            layer_num=0,
            total_layer_num=100,
        )
        await self._publish(client)
        steps = max(1, round(seconds / TICK))
        for step in range(steps + 1):
            await asyncio.sleep(seconds / (steps + 1))
            percent = round(100 * step / steps)
            self.state.update(
                gcode_state="RUNNING",
                mc_percent=percent,
                layer_num=percent,
                mc_remaining_time=max(0, round(seconds * (1 - step / steps) / 60)),
            )
            await self._publish(client)
        self.state.update(gcode_state="FINISH", mc_percent=100, mc_remaining_time=0)
        await self._publish(client)
        self._printing = None


def health() -> None:
    """``--health``: exit non-zero unless the control port answers (the HEALTHCHECK)."""
    host = os.environ.get("FAKE_PRINTER_HOST", "0.0.0.0")
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    port = os.environ.get("FAKE_PRINTER_CONTROL_PORT", "8081")
    with urllib.request.urlopen(f"http://{host}:{port}/__received", timeout=1):
        pass


def main() -> None:
    if sys.argv[1:] == ["--health"]:
        health()
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = Config.from_env()

    async def serve() -> None:
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await FakePrinter(config).run()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
