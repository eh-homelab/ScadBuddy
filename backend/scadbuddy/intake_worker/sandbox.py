"""``python -m scadbuddy.intake_worker.sandbox``: the intake sandbox (#6133, spec 2026-11-13
§7), a Deployment of its own. It runs every check's child (`runner.run_child`), so the
customer's file is parsed in a pod that holds no service-account token, no proof, no
database secret and no KEK, and shares no network namespace and no volume with the worker.
The worker hands it each check over HTTP on the pod network: ``POST /check`` on
``SCADBUDDY_INTAKE_SANDBOX_PORT`` (default `runner.SANDBOX_PORT`), the wire format in
`runner`'s docstring; ``GET /healthz`` answers 200 while it serves. A check must carry the
RPC token (``Authorization: Bearer <token>``, compared with `hmac.compare_digest`): without
it the answer is 401 before any byte of the body is read, and the connection is closed.
The token authorizes calls to this sandbox only, so it is the one secret the sandbox
holds; its NetworkPolicy admits only the worker's pods too, and the worker reads every
answer as untrusted. A request's head and whole body must arrive within
`REQUEST_DEADLINE`, so one connection cannot hold the one check slot by sending slowly.
It refuses to start without a valid token file (`runner.read_token`, from
``SCADBUDDY_INTAKE_SANDBOX_TOKEN_FILE``), with a database secret, the KEK or a proof
(`check_sandbox_environment`), or with any file under `SECRETS_DIR` (a mounted
ServiceAccount token or secret, `mounted_secrets`; the RPC token's Secret is mounted
elsewhere), and makes itself non-dumpable (`hardening.forbid_dumping`).

Each check's child runs in a box of its own (``box/`` on the sandbox's own spool,
``SCADBUDDY_INTAKE_WORK_DIR``, an emptyDir; mode 0700, the upload made in it with
``O_EXCL | O_NOFOLLOW``) through its launcher with ``--strict`` (`confine.py`): its own user,
network, mount and PID namespaces with an empty tmpfs over the spool, then no capabilities,
no new privileges and a seccomp filter. The launcher inherits the box's descriptor. The box
is removed after each check. It fails closed: at start the sandbox runs the launcher once
(`probe_isolation`), and where any step is refused it logs why and exits 3 without serving a
check, so the pod never parses a file unconfined (the worker counts the checks that then
fail, `runner.SANDBOX_FAILED`).

The isolation rule (spec §7): one customer's file per pod at any moment. The sandbox runs
one check at a time, fixed: a second request while one runs is answered 503, never queued,
and its body is never stored. Scale with replicas of the Deployment, never with checks at
once. Before each check it also makes sure no process of an earlier check is alive
(`clear_job_processes`): it is the pod's child subreaper, so a check's orphans become its
own, and it kills what it finds; one that survives makes it stop serving and exit 4. When
the client goes away or the check's timeout passes, the child's group is killed.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, Final

from scadbuddy.core.logging import configure_logging
from scadbuddy.intake_worker import confine
from scadbuddy.intake_worker.child import PREVIEW, UPLOAD, VERDICT
from scadbuddy.intake_worker.hardening import forbid_dumping
from scadbuddy.intake_worker.hardening import prctl as hardening_prctl
from scadbuddy.intake_worker.runner import (
    BOX,
    BOX_MODE,
    ID_HEADER,
    MAX_VERDICT_BYTES,
    REQUEST_HEADER,
    SANDBOX_PORT,
    TOKEN_FILE_ENV,
    VERDICT_BYTES_HEADER,
    CheckCrashedError,
    open_dir,
    parse_request,
    read_output,
    read_token,
    run_child,
)
from scadbuddy.intake_worker.settings import FORBIDDEN_ENV, IntakeRefusal
from scadbuddy.intake_worker.wire import MAX_PREVIEW_BYTES
from scadbuddy.storefront.wire import MAX_MODEL_FILE_BYTES

logger = logging.getLogger(__name__)

#: Besides `FORBIDDEN_ENV`: the sandbox holds no proof of the worker's.
SANDBOX_FORBIDDEN_ENV: Final = (
    *FORBIDDEN_ENV,
    "SCADBUDDY_SERVICE_TOKEN_FILE",
    "SCADBUDDY_SERVICE_BOOTSTRAP_FILE",
)
#: A request's head (its line and headers): at most this many bytes and headers, read
#: within `HEAD_TIMEOUT`.
MAX_HEAD_BYTES: Final = 16 << 10
MAX_HEADERS: Final = 64
HEAD_TIMEOUT: Final = 10.0
#: The longest wait for the next piece of a body.
BODY_IDLE: Final = 60.0
#: A request's head and its whole body arrive within this many seconds of the connection,
#: or the sandbox drops it, frees the slot and keeps no file. 256 MiB in 60 s is about
#: 4.5 MB/s, well below the pod network's speed.
REQUEST_DEADLINE: float = 60.0
#: After an early refusal, how long the sandbox reads and drops the unread body so the
#: client sees the answer, not a reset.
LINGER: Final = 0.5
_CHUNK: Final = 1 << 20
_TOKEN: Final = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_REASONS: Final = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    411: "Length Required",
    413: "Content Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


#: Where Kubernetes mounts a ServiceAccount token
#: (``kubernetes.io/serviceaccount/token``) and where secret volumes go by convention. Any
#: file here refuses the start (`mounted_secrets`): the sandbox's pod must set
#: ``automountServiceAccountToken: false`` and mount no secret
#: ([Kubernetes: ServiceAccount tokens](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/#opt-out-of-api-credential-automounting)).
SECRETS_DIR: Path = Path("/var/run/secrets")


def mounted_secrets(root: Path) -> list[str]:
    """Every entry under ``root`` that is not a directory (a file, or a symlink as a
    projected volume mounts them), plus every folder that cannot be listed (it may hold
    one: fail closed). Empty when ``root`` is missing."""
    found: list[str] = []

    def unreadable(error: OSError) -> None:
        if not isinstance(error, FileNotFoundError) or error.filename != str(root):
            found.append(str(error.filename))

    for folder, dirs, files in os.walk(root, onerror=unreadable, followlinks=False):
        found += [os.path.join(folder, name) for name in files]
        found += [
            os.path.join(folder, name)
            for name in dirs
            if os.path.islink(os.path.join(folder, name))
        ]
    return sorted(found)


def check_sandbox_environment(environ: Mapping[str, str]) -> None:
    reasons = [
        f"{name} is set; the intake sandbox holds no secret, key or proof"
        for name in SANDBOX_FORBIDDEN_ENV
        if name in environ
    ]
    if reasons:
        raise IntakeRefusal(reasons)


def probe_isolation(spool: Path) -> str | None:
    """Whether a check can be confined here (`confine.py --strict`, running a program
    that does nothing): None when it can, else why not."""
    out = subprocess.run(
        confine.command(str(spool), [sys.executable, "-I", "-c", "pass"], strict=True),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if out.returncode != 0:
        return out.stderr.strip() or f"the launcher exited with {out.returncode}"
    return None


#: How long the processes of an earlier check may take to die once killed.
JOB_EXIT_GRACE: Final = 5.0
_JOB_POLL: Final = 0.2
_PR_SET_CHILD_SUBREAPER: Final = 36


class JobOutlivedError(RuntimeError):
    """A process of an earlier check is still alive: no next check may run."""


def job_processes() -> list[int]:
    """This process's descendants in another PID namespace than its own, or whose
    namespace cannot be read: the processes of a check, since each parse has a PID
    namespace of its own and its launcher and parser are non-dumpable (`confine.py`).
    Between checks no other descendant exists. A zombie does not count, and is reaped when
    it is this process's child (the sandbox is the pod's child subreaper)."""
    own = os.readlink("/proc/self/ns/pid")
    found: list[int] = []
    todo = [os.getpid()]
    while todo:
        parent = todo.pop()
        try:
            tasks = os.listdir(f"/proc/{parent}/task")
        except OSError:
            continue
        for tid in tasks:
            try:
                with open(f"/proc/{parent}/task/{tid}/children") as listed:
                    children = [int(pid) for pid in listed.read().split()]
            except OSError:
                continue
            for pid in children:
                todo.append(pid)
                if _zombie(pid):
                    if parent == os.getpid():
                        with suppress(ChildProcessError):
                            os.waitpid(pid, os.WNOHANG)
                    continue
                try:
                    if os.readlink(f"/proc/{pid}/ns/pid") != own:
                        found.append(pid)
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except OSError:
                    found.append(pid)
    return found


def _zombie(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat_file:
            return stat_file.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")
    except (OSError, IndexError):
        return True


async def clear_job_processes() -> None:
    """No process of an earlier check left alive: each found is killed, and one still
    alive after `JOB_EXIT_GRACE` raises `JobOutlivedError`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + JOB_EXIT_GRACE
    while left := job_processes():
        if loop.time() > deadline:
            raise JobOutlivedError(f"processes of an earlier job outlived it: {left[:10]}")
        for pid in left:
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        await asyncio.sleep(_JOB_POLL)


class _BadHeadError(Exception):
    pass


def _parse_head(head: bytes) -> tuple[str, str, dict[str, list[str]]]:
    """The method, the path and the headers (lowercase names) of a request's head."""
    lines = head.decode("latin-1").split("\r\n")[:-2]
    parts = lines[0].split(" ") if lines else []
    if (
        len(parts) != 3
        or parts[2] not in ("HTTP/1.1", "HTTP/1.0")
        or not _TOKEN.fullmatch(parts[0])
    ):
        raise _BadHeadError
    if len(lines) - 1 > MAX_HEADERS:
        raise _BadHeadError
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if not colon or not _TOKEN.fullmatch(name):
            raise _BadHeadError
        headers.setdefault(name.lower(), []).append(value.strip(" \t"))
    return parts[0], parts[1], headers


def _error(status: int, error: str, detail: str = "") -> tuple[int, dict[str, str], bytes]:
    body = json.dumps({"error": error, "detail": detail} if detail else {"error": error})
    return status, {"Content-Type": "application/json"}, body.encode()


class Sandbox:
    """The server's state: one check at a time (`busy`); `outlived` and `stopped` once a
    process of an earlier check will not die."""

    def __init__(self, spool: Path, token: str) -> None:
        self.spool = spool
        self._authorization = f"Bearer {token}".encode("ascii")
        self.busy = False
        self.outlived: JobOutlivedError | None = None
        self.stopped = asyncio.Event()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("the intake sandbox could not answer a request")
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        deadline = asyncio.get_running_loop().time() + REQUEST_DEADLINE
        try:
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), min(HEAD_TIMEOUT, _left(deadline))
            )
        except asyncio.LimitOverrunError:
            await _send(writer, *_error(431, "head-too-large"), reader=reader, unread=None)
            return
        except (asyncio.IncompleteReadError, TimeoutError):
            return
        try:
            method, path, headers = _parse_head(head)
        except _BadHeadError:
            await _send(writer, *_error(400, "bad-head"), reader=reader, unread=None)
            return
        if path == "/healthz":
            answer = (200, {"Content-Type": "text/plain"}, b"ok") if method == "GET" else None
            await _send(writer, *(answer or _error(405, "method")))
            return
        if path != "/check":
            await _send(writer, *_error(404, "not-found"), reader=reader, unread=None)
            return
        if method != "POST":
            await _send(writer, *_error(405, "method"), reader=reader, unread=None)
            return
        if not self._authorized(headers):
            # Before any byte of the body is read; the connection is closed, not drained.
            status, kind, body = _error(401, "unauthorized")
            await _send(writer, status, {**kind, "WWW-Authenticate": "Bearer"}, body)
            return
        await self._check(reader, writer, headers, deadline)

    def _authorized(self, headers: dict[str, list[str]]) -> bool:
        values = headers.get("authorization", [])
        presented = values[0].encode("latin-1") if len(values) == 1 else b""
        return hmac.compare_digest(presented, self._authorization)

    async def _check(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, list[str]],
        deadline: float,
    ) -> None:
        lengths = headers.get("content-length", [])
        if "transfer-encoding" in headers or len(lengths) > 1:
            await _send(writer, *_error(400, "bad-length"), reader=reader, unread=None)
            return
        if not lengths:
            await _send(writer, *_error(411, "no-length"), reader=reader, unread=None)
            return
        if not (lengths[0].isascii() and lengths[0].isdigit()) or int(lengths[0]) == 0:
            await _send(writer, *_error(400, "bad-length"), reader=reader, unread=None)
            return
        length = int(lengths[0])
        if length > MAX_MODEL_FILE_BYTES:
            await _send(writer, *_error(413, "too-large"), reader=reader, unread=length)
            return
        requests = headers.get(REQUEST_HEADER.lower(), [])
        try:
            if len(requests) != 1:
                raise ValueError("one request header")
            request = parse_request(requests[0])
        except (ValueError, TypeError) as bad:
            await _send(
                writer, *_error(400, "bad-request", str(bad)[:200]), reader=reader, unread=length
            )
            return
        if self.busy or self.stopped.is_set():
            error = "stopping" if self.stopped.is_set() else "busy"
            await _send(writer, *_error(503, error), reader=reader, unread=length)
            return
        self.busy = True
        try:
            answer = await self._job(reader, request, length, deadline)
        finally:
            self.busy = False
        if answer is not None:
            # A 503 here (stopping) came before the body was read.
            await _send(writer, *answer, reader=reader, unread=length if answer[0] == 503 else 0)

    async def _job(
        self, reader: asyncio.StreamReader, request: dict[str, Any], length: int, deadline: float
    ) -> tuple[int, dict[str, str], bytes] | None:
        """One check in a fresh box; its answer, or None when the client went away."""
        try:
            await clear_job_processes()
        except JobOutlivedError as error:
            logger.error("the intake sandbox stops: %s", error)
            self.outlived = error
            self.stopped.set()
            return _error(503, "stopping")
        spool_fd = open_dir(self.spool)
        try:
            with suppress(FileNotFoundError):
                shutil.rmtree(BOX, dir_fd=spool_fd)
            os.mkdir(BOX, BOX_MODE, dir_fd=spool_fd)
            box_fd = open_dir(BOX, dir_fd=spool_fd)
            try:
                os.fchmod(box_fd, BOX_MODE)
                return await self._run(reader, box_fd, request, length, deadline)
            finally:
                os.close(box_fd)
                with suppress(OSError):
                    shutil.rmtree(BOX, dir_fd=spool_fd)
        finally:
            os.close(spool_fd)

    async def _run(
        self,
        reader: asyncio.StreamReader,
        box_fd: int,
        request: dict[str, Any],
        length: int,
        deadline: float,
    ) -> tuple[int, dict[str, str], bytes] | None:
        if not await _receive(reader, box_fd, length, deadline):
            logger.info("a request's body did not arrive whole in time; it was dropped")
            return None
        cancelled = asyncio.Event()

        async def watch() -> None:
            # The body is read whole; the next read ends only when the client goes away.
            with suppress(Exception):
                await reader.read(1)
            cancelled.set()

        watcher = asyncio.create_task(watch())
        try:
            # The box by its descriptor, inherited by the launcher, never by a path. Not by
            # this process's /proc/<pid>/fd link: it is non-dumpable, so its children
            # cannot follow one.
            code = await run_child(
                box_fd,
                request["filename"],
                hide=str(self.spool),
                strict=True,
                timeout=request["timeout"],
                address_space=request["address_space"],
                cancelled=cancelled,
            )
        except CheckCrashedError:
            if cancelled.is_set():
                logger.info("the client went away; the check was stopped")
                return None
            return _error(504, "timeout", f"past {request['timeout']:g}s")
        except OSError as error:
            logger.warning("the intake sandbox could not start a check: %s", error)
            return _error(500, "spawn-failed")
        finally:
            watcher.cancel()
        if code != 0:
            return _error(500, "crashed", f"exit {code}")
        own = os.geteuid()
        try:
            verdict = read_output(box_fd, VERDICT, MAX_VERDICT_BYTES, own)
        except CheckCrashedError:
            return _error(500, "no-verdict")
        try:
            os.stat(PREVIEW, dir_fd=box_fd, follow_symlinks=False)
        except FileNotFoundError:
            preview = b""
        else:
            try:
                preview = read_output(box_fd, PREVIEW, MAX_PREVIEW_BYTES, own)
            except CheckCrashedError:
                return _error(500, "bad-output")
        headers = {
            "Content-Type": "application/octet-stream",
            ID_HEADER: request["id"],
            VERDICT_BYTES_HEADER: str(len(verdict)),
        }
        return 200, headers, verdict + preview


def _left(deadline: float) -> float:
    return max(0.0, deadline - asyncio.get_running_loop().time())


async def _receive(reader: asyncio.StreamReader, box_fd: int, length: int, deadline: float) -> bool:
    """The body streamed into the box's ``upload.bin`` (made here, ``O_EXCL``, never
    through a symlink), exactly ``length`` bytes by ``deadline``; False when the client
    went away or the deadline passed."""
    fd = os.open(
        UPLOAD,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=box_fd,
    )
    try:
        left = length
        while left:
            wait = min(BODY_IDLE, _left(deadline))
            if wait <= 0:
                return False
            try:
                chunk = await asyncio.wait_for(reader.read(min(_CHUNK, left)), wait)
            except (TimeoutError, ConnectionError):
                return False
            if not chunk:
                return False
            view = memoryview(chunk)
            while view:
                view = view[os.write(fd, view) :]
            left -= len(chunk)
        return True
    finally:
        os.close(fd)


async def _send(
    writer: asyncio.StreamWriter,
    status: int,
    headers: dict[str, str],
    body: bytes,
    *,
    reader: asyncio.StreamReader | None = None,
    unread: int | None = 0,
) -> None:
    """The answer, then ``Connection: close``. With ``reader``, a body left unread
    (``unread`` bytes, None: unknown) is read and dropped for at most `LINGER`, so the
    client reads the answer rather than a reset."""
    lines = [f"HTTP/1.1 {status} {_REASONS[status]}"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    lines += [f"Content-Length: {len(body)}", "Connection: close"]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
    await writer.drain()
    if reader is None or unread == 0:
        return
    with suppress(Exception):
        writer.write_eof()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LINGER
    left = unread
    with suppress(Exception):
        while left is None or left > 0:
            wait = deadline - loop.time()
            if wait <= 0:
                break
            chunk = await asyncio.wait_for(
                reader.read(_CHUNK if left is None else min(_CHUNK, left)), wait
            )
            if not chunk:
                break
            if left is not None:
                left -= len(chunk)


async def start(sandbox: Sandbox, *, host: str, port: int) -> asyncio.Server:
    return await asyncio.start_server(sandbox.handle, host, port, limit=MAX_HEAD_BYTES)


async def serve(
    spool: Path,
    stop: asyncio.Event,
    *,
    token: str,
    host: str = "0.0.0.0",
    port: int = SANDBOX_PORT,
) -> None:
    """Serves checks one at a time until ``stop``; `JobOutlivedError` once a process of an
    earlier check will not die."""
    sandbox = Sandbox(spool, token)
    server = await start(sandbox, host=host, port=port)
    logger.info("intake sandbox serving on %s:%d, one check at a time", host, port)
    waits = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(sandbox.stopped.wait())]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiting in waits:
            waiting.cancel()
        # A check still running is cancelled with the loop's other tasks, which kills its
        # child's group (`run_child`).
        server.close()
    if sandbox.outlived is not None:
        raise sandbox.outlived


async def _main(spool: Path, port: int, token: str) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    await serve(spool, stop, token=token, port=port)


def _port(value: str) -> int | None:
    if value.isascii() and value.isdigit() and 0 < int(value) < 65536:
        return int(value)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging(os.environ.get("SCADBUDDY_LOG_LEVEL", "INFO").upper())
    try:
        check_sandbox_environment(os.environ)
    except IntakeRefusal as refusal:
        for reason in refusal.reasons:
            logger.error(reason)
        return 2
    secrets = mounted_secrets(SECRETS_DIR)
    if secrets:
        logger.error(
            "the intake sandbox refuses to start: a mounted secret is present (%s); the"
            " sandbox holds no token or secret (automountServiceAccountToken: false)",
            ", ".join(secrets[:5]),
        )
        return 2
    token_file = os.environ.get(TOKEN_FILE_ENV)
    if not token_file:
        logger.error("the intake sandbox refuses to start: %s is not set", TOKEN_FILE_ENV)
        return 2
    try:
        token = read_token(Path(token_file))
    except ValueError as bad:
        logger.error("the intake sandbox refuses to start: %s %s", TOKEN_FILE_ENV, bad)
        return 2
    port = _port(os.environ.get("SCADBUDDY_INTAKE_SANDBOX_PORT", str(SANDBOX_PORT)))
    if port is None:
        logger.error("SCADBUDDY_INTAKE_SANDBOX_PORT must be a port number, 1 to 65535")
        return 2
    forbid_dumping()
    spool = Path(os.environ.get("SCADBUDDY_INTAKE_WORK_DIR", "/tmp"))
    refused = probe_isolation(spool)
    if refused is not None:
        logger.error(
            "the intake sandbox refuses to start: a check cannot be confined here (%s)."
            " Allow unshare(CLONE_NEWUSER) for this container (its seccomp profile).",
            refused[:300],
        )
        return 3
    # A check's orphans become this process's children, so `job_processes` sees them.
    if hardening_prctl(_PR_SET_CHILD_SUBREAPER, 1) != 0:
        logger.error("the intake sandbox refuses to start: it cannot be the child subreaper")
        return 3
    try:
        asyncio.run(_main(spool, port, token))
    except JobOutlivedError as error:
        logger.error("the intake sandbox stops: %s", error)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
