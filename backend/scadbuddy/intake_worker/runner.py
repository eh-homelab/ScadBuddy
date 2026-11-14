"""Runs one check in a child process (`child.py`) started through its launcher
(`confine.py`: its own namespaces with the spool hidden, no capabilities, no new privileges,
a seccomp filter): its own process group, an address-space cap it sets on itself, and a
timeout, past which (or on cancellation) the whole group is killed, as
`workflows/template_process.py` runs template Python (spec 2026-11-13 §7).

Two ways to reach the child:

- the sandbox (``SCADBUDDY_INTAKE_SANDBOX=true``, the deployment's): a Deployment of its
  own (`sandbox.py`), whose pods share no network namespace, no volume and no token with
  the worker's. The worker hands it each check over HTTP on the pod network
  (``SCADBUDDY_INTAKE_SANDBOX_URL``, `run_check`). The sandbox starts the child there with
  ``--strict``: a launcher step refused is a failed check, never an unconfined parse. The
  worker never runs the parse.
- local (``SCADBUDDY_INTAKE_SANDBOX=unsafe-local``, development and tests only): the worker
  starts the child itself, in the check's scratch folder, and a launcher step refused is
  reported in the verdict's ``isolation``.

The RPC. ``POST /check`` with the file's bytes as the body (``Content-Length`` required,
at most `MAX_MODEL_FILE_BYTES`) and the request as JSON in `REQUEST_HEADER`: the filename,
the timeout, the address-space cap and a random ``id`` of 16 to 64 lowercase hex digits, at
most `MAX_REQUEST_BYTES`, checked by `parse_request`. The answer is 200,
``application/octet-stream``, with `ID_HEADER` that echoes the id and
`VERDICT_BYTES_HEADER` ``N``: the body's first ``N`` bytes are the verdict's JSON (at most
`MAX_VERDICT_BYTES`), the rest is the preview PNG (at most `MAX_PREVIEW_BYTES`, empty when
there is none). The framing is binary: never base64, never bytes inside JSON. Each other
status carries a small JSON error (``{"error": …, "detail": …}``) and fails the check: 400
a bad request, 404 or 405 a wrong route, 411 no length, 413 a body past its cap, 431 a head
past its cap, 500 a child that crashed, did not start or wrote no verdict, 503 busy (the
sandbox runs one check at a time) or stopping, 504 a check past its timeout. When the
client goes away, the sandbox kills the child's group.

The sandbox holds no secret, so the RPC carries no key: the sandbox's NetworkPolicy admits
only the worker's pods, and the worker reads every answer as untrusted (`_read_answer`):
status 200, the echoed id, `VERDICT_BYTES_HEADER` an integer within its cap, no
``Content-Encoding``, the body counted while it is read and stopped past
`MAX_ANSWER_BYTES`, the verdict validated (`parse_verdict`) and the preview a PNG with a
valid ``IHDR`` (`check_png`). Each failed call is counted in `SANDBOX_FAILED` by reason, on
the worker's ``/metrics``.

In local mode, what the child wrote is read as untrusted too (`read_output`): opened with
``O_NOFOLLOW`` through the descriptor of the scratch folder, so a symlink is refused;
``fstat`` must show a regular file with one link, owned by this process's user, of at most
its cap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import signal
import stat
import struct
import sys
import zlib
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NoReturn

import httpx
from prometheus_client import Counter
from pydantic import ValidationError

from scadbuddy.intake_worker import confine
from scadbuddy.intake_worker.child import PREVIEW, UPLOAD, VERDICT
from scadbuddy.intake_worker.wire import MAX_PREVIEW_BYTES, FileFacts

logger = logging.getLogger(__name__)

#: The child's address space: the largest measured fixture's model inflates to 470 MiB
#: (spec §7.1), and the read keeps its meshes besides.
CHILD_ADDRESS_SPACE: Final = 4 << 30
CHILD_TIMEOUT: Final = 300.0
#: The largest verdict a child may write.
MAX_VERDICT_BYTES: Final = 1 << 20
#: The largest answer body the worker reads: a verdict and a preview, each at its cap.
MAX_ANSWER_BYTES: Final = MAX_VERDICT_BYTES + MAX_PREVIEW_BYTES
#: The largest request header, and the largest error answer the worker reads.
MAX_REQUEST_BYTES: Final = 4096
MAX_ERROR_BYTES: Final = 4096
#: The longest file name a request carries.
MAX_FILENAME: Final = 255
#: The largest preview side: the check renders 512 (`check.PREVIEW_SIZE`).
MAX_PREVIEW_SIDE: Final = 4096
#: The variables the child gets: none of the worker's (its proof's path among them).
_ENV_KEEP: Final = ("PATH", "LANG", "LC_ALL", "TMPDIR")
#: A check's box on the sandbox's spool (mode 0700), and where the confined child sees it.
BOX: Final = "box"
BOX_MODE: Final = 0o700
PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"

#: The sandbox's default port (``SCADBUDDY_INTAKE_SANDBOX_PORT``), and the RPC's headers.
SANDBOX_PORT: Final = 8090
REQUEST_HEADER: Final = "X-Intake-Request"
ID_HEADER: Final = "X-Intake-Id"
VERDICT_BYTES_HEADER: Final = "X-Intake-Verdict-Bytes"
#: How long past the check's timeout the worker waits for the sandbox's answer.
SANDBOX_GRACE: Final = 30.0
#: How long the worker tries again while the sandbox answers 503 busy or cannot be reached
#: (another worker's check, a roll); then the check fails, and Temporal tries it again.
BUSY_WAIT: float = 60.0
BUSY_STEP: Final = 1.0
_ID: Final = re.compile(r"[0-9a-f]{16,64}")
_REQUEST_KEYS: Final = frozenset({"filename", "timeout", "address_space", "id"})
_CHUNK: Final = 1 << 20

_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

#: Whether a refused namespace isolation was logged yet (once per process).
_isolation_logged = False

#: Why a call to the sandbox failed: the label values of `SANDBOX_FAILED`.
SANDBOX_FAILURE_REASONS: Final = (
    "busy",
    "unreachable",
    "timeout",
    "transport",
    "check-timeout",
    "crashed",
    "status",
    "bad-answer",
)
#: Checks handed to the sandbox that came back with no verdict: it is down or busy, it
#: refused to start (`sandbox.main` exits when a check cannot be confined), the check
#: crashed or timed out, or the answer broke its bounds.
SANDBOX_FAILED: Final = Counter(
    "scadbuddy_intake_sandbox_failed",
    "Checks handed to the intake sandbox that came back with no verdict, by reason.",
    ["reason"],
)
for _reason in SANDBOX_FAILURE_REASONS:
    SANDBOX_FAILED.labels(reason=_reason)


class CheckCrashedError(RuntimeError):
    """The child timed out, crashed or wrote no verdict: the check could not finish."""


class _SandboxFailedError(CheckCrashedError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str | None = None
    facts: FileFacts | None = None
    preview: bytes | None = None


def _kill_group(process: asyncio.subprocess.Process) -> None:
    # Under start_new_session the group id is the child's pid (template_process.py).
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def child_env(scratch: Path) -> dict[str, str]:
    """The child's environment: `_ENV_KEEP` of the worker's, nothing else."""
    env = {name: os.environ[name] for name in _ENV_KEEP if name in os.environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "MPLCONFIGDIR": str(scratch)})
    return env


def open_dir(name: str | Path, *, dir_fd: int | None = None) -> int:
    """A descriptor of the directory ``name``, never through a symlink."""
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def read_output(dir_fd: int, name: str, cap: int, owner: int | None) -> bytes:
    """``name`` in the directory ``dir_fd``, read as untrusted (the module docstring):
    `CheckCrashedError` unless it is a regular file with one link, owned by ``owner``
    (None: any), of at most ``cap`` bytes."""
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd
        )
    except FileNotFoundError:
        raise CheckCrashedError(f"the check wrote no {name}") from None
    except OSError as error:
        # ELOOP: a symlink; anything else (a FIFO that will not open) is no file either.
        raise CheckCrashedError(f"the check's {name} is not a plain file ({error.errno})") from None
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode) or found.st_nlink != 1:
            raise CheckCrashedError(f"the check's {name} is not a plain file of one link")
        if owner is not None and found.st_uid != owner:
            raise CheckCrashedError(f"the check's {name} is not the expected user's own")
        if found.st_size > cap:
            raise CheckCrashedError(f"the check wrote a {name} past its cap")
        chunks: list[bytes] = []
        taken = 0
        while taken <= cap:
            chunk = os.read(fd, min(1 << 20, cap + 1 - taken))
            if not chunk:
                break
            chunks.append(chunk)
            taken += len(chunk)
        if taken > cap:
            raise CheckCrashedError(f"the check wrote a {name} past its cap")
        return b"".join(chunks)
    finally:
        os.close(fd)


def check_png(data: bytes) -> None:
    """`CheckCrashedError` unless ``data`` starts as a PNG: the signature, then an
    ``IHDR`` chunk of 13 bytes with sane values and a matching CRC
    ([PNG §11.2.2](https://www.w3.org/TR/png-3/#11IHDR))."""
    if len(data) < 33 or data[:8] != PNG_MAGIC or data[8:16] != b"\x00\x00\x00\x0dIHDR":
        raise CheckCrashedError("the check's preview is not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    depth, colour, compression, filtering, interlace = data[24:29]
    (crc,) = struct.unpack(">I", data[29:33])
    if not (0 < width <= MAX_PREVIEW_SIDE and 0 < height <= MAX_PREVIEW_SIDE):
        raise CheckCrashedError("the check's preview is not a PNG")
    if depth not in (1, 2, 4, 8, 16) or colour not in (0, 2, 3, 4, 6):
        raise CheckCrashedError("the check's preview is not a PNG")
    if compression or filtering or interlace not in (0, 1) or zlib.crc32(data[12:29]) != crc:
        raise CheckCrashedError("the check's preview is not a PNG")


def check_request(
    filename: str, timeout: float, address_space: int, request_id: str
) -> dict[str, Any]:
    """A check's request, its bounds checked: `ValueError` for a name that is empty, too
    long or holds a NUL or a separator, a timeout that is not finite and in
    (0, `CHILD_TIMEOUT`], an address space not in (0, `CHILD_ADDRESS_SPACE`], or an id that
    is not 16 to 64 lowercase hex digits."""
    if not 0 < len(filename) <= MAX_FILENAME or "\x00" in filename or "/" in filename:
        raise ValueError("a check's file name is 1 to 255 characters, no NUL or slash")
    if not (math.isfinite(timeout) and 0 < timeout <= CHILD_TIMEOUT):
        raise ValueError("a check's timeout is finite and in (0, CHILD_TIMEOUT]")
    if isinstance(address_space, bool) or not 0 < address_space <= CHILD_ADDRESS_SPACE:
        raise ValueError("a check's address space is in (0, CHILD_ADDRESS_SPACE]")
    if not _ID.fullmatch(request_id):
        raise ValueError("a check's id is 16 to 64 lowercase hex digits")
    return {
        "filename": filename,
        "timeout": timeout,
        "address_space": address_space,
        "id": request_id,
    }


def parse_request(header: str) -> dict[str, Any]:
    """`REQUEST_HEADER`'s value read: at most `MAX_REQUEST_BYTES`, a JSON object of exactly
    the four keys with their types, then `check_request`; `ValueError` otherwise."""
    if len(header) > MAX_REQUEST_BYTES:
        raise ValueError("a check's request is at most 4 KiB")
    request = json.loads(header)
    if not isinstance(request, dict) or set(request) != _REQUEST_KEYS:
        raise ValueError("a check's request names its filename, timeout, address_space and id")
    filename, timeout = request["filename"], request["timeout"]
    space, request_id = request["address_space"], request["id"]
    if not isinstance(filename, str) or not isinstance(request_id, str):
        raise ValueError("a check's filename and id are strings")
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise ValueError("a check's timeout is a number")
    if isinstance(space, bool) or not isinstance(space, int):
        raise ValueError("a check's address space is an integer")
    return check_request(filename, float(timeout), space, request_id)


async def run_child(
    box: str | Path | int,
    filename: str,
    *,
    hide: str,
    strict: bool,
    timeout: float = CHILD_TIMEOUT,
    address_space: int = CHILD_ADDRESS_SPACE,
    cancelled: asyncio.Event | None = None,
) -> int:
    """The child run in ``box`` (its working directory, holding ``upload.bin``: a path, or
    a descriptor of it, which the launcher inherits and ``fchdir``s to) through its
    launcher, with an empty tmpfs over ``hide`` in its own mount namespace (`confine.py`;
    with ``strict``, a step refused exits 126 with no parse); its exit code.
    `CheckCrashedError` past ``timeout``; the group is killed then, on cancellation and
    when ``cancelled`` is set."""
    # -I: the box (its working directory, which holds the customer's file) and the
    # environment never reach its import path.
    parser = [sys.executable, "-I", "-m", "scadbuddy.intake_worker.child"]
    box_fd = box if isinstance(box, int) else None
    argv = confine.command(
        hide, [*parser, filename, str(address_space)], strict=strict, box_fd=box_fd
    )
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        # Where the confined child sees its box (`confine.isolate` binds it there).
        env=child_env(Path(hide) / BOX if box_fd is not None else Path(str(box))),
        cwd=None if isinstance(box, int) else box,
        start_new_session=True,
        # Only its standard streams (all /dev/null) and the box's descriptor, which the
        # launcher closes before the parser starts: no socket (the worker's Temporal and
        # CRUD connections, the sandbox's client connection) is inherited.
        close_fds=True,
        pass_fds=(box_fd,) if box_fd is not None else (),
    )
    waits: list[asyncio.Future[Any]] = [asyncio.ensure_future(process.wait())]
    if cancelled is not None:
        waits.append(asyncio.ensure_future(cancelled.wait()))
    try:
        done, _ = await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        _kill_group(process)
        await process.wait()
        raise
    finally:
        for waiting in waits[1:]:
            waiting.cancel()
    _kill_group(process)
    if waits[0] not in done:
        await process.wait()
        if cancelled is not None and cancelled.is_set():
            raise CheckCrashedError("the check was cancelled")
        raise CheckCrashedError(f"the check timed out after {timeout:g}s")
    return process.returncode if process.returncode is not None else -1


def parse_verdict(raw: bytes, preview: Callable[[], bytes]) -> Verdict:
    """The child's verdict, read as untrusted: JSON with ``outcome`` ``ready`` and valid
    `FileFacts`, or ``refused`` and a reason; a ready one's preview (``preview()``) must be
    a PNG (`check_png`). `CheckCrashedError` otherwise."""
    global _isolation_logged
    try:
        verdict = json.loads(raw)
        outcome = verdict["outcome"]
        isolation = verdict.get("isolation")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CheckCrashedError("the check wrote a verdict that is not one") from None
    if isolation is not None and not _isolation_logged:
        _isolation_logged = True
        logger.warning("the check runs without its own namespaces: %s", str(isolation)[:200])
    try:
        if outcome == "ready":
            facts = FileFacts.model_validate(verdict["facts"])
        elif outcome == "refused":
            return Verdict("refused", reason=str(verdict["reason"])[:500])
        else:
            raise CheckCrashedError("the check wrote a verdict that is not one")
    except (ValidationError, KeyError, TypeError):
        raise CheckCrashedError("the check wrote a verdict that is not one") from None
    png = preview()
    check_png(png)
    return Verdict("ready", facts=facts, preview=png)


def read_verdict(dir_fd: int, owner: int | None) -> Verdict:
    """The verdict and the preview the child wrote in the directory ``dir_fd``, read as
    untrusted (`read_output`), then `parse_verdict`."""
    raw = read_output(dir_fd, VERDICT, MAX_VERDICT_BYTES, owner)
    return parse_verdict(raw, lambda: read_output(dir_fd, PREVIEW, MAX_PREVIEW_BYTES, owner))


def _fail(reason: str, message: str) -> NoReturn:
    raise _SandboxFailedError(reason, message)


_STATUS_REASONS: Final = {500: "crashed", 503: "busy", 504: "check-timeout"}


async def _read_capped(response: httpx.Response, cap: int) -> bytes:
    """The body as sent (never decoded), counted while it is read; past ``cap`` the check
    fails before more is read."""
    data = bytearray()
    async for chunk in response.aiter_raw():
        data += chunk
        if len(data) > cap:
            _fail("bad-answer", "the sandbox's answer is past its cap")
    return bytes(data)


async def _read_answer(response: httpx.Response, request_id: str) -> Verdict:
    """The sandbox's answer, read as untrusted (the module docstring)."""
    if response.status_code != 200:
        error = ""
        with suppress(Exception):
            body = json.loads(await _read_capped(response, MAX_ERROR_BYTES))
            error = "".join(ch for ch in str(body["error"])[:32] if ch.isprintable())
        _fail(
            _STATUS_REASONS.get(response.status_code, "status"),
            f"the sandbox answered {response.status_code} ({error or 'no error'})",
        )
    if response.headers.get(ID_HEADER) != request_id:
        _fail("bad-answer", "the sandbox's answer is not for this check")
    if response.headers.get("content-encoding", "identity") != "identity":
        _fail("bad-answer", "the sandbox's answer is encoded")
    declared = response.headers.get(VERDICT_BYTES_HEADER, "")
    if not (declared.isascii() and declared.isdigit()) or int(declared) > MAX_VERDICT_BYTES:
        _fail("bad-answer", "the sandbox's verdict length is not an integer within its cap")
    length = int(declared)
    body = await _read_capped(response, MAX_ANSWER_BYTES)
    if len(body) < length:
        _fail("bad-answer", "the sandbox's answer is shorter than its verdict")
    preview = body[length:]
    if len(preview) > MAX_PREVIEW_BYTES:
        _fail("bad-answer", "the sandbox's preview is past its cap")
    try:
        return parse_verdict(body[:length], lambda: preview)
    except CheckCrashedError as error:
        _fail("bad-answer", str(error))


async def _file_chunks(fd: int, size: int) -> AsyncIterator[bytes]:
    sent = 0
    while sent < size:
        chunk = os.pread(fd, min(_CHUNK, size - sent), sent)
        if not chunk:
            raise CheckCrashedError("the check's file shrank while it was sent")
        sent += len(chunk)
        yield chunk


async def _call_sandbox(file: Path, request: dict[str, Any], sandbox_url: str) -> Verdict:
    """One ``POST /check``, tried again while the sandbox answers 503 busy or cannot be
    reached, for at most `BUSY_WAIT`."""
    header = json.dumps(request, ensure_ascii=True, separators=(",", ":"))
    timeout = httpx.Timeout(
        connect=10.0, read=request["timeout"] + SANDBOX_GRACE, write=60.0, pool=10.0
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BUSY_WAIT
    fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        size = os.fstat(fd).st_size
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            "Accept-Encoding": "identity",
            REQUEST_HEADER: header,
        }
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            while True:
                try:
                    async with client.stream(
                        "POST",
                        f"{sandbox_url}/check",
                        content=_file_chunks(fd, size),
                        headers=headers,
                    ) as response:
                        return await _read_answer(response, request["id"])
                except _SandboxFailedError as failure:
                    if failure.reason != "busy" or loop.time() >= deadline:
                        raise
                except httpx.ConnectError:
                    if loop.time() >= deadline:
                        _fail("unreachable", "the sandbox is unreachable")
                except httpx.TimeoutException:
                    _fail("timeout", "the sandbox gave no answer in time")
                except httpx.HTTPError as error:
                    _fail("transport", f"the call to the sandbox failed ({type(error).__name__})")
                await asyncio.sleep(BUSY_STEP)
    finally:
        os.close(fd)


async def run_check(
    file: Path,
    filename: str,
    scratch: Path,
    *,
    timeout: float = CHILD_TIMEOUT,
    address_space: int = CHILD_ADDRESS_SPACE,
    sandbox_url: str | None = None,
) -> Verdict:
    """The child's verdict on ``file`` (``scratch``'s ``upload.bin``); `CheckCrashedError`
    when it gave none. With ``sandbox_url`` the sandbox runs it (the module docstring);
    else this process does, in ``scratch``, with the tmpfs over ``scratch``'s parent (the
    work directory of every check)."""
    if file.parent != scratch or file.name != UPLOAD:
        raise ValueError("a check's file is its scratch folder's upload.bin")
    if sandbox_url is not None:
        request = check_request(filename, timeout, address_space, secrets.token_hex(16))
        try:
            return await _call_sandbox(file, request, sandbox_url)
        except _SandboxFailedError as failure:
            SANDBOX_FAILED.labels(reason=failure.reason).inc()
            raise
    code = await run_child(
        scratch,
        filename,
        hide=str(scratch.parent),
        strict=False,
        timeout=timeout,
        address_space=address_space,
    )
    if code != 0:
        raise CheckCrashedError(f"the check exited with {code}")
    scratch_fd = open_dir(scratch)
    try:
        return read_verdict(scratch_fd, os.geteuid())
    finally:
        os.close(scratch_fd)
