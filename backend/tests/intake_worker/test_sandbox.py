"""The intake sandbox's HTTP RPC (#6133, spec 2026-11-13 §7): the sandbox, a Deployment of
its own, runs each check the worker hands it over ``POST /check``; the worker reads every
answer as untrusted."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from scadbuddy.intake_worker import confine, runner, sandbox
from scadbuddy.intake_worker.child import PREVIEW, UPLOAD, VERDICT
from scadbuddy.intake_worker.runner import CheckCrashedError, run_check
from scadbuddy.intake_worker.wire import MAX_PREVIEW_BYTES
from tests.intake_worker.test_check import box_stl
from tests.intake_worker.test_runner import _job, _png, _state, needs_confinement

ID = "ab" * 16
#: The RPC token both processes read from `SCADBUDDY_INTAKE_SANDBOX_TOKEN_FILE`.
TOKEN = "Zk3q" * 10


def _token_file(folder: Path, token: str = TOKEN) -> Path:
    """The token in a directory of its own, as its Secret volume mounts it (the launcher
    covers that directory for each check)."""
    path = folder / "rpc" / "token"
    path.parent.mkdir(exist_ok=True)
    path.write_text(token)
    return path


def _request(**changes: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "filename": "box.stl",
        "timeout": 60.0,
        "address_space": runner.CHILD_ADDRESS_SPACE,
        "id": ID,
    }
    request.update(changes)
    return request


def _head(
    request: dict[str, Any] | str | None,
    length: int | None,
    *,
    method: str = "POST",
    path: str = "/check",
    extra: str = "",
    token: str | None = TOKEN,
) -> bytes:
    lines = [f"{method} {path} HTTP/1.1", "Host: sandbox"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    if length is not None:
        lines.append(f"Content-Length: {length}")
    if request is not None:
        value = request if isinstance(request, str) else json.dumps(request)
        lines.append(f"{runner.REQUEST_HEADER}: {value}")
    if extra:
        lines.append(extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


async def _raw(url: str, data: bytes) -> tuple[int, dict[str, str], bytes]:
    """``data`` sent as is; the answer's status, headers and body."""
    parts = urlsplit(url)
    reader, writer = await asyncio.open_connection(parts.hostname, parts.port)
    try:
        writer.write(data)
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(), 30)
    finally:
        writer.close()
    head, _, body = answer.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    headers = {
        name.strip().lower(): value.strip()
        for name, _, value in (line.partition(":") for line in header_lines)
    }
    return int(status_line.split()[1]), headers, body


@contextlib.asynccontextmanager
async def _served(spool: Path) -> AsyncIterator[tuple[str, sandbox.Sandbox]]:
    spool.mkdir(parents=True, exist_ok=True)
    box = sandbox.Sandbox(spool, TOKEN)
    server = await sandbox.start(box, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", box
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
def sleeper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Each check's child replaced by a process that writes its pid and sleeps: no
    confinement needed to test what the sandbox does around it."""
    pidfile = tmp_path / "child.pid"
    code = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(60)"

    def command(
        hide: str,
        program: list[str],
        *,
        strict: bool,
        box_fd: int | None = None,
        covers: tuple[str, ...] = (),
    ) -> list[str]:
        return [sys.executable, "-I", "-c", code]

    monkeypatch.setattr(confine, "command", command)
    return pidfile


async def _until(condition: Callable[[], bool], what: str, limit: float = 20.0) -> None:
    deadline = time.monotonic() + limit
    while not condition():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.02)


def _pid(pidfile: Path) -> int:
    return int(pidfile.read_text())


def _failed(reason: str) -> float:
    value: float = runner.SANDBOX_FAILED.labels(reason=reason)._value.get()
    return value


# -- the sandbox server --------------------------------------------------------------------


@needs_confinement
async def test_the_sandbox_runs_a_ready_check(tmp_path: Path) -> None:
    job = _job(tmp_path, box_stl())
    async with _served(tmp_path / "spool") as (url, _):
        verdict = await run_check(
            job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token=TOKEN
        )
    assert verdict.outcome == "ready" and verdict.facts is not None
    assert verdict.facts.triangles == 12
    assert verdict.preview is not None and verdict.preview.startswith(runner.PNG_MAGIC)
    # The box is removed after the job: no customer's file stays on the sandbox's spool.
    assert list((tmp_path / "spool").iterdir()) == []


@needs_confinement
async def test_the_sandbox_answers_a_refused_file(tmp_path: Path) -> None:
    job = _job(tmp_path, b"not a model at all")
    async with _served(tmp_path / "spool") as (url, _):
        verdict = await run_check(
            job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token=TOKEN
        )
    assert (verdict.outcome, verdict.preview) == ("refused", None)
    assert verdict.reason == "The file is not a 3MF or an STL."


async def test_healthz_answers_200(tmp_path: Path) -> None:
    async with _served(tmp_path / "spool") as (url, _):
        status, _, body = await _raw(url, _head(None, None, method="GET", path="/healthz"))
    assert (status, body) == (200, b"ok")


@pytest.mark.parametrize(
    ("method", "path", "status"), [("GET", "/nowhere", 404), ("GET", "/check", 405)]
)
async def test_an_unknown_route_is_refused(
    tmp_path: Path, method: str, path: str, status: int
) -> None:
    async with _served(tmp_path / "spool") as (url, _):
        answer = await _raw(url, _head(None, None, method=method, path=path))
    assert answer[0] == status


async def test_a_second_check_while_one_runs_is_503(tmp_path: Path, sleeper: Path) -> None:
    """One check at a time (the isolation rule): a second one is refused, never queued,
    and the running one's upload is the only customer file on the spool, in a box of
    mode 0700."""
    job = _job(tmp_path, box_stl())
    spool = tmp_path / "spool"
    async with _served(spool) as (url, box):
        first = asyncio.create_task(
            run_check(
                job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token=TOKEN, timeout=30.0
            )
        )
        await _until(sleeper.exists, "the first check's child never started")
        assert box.busy
        assert (spool / runner.BOX).stat().st_mode & 0o777 == runner.BOX_MODE
        assert (spool / runner.BOX / UPLOAD).read_bytes() == box_stl()
        status, headers, body = await _raw(url, _head(_request(), 3) + b"abc")
        assert status == 503 and json.loads(body)["error"] == "busy"
        assert headers["connection"] == "close"
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        await _until(lambda: not box.busy, "the sandbox stayed busy")


async def test_a_busy_sandbox_refuses_a_large_upload_with_an_answer(
    tmp_path: Path, sleeper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy refusal of a large upload reaches the worker as busy (503), never as a reset
    of the half-sent body, which would fail the attempt as a transport error."""
    monkeypatch.setattr(runner, "BUSY_WAIT", 0.5)
    job = _job(tmp_path, box_stl())
    large = _job(tmp_path, box_stl() + b"\0" * (64 << 20), "check-large")
    async with _served(tmp_path / "spool") as (url, _):
        first = asyncio.create_task(
            run_check(
                job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token=TOKEN, timeout=30.0
            )
        )
        await _until(sleeper.exists, "the first check's child never started")
        before = _failed("busy")
        with pytest.raises(CheckCrashedError, match="503"):
            await run_check(large / UPLOAD, "box.stl", large, sandbox_url=url, sandbox_token=TOKEN)
        assert _failed("busy") == before + 1
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first


async def test_a_busy_sandbox_fails_the_check_once_its_wait_is_spent(
    tmp_path: Path, sleeper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "BUSY_WAIT", 0.3)
    job, other = _job(tmp_path, box_stl()), _job(tmp_path, box_stl(), "check-2")
    async with _served(tmp_path / "spool") as (url, _):
        first = asyncio.create_task(
            run_check(
                job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token=TOKEN, timeout=30.0
            )
        )
        await _until(sleeper.exists, "the first check's child never started")
        before = _failed("busy")
        with pytest.raises(CheckCrashedError, match="503"):
            await run_check(other / UPLOAD, "box.stl", other, sandbox_url=url, sandbox_token=TOKEN)
        assert _failed("busy") == before + 1
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first


async def test_a_body_past_its_cap_is_refused_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "MAX_MODEL_FILE_BYTES", 10)
    async with _served(tmp_path / "spool") as (url, box):
        status, _, body = await _raw(url, _head(_request(), 11) + b"x" * 11)
        assert status == 413 and json.loads(body)["error"] == "too-large"
        assert not box.busy
    assert list((tmp_path / "spool").iterdir()) == []


@pytest.mark.parametrize(
    ("head", "status"),
    [
        (_head(None, 3), 400),  # no request header
        (_head("not json", 3), 400),
        (_head(json.dumps(_request(filename="x" * 5000)), 3), 400),  # past 4 KiB
        (_head(_request(id="short"), 3), 400),
        (_head(_request(id="AB" * 16), 3), 400),
        (_head(_request(timeout="Infinity"), 3), 400),
        (_head(_request(address_space=True), 3), 400),
        (_head(_request(filename="a/b.stl"), 3), 400),
        (_head(_request(), None), 411),  # no Content-Length
        (_head(_request(), 0), 400),
        (_head(_request(), None, extra="Transfer-Encoding: chunked"), 400),
        (_head(_request(), 3, extra="Content-Length: 4"), 400),  # two lengths
        (_head(_request(), 3).replace(b"Content-Length: 3", b"Content-Length: +3"), 400),
    ],
)
async def test_a_bad_request_is_refused_without_a_run(
    tmp_path: Path, head: bytes, status: int, sleeper: Path
) -> None:
    async with _served(tmp_path / "spool") as (url, box):
        answer = await _raw(url, head + b"abc")
        assert answer[0] == status, answer
        assert not box.busy
    assert not sleeper.exists()
    assert list((tmp_path / "spool").iterdir()) == []


async def test_a_head_past_its_cap_is_refused(tmp_path: Path) -> None:
    async with _served(tmp_path / "spool") as (url, _):
        status, _, _ = await _raw(url, _head(_request(), 3, extra="X-Pad: " + "a" * 20_000))
    assert status == 431


@pytest.mark.parametrize(
    "auth",
    [
        None,
        "Bearer " + "x" * len(TOKEN),
        "Bearer " + TOKEN[:-1],
        "Bearer " + TOKEN + "x",
        "bearer " + TOKEN,
        "Basic " + TOKEN,
        TOKEN,
        "Bearer  " + TOKEN,
    ],
)
async def test_a_check_without_the_token_is_401_before_its_body(
    tmp_path: Path, sleeper: Path, monkeypatch: pytest.MonkeyPatch, auth: str | None
) -> None:
    """Only the head is read: the 401 comes while the declared body is still unsent, and
    nothing reaches the spool."""
    received: list[int] = []
    real = sandbox._receive

    async def spy(*args: Any, **kwargs: Any) -> bool:
        received.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(sandbox, "_receive", spy)
    head = _head(_request(), 1 << 20, token=None, extra=f"Authorization: {auth}" if auth else "")
    async with _served(tmp_path / "spool") as (url, box):
        status, headers, _ = await _raw(url, head)
        assert not box.busy
    assert status == 401
    assert headers["www-authenticate"] == "Bearer"
    assert received == []
    assert not sleeper.exists()
    assert list((tmp_path / "spool").iterdir()) == []


async def test_two_authorization_headers_are_401(tmp_path: Path) -> None:
    head = _head(_request(), 3, extra=f"Authorization: Bearer {TOKEN}")
    async with _served(tmp_path / "spool") as (url, _):
        assert (await _raw(url, head + b"abc"))[0] == 401


async def test_a_check_with_a_wrong_token_fails_unauthorized(tmp_path: Path) -> None:
    job = _job(tmp_path, box_stl())
    before = _failed("unauthorized")
    async with _served(tmp_path / "spool") as (url, _):
        with pytest.raises(CheckCrashedError, match="401"):
            await run_check(job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token="w" * 40)
    assert _failed("unauthorized") == before + 1


def test_a_sandbox_call_needs_its_token(tmp_path: Path) -> None:
    job = _job(tmp_path, box_stl())
    with pytest.raises(ValueError, match="token"):
        asyncio.run(run_check(job / UPLOAD, "box.stl", job, sandbox_url="http://127.0.0.1:1"))


async def test_a_body_trickled_past_the_request_deadline_is_cut_off(
    tmp_path: Path, sleeper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One connection cannot hold the one check slot by sending slowly: past
    `REQUEST_DEADLINE` (head and body together) the sandbox drops it, frees the slot and
    keeps no file."""
    monkeypatch.setattr(sandbox, "REQUEST_DEADLINE", 0.6)
    async with _served(tmp_path / "spool") as (url, box):
        parts = urlsplit(url)
        reader, writer = await asyncio.open_connection(parts.hostname, parts.port)
        writer.write(_head(_request(), 1000))
        await writer.drain()
        cut = asyncio.get_running_loop().time()
        dropped = False
        try:
            for _ in range(100):
                writer.write(b"x")
                await writer.drain()
                await asyncio.sleep(0.05)
                if reader.at_eof():
                    dropped = True
                    break
            answer = await asyncio.wait_for(reader.read(), 5)
            dropped = dropped or answer == b""
        except ConnectionError:
            dropped = True
        finally:
            writer.close()
        assert dropped
        assert asyncio.get_running_loop().time() - cut < 3
        await _until(lambda: not box.busy, "the slot stayed taken")
    assert not sleeper.exists()
    assert list((tmp_path / "spool").iterdir()) == []


async def test_a_head_trickled_past_the_request_deadline_is_cut_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "REQUEST_DEADLINE", 0.4)
    async with _served(tmp_path / "spool") as (url, _):
        parts = urlsplit(url)
        reader, writer = await asyncio.open_connection(parts.hostname, parts.port)
        writer.write(b"POST /check HTTP/1.1\r\n")
        await writer.drain()
        try:
            answer = await asyncio.wait_for(reader.read(), 5)
        except ConnectionError:
            answer = b""
        finally:
            writer.close()
    assert answer == b""


async def test_a_check_past_its_timeout_kills_the_child(tmp_path: Path, sleeper: Path) -> None:
    job = _job(tmp_path, box_stl())
    before = _failed("check-timeout")
    async with _served(tmp_path / "spool") as (url, box):
        with pytest.raises(CheckCrashedError, match="504"):
            await run_check(
                job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token=TOKEN, timeout=0.5
            )
        assert not box.busy
    assert _failed("check-timeout") == before + 1
    assert _state(_pid(sleeper)) in (None, "Z", "X")
    assert list((tmp_path / "spool").iterdir()) == []


async def test_a_client_that_goes_away_kills_the_child(
    tmp_path: Path, sleeper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes: list[str] = []
    real = runner.run_child

    async def spy(*args: Any, **kwargs: Any) -> int:
        try:
            return await real(*args, **kwargs)
        except BaseException as error:
            outcomes.append(str(error))
            raise

    monkeypatch.setattr(sandbox, "run_child", spy)
    async with _served(tmp_path / "spool") as (url, box):
        parts = urlsplit(url)
        _, writer = await asyncio.open_connection(parts.hostname, parts.port)
        writer.write(_head(_request(), 3) + b"abc")
        await writer.drain()
        await _until(sleeper.exists, "the child never started")
        writer.close()
        await _until(lambda: bool(outcomes), "the child was never stopped")
        await _until(lambda: not box.busy, "the sandbox stayed busy")
    assert outcomes == ["the check was cancelled"]
    assert _state(_pid(sleeper)) in (None, "Z", "X")
    assert list((tmp_path / "spool").iterdir()) == []


async def test_a_child_that_exits_non_zero_is_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exits(*args: Any, **kwargs: Any) -> int:
        return 7

    monkeypatch.setattr(sandbox, "run_child", exits)
    job = _job(tmp_path, box_stl())
    before = _failed("crashed")
    async with _served(tmp_path / "spool") as (url, _):
        status, _, body = await _raw(url, _head(_request(), 3) + b"abc")
        assert status == 500 and json.loads(body) == {"error": "crashed", "detail": "exit 7"}
        with pytest.raises(CheckCrashedError, match="500"):
            await run_check(job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token=TOKEN)
    assert _failed("crashed") == before + 1


async def test_a_child_that_wrote_no_verdict_is_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def silent(*args: Any, **kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(sandbox, "run_child", silent)
    async with _served(tmp_path / "spool") as (url, _):
        status, _, body = await _raw(url, _head(_request(), 3) + b"abc")
    assert status == 500 and json.loads(body)["error"] == "no-verdict"


async def test_a_launcher_that_does_not_start_is_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_launcher(*args: Any, **kwargs: Any) -> int:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(sandbox, "run_child", no_launcher)
    async with _served(tmp_path / "spool") as (url, box):
        status, _, body = await _raw(url, _head(_request(), 3) + b"abc")
        assert not box.busy
    assert status == 500 and json.loads(body)["error"] == "spawn-failed"


async def test_the_answer_is_framed_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The verdict's bytes, then the preview's: never base64, never bytes inside JSON."""
    verdict = json.dumps({"outcome": "refused", "reason": "r"}).encode()

    async def writes(box: int, *args: Any, **kwargs: Any) -> int:
        for name, data in ((VERDICT, verdict), (PREVIEW, _png())):
            fd = os.open(name, os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=box)
            os.write(fd, data)
            os.close(fd)
        return 0

    monkeypatch.setattr(sandbox, "run_child", writes)
    async with _served(tmp_path / "spool") as (url, _):
        status, headers, body = await _raw(url, _head(_request(), 3) + b"abc")
    assert status == 200
    assert headers["content-type"] == "application/octet-stream"
    assert headers[runner.ID_HEADER.lower()] == ID
    assert headers[runner.VERDICT_BYTES_HEADER.lower()] == str(len(verdict))
    assert body == verdict + _png()


async def test_the_sandbox_clears_earlier_job_processes_before_each_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeper: Path
) -> None:
    cleared: list[bool] = []

    async def clear() -> None:
        cleared.append(True)

    monkeypatch.setattr(sandbox, "clear_job_processes", clear)
    job = _job(tmp_path, box_stl())
    async with _served(tmp_path / "spool") as (url, _):
        with pytest.raises(CheckCrashedError):
            await run_check(
                job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token=TOKEN, timeout=0.2
            )
    assert cleared == [True]


async def test_a_job_process_that_will_not_die_stops_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeper: Path
) -> None:
    async def outlived() -> None:
        raise sandbox.JobOutlivedError("left: [1]")

    monkeypatch.setattr(sandbox, "clear_job_processes", outlived)
    async with _served(tmp_path / "spool") as (url, box):
        status, _, _ = await _raw(url, _head(_request(), 3) + b"abc")
        assert status == 503
        assert isinstance(box.outlived, sandbox.JobOutlivedError)
        assert box.stopped.is_set()
    assert not sleeper.exists()


def test_the_sandbox_takes_no_concurrency_setting() -> None:
    import inspect

    assert set(inspect.signature(sandbox.serve).parameters) == {
        "spool",
        "stop",
        "token",
        "host",
        "port",
        "covers",
    }


@pytest.fixture
def start_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """The sandbox's environment at start, whole: no forbidden variable, no mounted secret,
    a valid token file; nothing past the start's checks runs."""
    for name in sandbox.SANDBOX_FORBIDDEN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sandbox, "SECRETS_DIR", tmp_path / "no-secrets")
    monkeypatch.setenv("SCADBUDDY_INTAKE_SANDBOX_TOKEN_FILE", str(_token_file(tmp_path)))
    monkeypatch.setenv("SCADBUDDY_INTAKE_WORK_DIR", str(tmp_path / "spool"))
    monkeypatch.setattr(sandbox, "forbid_dumping", lambda: None)
    monkeypatch.setattr(sandbox, "probe_isolation", lambda spool, covers=(): "stop here")
    return monkeypatch


def test_the_start_checks_pass_with_a_whole_environment(start_env: pytest.MonkeyPatch) -> None:
    # It gets as far as the confinement probe (stubbed to refuse).
    assert sandbox.main([]) == 3


@pytest.mark.parametrize("port", ["0", "70000", "http", "-1"])
def test_the_sandbox_refuses_a_bad_port(
    start_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], port: str
) -> None:
    start_env.setenv("SCADBUDDY_INTAKE_SANDBOX_PORT", port)
    assert sandbox.main([]) == 2
    assert "SCADBUDDY_INTAKE_SANDBOX_PORT" in capsys.readouterr().out


@pytest.mark.parametrize(
    "token", [None, "missing", "", "\n", "x" * 31, "x" * 16 + " " + "x" * 16, "é" * 40]
)
def test_the_sandbox_refuses_a_missing_empty_or_short_token(
    start_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    token: str | None,
) -> None:
    if token is None:
        start_env.delenv("SCADBUDDY_INTAKE_SANDBOX_TOKEN_FILE")
    elif token == "missing":
        start_env.setenv("SCADBUDDY_INTAKE_SANDBOX_TOKEN_FILE", str(tmp_path / "nowhere"))
    else:
        (tmp_path / "bad").write_text(token)
        start_env.setenv("SCADBUDDY_INTAKE_SANDBOX_TOKEN_FILE", str(tmp_path / "bad"))
    assert sandbox.main([]) == 2
    assert "SCADBUDDY_INTAKE_SANDBOX_TOKEN_FILE" in capsys.readouterr().out


def test_a_token_file_ending_in_a_newline_is_read_without_it(tmp_path: Path) -> None:
    assert runner.read_token(_token_file(tmp_path, TOKEN + "\n")) == TOKEN


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@needs_confinement
async def test_the_sandbox_as_deployed_runs_a_check(tmp_path: Path) -> None:
    """``python -m scadbuddy.intake_worker.sandbox`` itself, non-dumpable as it makes itself
    at start: its child must reach the box without a ``/proc/<pid>/fd`` path of the
    sandbox's, which a non-dumpable process's children cannot follow."""
    job = _job(tmp_path, box_stl())
    spool = tmp_path / "spool"
    spool.mkdir()
    port = _free_port()
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in sandbox.SANDBOX_FORBIDDEN_ENV
    }
    env |= {
        "SCADBUDDY_INTAKE_WORK_DIR": str(spool),
        "SCADBUDDY_INTAKE_SANDBOX_PORT": str(port),
        "SCADBUDDY_INTAKE_SANDBOX_TOKEN_FILE": str(_token_file(tmp_path)),
    }
    served = subprocess.Popen(
        [sys.executable, "-m", "scadbuddy.intake_worker.sandbox"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            assert served.poll() is None, "the sandbox exited at start"
            with contextlib.suppress(OSError, IndexError, ValueError):
                if (await _raw(url, _head(None, None, method="GET", path="/healthz")))[0] == 200:
                    break
            assert time.monotonic() < deadline, "the sandbox never answered /healthz"
            await asyncio.sleep(0.1)
        verdict = await run_check(
            job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token=TOKEN, timeout=60.0
        )
    finally:
        served.terminate()
        log = served.communicate(timeout=30)[0].decode(errors="replace")
    assert verdict.outcome == "ready", log
    assert served.returncode == 0, log


# -- the worker reads every answer as untrusted --------------------------------------------


def _framed(
    request_id: str,
    verdict: bytes,
    preview: bytes = b"",
    *,
    status: str = "200 OK",
    verdict_bytes: str | None = None,
    extra: str = "",
) -> bytes:
    body = verdict + preview
    lines = [
        f"HTTP/1.1 {status}",
        "Content-Type: application/octet-stream",
        f"Content-Length: {len(body)}",
        f"{runner.ID_HEADER}: {request_id}",
        f"{runner.VERDICT_BYTES_HEADER}: "
        + (str(len(verdict)) if verdict_bytes is None else verdict_bytes),
        "Connection: close",
    ]
    if extra:
        lines.append(extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


@contextlib.asynccontextmanager
async def _answering(answer: Callable[[str], bytes]) -> AsyncIterator[str]:
    """A fake sandbox: it reads one request whole, then sends ``answer(the request's id)``."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        headers = {
            name.strip().lower(): value.strip()
            for name, _, value in (
                line.partition(":") for line in head.decode("latin-1").split("\r\n")[1:]
            )
        }
        await reader.readexactly(int(headers["content-length"]))
        request = json.loads(headers[runner.REQUEST_HEADER.lower()])
        writer.write(answer(request["id"]))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


READY = json.dumps(
    {
        "outcome": "ready",
        "facts": {
            "format": "stl",
            "plates": [{"index": 1, "triangles": 12, "size_mm": [1, 1, 1]}],
            "triangles": 12,
        },
    }
).encode()


async def test_a_well_formed_answer_is_read(tmp_path: Path) -> None:
    job = _job(tmp_path, box_stl())
    async with _answering(lambda rid: _framed(rid, READY, _png())) as url:
        verdict = await run_check(
            job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token=TOKEN
        )
    assert verdict.outcome == "ready" and verdict.preview == _png()


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(lambda rid: _framed("cd" * 16, READY, _png()), id="wrong-id"),
        pytest.param(
            lambda rid: _framed(rid, READY, _png()).replace(
                f"{runner.ID_HEADER}: {rid}\r\n".encode(), b""
            ),
            id="no-id",
        ),
        pytest.param(lambda rid: _framed(rid, READY, _png(), verdict_bytes="12a"), id="bytes-nan"),
        pytest.param(lambda rid: _framed(rid, READY, _png(), verdict_bytes="-1"), id="bytes-neg"),
        pytest.param(
            lambda rid: _framed(
                rid, READY, _png(), verdict_bytes=str(runner.MAX_VERDICT_BYTES + 1)
            ),
            id="verdict-past-cap",
        ),
        pytest.param(
            lambda rid: _framed(rid, READY, _png(), verdict_bytes=str(len(READY) + 100)),
            id="verdict-past-body",
        ),
        pytest.param(
            lambda rid: _framed(rid, READY, b"\0" * (MAX_PREVIEW_BYTES + 1)),
            id="preview-past-cap",
        ),
        pytest.param(
            lambda rid: _framed(
                rid,
                b" " * runner.MAX_VERDICT_BYTES,
                b"\0" * (MAX_PREVIEW_BYTES + 1),
            ),
            id="body-past-cap",
        ),
        pytest.param(lambda rid: _framed(rid, READY, b"GIF89a" + b"\0" * 40), id="not-a-png"),
        pytest.param(lambda rid: _framed(rid, READY, b""), id="ready-without-preview"),
        pytest.param(lambda rid: _framed(rid, b"[1, 2]"), id="not-a-verdict"),
        pytest.param(lambda rid: _framed(rid, b'{"outcome": "refused"}'), id="no-reason"),
        pytest.param(
            lambda rid: _framed(rid, READY, _png(), extra="Content-Encoding: gzip"),
            id="encoded",
        ),
    ],
)
async def test_an_answer_out_of_its_bounds_fails_the_check(
    tmp_path: Path, answer: Callable[[str], bytes]
) -> None:
    job = _job(tmp_path, box_stl())
    before = _failed("bad-answer")
    async with _answering(answer) as url:
        with pytest.raises(CheckCrashedError):
            await run_check(job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token=TOKEN)
    assert _failed("bad-answer") == before + 1


@pytest.mark.parametrize(
    ("status", "reason"),
    [("500 Internal Server Error", "crashed"), ("418 I'm a teapot", "status")],
)
async def test_a_non_200_answer_fails_the_check(tmp_path: Path, status: str, reason: str) -> None:
    job = _job(tmp_path, box_stl())
    before = _failed(reason)
    error = json.dumps({"error": "crashed", "detail": "exit 1"}).encode()
    async with _answering(lambda rid: _framed(rid, error, status=status)) as url:
        with pytest.raises(CheckCrashedError, match=status.split()[0]):
            await run_check(job / UPLOAD, "box.stl", job, sandbox_url=url, sandbox_token=TOKEN)
    assert _failed(reason) == before + 1


async def test_an_unreachable_sandbox_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "BUSY_WAIT", 0.2)
    job = _job(tmp_path, box_stl())
    before = _failed("unreachable")
    with pytest.raises(CheckCrashedError, match="unreachable"):
        await run_check(
            job / UPLOAD,
            "box.stl",
            job,
            sandbox_url=f"http://127.0.0.1:{_free_port()}",
            sandbox_token=TOKEN,
        )
    assert _failed("unreachable") == before + 1


def test_the_failed_counter_is_served_with_every_reason() -> None:
    from prometheus_client import generate_latest

    metrics = generate_latest().decode()
    for reason in runner.SANDBOX_FAILURE_REASONS:
        assert f'scadbuddy_intake_sandbox_failed_total{{reason="{reason}"}}' in metrics


@pytest.mark.parametrize(
    "header",
    [
        "x" * (runner.MAX_REQUEST_BYTES + 1),
        "not json",
        "[1]",
        json.dumps({k: v for k, v in _request().items() if k != "id"}),
        json.dumps(_request(extra=1)),
        json.dumps(_request(id="ab" * 7)),  # 14 hex characters
        json.dumps(_request(id="ab" * 33)),  # 66
        json.dumps(_request(id="zz" * 16)),
        json.dumps(_request(filename=7)),
        json.dumps(_request(timeout=True)),
        json.dumps(_request(timeout="1")),
        json.dumps(_request(address_space=1.5)),
    ],
)
def test_a_request_header_out_of_bounds_is_refused(header: str) -> None:
    with pytest.raises(ValueError):
        runner.parse_request(header)


def test_a_request_header_in_bounds_is_read() -> None:
    assert runner.parse_request(json.dumps(_request(timeout=5))) == _request(timeout=5.0)


def test_the_sandbox_refuses_a_token_directory_that_holds_the_spool(
    start_env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tmpfs over a directory hides it only from above: a cover that holds the spool
    (or lies in it) would leave the token reachable by ``..`` from the box, so the start
    refuses it."""
    start_env.setenv("SCADBUDDY_INTAKE_WORK_DIR", str(tmp_path / "rpc" / "spool"))
    assert sandbox.main([]) == 2
    assert "token" in capsys.readouterr().out
