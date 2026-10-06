from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library import lsp
from scadbuddy.library.lsp import DEFAULT_CLIENT_ROOT, frame, read_message
from scadbuddy.main import create_app
from tests.conftest import MODEL_SLUG

from .conftest import set_fake_env

# A stand-in for openscad-lsp: it speaks the same Content-Length framing on stdio and
# answers every request with what it saw, so a test can read the server's side of the
# bridge. `seen` is a JSON string on purpose — the bridge rewrites strings that START
# with a root, so one that starts with `{` reaches the test exactly as the server saw it.
FAKE_LSP = """#!/usr/bin/env python3
import json
import os
import signal
import pathlib
import sys
import time

sidecar = pathlib.Path(sys.argv[0]).with_name("fake-env.json")
settings = json.loads(sidecar.read_text()) if sidecar.is_file() else {}
pid_file = settings.get("FAKE_LSP_PID")
if pid_file:
    pathlib.Path(pid_file).write_text(str(os.getpid()))
assert sys.argv[1:] == ["--stdio"], sys.argv

stdin, stdout = sys.stdin.buffer, sys.stdout.buffer


def send(message):
    body = json.dumps(message).encode()
    stdout.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
    stdout.flush()


while True:
    length = None
    while True:
        line = stdin.readline()
        if not line:
            raise SystemExit(0)
        if line == b"\\r\\n":
            break
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    message = json.loads(stdin.read(length))
    method = message.get("method")
    if method == "exit":
        raise SystemExit(0)
    if method == "crash":
        raise SystemExit(3)
    if method == "closeStdin":
        # Stops reading but keeps its stdout open: the next write to it breaks the pipe.
        os.close(0)
        send({"jsonrpc": "2.0", "id": message["id"], "result": None})
        time.sleep(60)
    if method == "hang":
        # Alive, but never answers again: a wedged server.
        time.sleep(60)
    if method == "delay":
        # Answers, but only after a while.
        time.sleep(message["params"]["seconds"])
    if method == "emit":
        # Writes raw bytes as its whole output, then stops writing without exiting.
        stdout.write(message["params"]["raw"].encode())
        stdout.flush()
        os.close(1)
        time.sleep(60)
    if "id" not in message:
        continue
    cwd = pathlib.Path.cwd()
    send({
        "jsonrpc": "2.0",
        "id": message["id"],
        "result": {
            "seen": json.dumps(message.get("params")),
            "cwd": json.dumps(str(cwd)),
            "location": {"uri": (cwd / "helper.scad").as_uri(), "range": None},
            "elsewhere": "file:///usr/share/openscad/libraries/MCAD/units.scad",
        },
    })
"""

CLIENT_ROOT = f"file:///models/{MODEL_SLUG}/"


@pytest.fixture
def fake_lsp(tmp_path: Path) -> str:
    binary = tmp_path / "fake-openscad-lsp"
    binary.write_text(FAKE_LSP, encoding="utf-8")
    binary.chmod(0o755)
    return str(binary)


@pytest.fixture
def pid_file(tmp_path: Path) -> Path:
    path = tmp_path / "lsp.pid"
    set_fake_env(tmp_path, "FAKE_LSP_PID", str(path))
    return path


@pytest.fixture(autouse=True)
def unreaped(monkeypatch: pytest.MonkeyPatch) -> dict[Any, Any]:
    """Each test counts only its own unreaped servers: the module-level registry would
    otherwise carry one test's never-reaped server (and its waiting task) into the next."""
    fresh: dict[Any, Any] = {}
    monkeypatch.setattr(lsp, "_unreaped", fresh)
    return fresh


@pytest.fixture
def settings(settings: Settings, fake_lsp: str) -> Settings:
    return settings.model_copy(update={"openscad_lsp": fake_lsp})


def _initialize(session: WebSocketTestSession, root: str | None = CLIENT_ROOT) -> dict[str, Any]:
    params: dict[str, Any] = {"processId": None, "capabilities": {}}
    if root is not None:
        params["rootUri"] = root
        params["workspaceFolders"] = [{"uri": root, "name": "model"}]
    session.send_json({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    response: dict[str, Any] = session.receive_json()
    return response


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _wait_until(condition: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_the_server_runs_in_the_model_directory_under_its_real_path(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The client names the model by the URI the editor opened it under; openscad-lsp
    resolves `include <helper.scad>` beside the file, so it has to be told where the
    file really is."""
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        result = _initialize(session)["result"]

    real = paths.model_dir(model).as_uri()
    seen = json.loads(result["seen"])
    assert json.loads(result["cwd"]) == str(paths.model_dir(model))
    assert seen["rootUri"] == real + "/"
    assert seen["workspaceFolders"] == [{"uri": real + "/", "name": "model"}]


@pytest.mark.requires_git
def test_a_builtin_runs_the_server_in_its_mirror(
    app: FastAPI, seed_dir: Path, paths: DataPaths
) -> None:
    """A built-in is read-only but still gets completion and hover: its id reaches the
    route percent-encoded, as the editor sends it, and the server runs in the
    `_builtin/` mirror where its sibling files are."""
    bundled = seed_dir / "keychain"
    bundled.mkdir()
    (bundled / "model.scad").write_text("cube(1);\n", encoding="utf-8")
    (bundled / "model.json").write_text(json.dumps({"name": "Keychain"}), encoding="utf-8")
    root = "file:///models/builtin%3Akeychain/"

    with (
        TestClient(app) as client,
        client.websocket_connect("/api/v1/models/builtin%3Akeychain/lsp") as session,
    ):
        result = _initialize(session, root)["result"]

    mirror = paths.builtins / "keychain"
    assert paths.model_dir("builtin:keychain") == mirror
    assert json.loads(result["cwd"]) == str(mirror)
    assert json.loads(result["seen"])["rootUri"] == mirror.as_uri() + "/"
    assert result["location"]["uri"] == root + "helper.scad"


def test_document_uris_are_translated_both_ways(client: TestClient, model: str) -> None:
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        _initialize(session)
        session.send_json(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "textDocument/definition",
                "params": {
                    "textDocument": {"uri": CLIENT_ROOT + "model.scad"},
                    "position": {"line": 0, "character": 0},
                },
            }
        )
        result = session.receive_json()["result"]

    seen = json.loads(result["seen"])
    assert seen["textDocument"]["uri"].endswith(f"/data/models/{model}/model.scad")
    # Out of the model directory and back under the name the editor knows it by.
    assert result["location"]["uri"] == CLIENT_ROOT + "helper.scad"
    # Anything outside the model directory is left as the server named it.
    assert result["elsewhere"] == "file:///usr/share/openscad/libraries/MCAD/units.scad"


def test_unsaved_source_gets_a_scratch_directory_that_goes_with_the_session(
    client: TestClient,
) -> None:
    with client.websocket_connect("/api/v1/lsp") as session:
        result = _initialize(session, "file:///models/new/")["result"]
        scratch = Path(json.loads(result["cwd"]))
        assert scratch.is_dir()
        assert json.loads(result["seen"])["rootUri"] == scratch.as_uri() + "/"

    assert _wait_until(lambda: not scratch.exists())


def test_a_client_that_names_no_root_is_given_the_server_one(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        result = _initialize(session, root=None)["result"]

    assert json.loads(result["seen"])["rootUri"] == paths.model_dir(model).as_uri() + "/"


def test_a_client_that_names_no_root_is_shown_no_server_path(
    client: TestClient, model: str
) -> None:
    """Without a client root the server's paths still go out rewritten (#194)."""
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        result = _initialize(session, root=None)["result"]

    assert result["location"]["uri"] == DEFAULT_CLIENT_ROOT + "helper.scad"
    assert result["elsewhere"] == "file:///usr/share/openscad/libraries/MCAD/units.scad"


def test_messages_before_initialize_are_rewritten(client: TestClient, model: str) -> None:
    """A server that talks before `initialize` names no container path either (#194);
    the client's own root, once named, takes over."""

    def definition(request_id: int, root: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "textDocument/definition",
            "params": {"textDocument": {"uri": root + "model.scad"}},
        }

    assert CLIENT_ROOT != DEFAULT_CLIENT_ROOT
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        session.send_json(definition(2, DEFAULT_CLIENT_ROOT))
        early = session.receive_json()["result"]
        _initialize(session)
        session.send_json(definition(3, CLIENT_ROOT))
        late = session.receive_json()["result"]

    real = f"/data/models/{model}/model.scad"
    assert early["location"]["uri"] == DEFAULT_CLIENT_ROOT + "helper.scad"
    assert json.loads(early["seen"])["textDocument"]["uri"].endswith(real)
    # After `initialize`, the client's own root is the one rewritten, both ways.
    assert json.loads(late["seen"])["textDocument"]["uri"].endswith(real)
    assert late["location"]["uri"] == CLIENT_ROOT + "helper.scad"


def test_closing_the_editor_stops_the_server(
    client: TestClient, model: str, pid_file: Path
) -> None:
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        _initialize(session)
        pid = int(pid_file.read_text())
        assert not _gone(pid)

    assert _wait_until(lambda: _gone(pid))


def test_a_server_that_exits_closes_the_socket(client: TestClient, model: str) -> None:
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        _initialize(session)
        session.send_json({"jsonrpc": "2.0", "method": "exit"})
        with pytest.raises(WebSocketDisconnect):
            session.receive_json()


def test_a_server_that_stops_reading_closes_the_socket(
    client: TestClient, model: str, pid_file: Path
) -> None:
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        _initialize(session)
        pid = int(pid_file.read_text())
        session.send_json({"jsonrpc": "2.0", "id": 2, "method": "closeStdin"})
        session.receive_json()
        session.send_json({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        with pytest.raises(WebSocketDisconnect):
            session.receive_json()

    assert _wait_until(lambda: _gone(pid))


def test_a_server_that_crashes_is_logged(
    client: TestClient, model: str, caplog: pytest.LogCaptureFixture
) -> None:
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        _initialize(session)
        session.send_json({"jsonrpc": "2.0", "method": "crash"})
        with pytest.raises(WebSocketDisconnect):
            session.receive_json()

    assert "openscad-lsp exited with status 3" in caplog.text


@pytest.mark.parametrize(
    "raw",
    [
        "Content-Length: many\r\n\r\n{}",
        "Content-Length: -1\r\n\r\n{}",
        "Content-Length: 8\r\n\r\nnot json",
        "Content-Length: 100\r\n\r\n{}",
        "Content-Length: 6\r\n\r\n[1, 2]",
        'Content-Length: 6\r\n\r\n"text"',
        "Content-Length: 1\r\n\r\n5",
    ],
    ids=["bad-length", "negative-length", "not-json", "truncated", "array", "string", "number"],
)
def test_an_unreadable_server_message_ends_the_session(
    client: TestClient, model: str, pid_file: Path, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        _initialize(session)
        pid = int(pid_file.read_text())
        session.send_json({"jsonrpc": "2.0", "method": "emit", "params": {"raw": raw}})
        with pytest.raises(WebSocketDisconnect):
            session.receive_json()

    assert "openscad-lsp sent an unreadable message" in caplog.text
    assert _wait_until(lambda: _gone(pid))


@pytest.mark.parametrize("frame", ["[1, 2]", '"text"', "null", "not json"])
def test_a_frame_that_is_not_a_json_object_closes_the_session(
    client: TestClient, model: str, pid_file: Path, frame: str
) -> None:
    """Every JSON-RPC message is an object; anything else is refused by code, not
    by an exception out of the bridge."""
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        _initialize(session)
        pid = int(pid_file.read_text())
        session.send_text(frame)
        with pytest.raises(WebSocketDisconnect) as closed:
            session.receive_json()

    assert closed.value.code == 1007
    assert _wait_until(lambda: _gone(pid))


def test_an_unknown_model_is_refused(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect("/api/v1/models/nope/lsp"),
    ):
        pass
    assert refused.value.code == 1008


def test_no_language_server_installed_is_refused(
    settings: Settings, paths: DataPaths, model: str
) -> None:
    """A dev machine or the frontend CI job has no openscad-lsp; the editor carries on
    without it, so the refusal is all there is to say."""
    app = create_app(settings.model_copy(update={"openscad_lsp": "/nonexistent/openscad-lsp"}))
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(f"/api/v1/models/{model}/lsp"),
    ):
        pass
    assert refused.value.code == 1011


def test_sessions_past_the_cap_are_refused(settings: Settings, model: str) -> None:
    """Every editor is a process; the cap is what stops open tabs from being a fork bomb."""
    app: FastAPI = create_app(settings.model_copy(update={"lsp_sessions": 1}))
    route = f"/api/v1/models/{model}/lsp"
    with TestClient(app) as client, client.websocket_connect(route) as first:
        _initialize(first)
        with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(route):
            pass
        assert refused.value.code == 1013


def test_a_wedged_server_is_killed_and_its_slot_freed(
    settings: Settings,
    model: str,
    pid_file: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that stops answering would hold its permit for as long as the editor
    stays open (#201); an unanswered request ends the session instead."""
    monkeypatch.setattr(lsp, "REQUEST_TIMEOUT", 1.0)
    app: FastAPI = create_app(settings.model_copy(update={"lsp_sessions": 1}))
    route = f"/api/v1/models/{model}/lsp"
    with TestClient(app) as client:
        # The app's start reconfigures logging, dropping the handler caplog put there.
        logging.getLogger().addHandler(caplog.handler)
        with client.websocket_connect(route) as session:
            _initialize(session)
            pid = int(pid_file.read_text())
            session.send_json({"jsonrpc": "2.0", "id": 2, "method": "hang"})
            with pytest.raises(WebSocketDisconnect) as closed:
                session.receive_json()

        assert closed.value.code == 1011
        assert _wait_until(lambda: _gone(pid))
        assert "left a request unanswered" in caplog.text
        with client.websocket_connect(route) as again:
            assert "result" in _initialize(again)


def test_a_killed_server_that_is_never_reaped_still_frees_its_slot(
    settings: Settings,
    model: str,
    pid_file: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    unreaped: dict[Any, Any],
) -> None:
    """A process stuck in the kernel outlives SIGKILL; waiting on it forever would
    hold the permit all the same (#201), so cleanup gives up after KILL_WAIT."""
    monkeypatch.setattr(lsp, "REQUEST_TIMEOUT", 1.0)
    monkeypatch.setattr(lsp, "KILL_WAIT", 0.2)
    spawn = asyncio.create_subprocess_exec

    async def unreapable(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)

        async def never() -> int:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(process, "wait", never)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unreapable)
    app: FastAPI = create_app(settings.model_copy(update={"lsp_sessions": 1}))
    route = f"/api/v1/models/{model}/lsp"
    with TestClient(app) as client:
        logging.getLogger().addHandler(caplog.handler)
        with client.websocket_connect(route) as session:
            _initialize(session)
            pid = int(pid_file.read_text())
            session.send_json({"jsonrpc": "2.0", "id": 2, "method": "hang"})
            with pytest.raises(WebSocketDisconnect) as closed:
                session.receive_json()

        assert closed.value.code == 1011
        assert "was not reaped within 0.2s" in caplog.text
        assert "1 killed server(s) not yet reaped" in caplog.text
        assert len(unreaped) == 1
        with client.websocket_connect(route) as again:
            assert "result" in _initialize(again)

    assert _wait_until(lambda: _gone(pid))


def test_unreaped_servers_are_counted(
    settings: Settings,
    model: str,
    pid_file: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server that outlives its kill no longer holds a permit, so the log keeps
    count of how many are still around past SCADBUDDY_LSP_SESSIONS."""
    monkeypatch.setattr(lsp, "REQUEST_TIMEOUT", 1.0)
    monkeypatch.setattr(lsp, "KILL_WAIT", 0.2)
    spawn = asyncio.create_subprocess_exec

    async def unkillable(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)

        async def never() -> int:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(process, "kill", lambda: None)
        monkeypatch.setattr(process, "wait", never)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unkillable)
    app: FastAPI = create_app(settings.model_copy(update={"lsp_sessions": 1}))
    route = f"/api/v1/models/{model}/lsp"
    pids: list[int] = []
    try:
        with TestClient(app) as client:
            logging.getLogger().addHandler(caplog.handler)
            for _ in range(2):
                with client.websocket_connect(route) as session:
                    _initialize(session)
                    pids.append(int(pid_file.read_text()))
                    session.send_json({"jsonrpc": "2.0", "id": 2, "method": "hang"})
                    with pytest.raises(WebSocketDisconnect) as closed:
                        session.receive_json()
                assert closed.value.code == 1011

        assert "1 killed server(s) not yet reaped" in caplog.text
        assert "2 killed server(s) not yet reaped" in caplog.text
    finally:
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_a_late_reaped_server_is_no_longer_counted(
    settings: Settings,
    model: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    unreaped: dict[Any, Any],
) -> None:
    """A server that outlives KILL_WAIT but is reaped later drops out of the count on
    its own, without waiting for another server to be left unreaped."""
    monkeypatch.setattr(lsp, "REQUEST_TIMEOUT", 1.0)
    monkeypatch.setattr(lsp, "KILL_WAIT", 0.2)
    spawn = asyncio.create_subprocess_exec

    async def slow_to_reap(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await spawn(*args, **kwargs)
        wait = process.wait

        async def late() -> int:
            await asyncio.sleep(0.5)
            return await wait()

        monkeypatch.setattr(process, "wait", late)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_to_reap)
    app: FastAPI = create_app(settings)
    with TestClient(app) as client:
        logging.getLogger().addHandler(caplog.handler)
        with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
            _initialize(session)
            session.send_json({"jsonrpc": "2.0", "id": 2, "method": "hang"})
            with pytest.raises(WebSocketDisconnect):
                session.receive_json()

        assert "1 killed server(s) not yet reaped" in caplog.text
        assert _wait_until(lambda: not unreaped)


def test_a_reused_request_id_is_still_watched(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two requests in flight under one id: the first reply leaves the second watched."""
    monkeypatch.setattr(lsp, "REQUEST_TIMEOUT", 1.0)
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        _initialize(session)
        session.send_json(
            {"jsonrpc": "2.0", "id": 2, "method": "delay", "params": {"seconds": 0.3}}
        )
        session.send_json({"jsonrpc": "2.0", "id": 2, "method": "hang"})
        assert session.receive_json()["id"] == 2
        with pytest.raises(WebSocketDisconnect) as closed:
            session.receive_json()
    assert closed.value.code == 1011


def test_an_idle_session_is_not_ended(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an unanswered request counts: an editor left open with nothing to ask
    keeps its server. The timeout stays above the fake server's start, which
    ``initialize`` waits on, so only the idle stretch is judged."""
    monkeypatch.setattr(lsp, "REQUEST_TIMEOUT", 1.0)
    with client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        _initialize(session)
        session.send_json({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        time.sleep(2.5)
        session.send_json({"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
        assert session.receive_json()["id"] == 2


async def test_a_message_is_read_by_its_declared_length() -> None:
    reader = asyncio.StreamReader()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "ünïcode"}).encode()
    reader.feed_data(b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n")
    reader.feed_data(frame(body)[: len(frame(body)) - 5])
    reader.feed_data(frame(body)[-5:] + frame(b"{}"))
    reader.feed_eof()

    assert await read_message(reader) == body
    assert await read_message(reader) == b"{}"
    assert await read_message(reader) is None


def test_frame_counts_bytes_not_characters() -> None:
    assert frame("é".encode()) == b"Content-Length: 2\r\n\r\n\xc3\xa9"


@pytest.fixture
def real_client(settings: Settings) -> Iterator[TestClient]:
    # The real openscad-lsp, not this module's fake.
    app = create_app(
        settings.model_copy(update={"openscad_lsp": Settings.model_fields["openscad_lsp"].default})
    )
    with TestClient(app) as client:
        yield client


@pytest.mark.requires_openscad_lsp
def test_the_real_server_completes_and_documents_through_the_bridge(
    real_client: TestClient, model: str, paths: DataPaths
) -> None:
    (paths.model_dir(model) / "helper.scad").write_text(
        "// A plate the width of the tag.\nmodule plate(w = 10) { cube([w, w, 2]); }\n",
        encoding="utf-8",
    )
    uri = CLIENT_ROOT + "model.scad"
    text = "include <helper.scad>\nplate(width);\ncu\n"
    with real_client.websocket_connect(f"/api/v1/models/{model}/lsp") as session:
        assert "hoverProvider" in _initialize(session)["result"]["capabilities"]
        session.send_json({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        session.send_json(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": uri,
                        "languageId": "openscad",
                        "version": 1,
                        "text": text,
                    }
                },
            }
        )
        requests = {
            2: ("textDocument/completion", {"line": 2, "character": 2}),
            3: ("textDocument/hover", {"line": 1, "character": 1}),
            4: ("textDocument/definition", {"line": 1, "character": 1}),
        }
        for request_id, (method, position) in requests.items():
            session.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": {"textDocument": {"uri": uri}, "position": position},
                }
            )
        responses: dict[int, Any] = {}
        while len(responses) < len(requests):
            message = session.receive_json()
            if "id" in message and "method" not in message:
                responses[message["id"]] = message["result"]
            elif "id" in message:
                session.send_json({"jsonrpc": "2.0", "id": message["id"], "result": None})

    labels = {item["label"] for item in responses[2]["items"]}
    assert any(label.startswith("cube(") for label in labels)
    assert any(label.startswith("plate(") for label in labels)
    assert "A plate the width of the tag." in responses[3]["contents"]["value"]
    assert responses[4][0]["uri"] == CLIENT_ROOT + "helper.scad"


def _lsp_routes(model: str) -> list[str]:
    return [f"/api/v1/models/{model}/lsp", "/api/v1/lsp"]


@pytest.mark.parametrize("which", [0, 1], ids=["model", "scratch"])
@pytest.mark.parametrize("origin", ["https://evil.example", "http://scad.example.com", "null"])
def test_a_foreign_origin_is_refused(
    settings: Settings, model: str, pid_file: Path, which: int, origin: str
) -> None:
    """Browsers do not apply the same-origin policy to sockets, so a page on another
    origin could otherwise start a language server in a model's directory and hold the
    permits every real editor needs (#1317); the realtime socket's rule (#266)."""
    app: FastAPI = create_app(settings.model_copy(update={"lsp_sessions": 1}))
    route = _lsp_routes(model)[which]
    with TestClient(app) as client:
        client.put("/api/v1/settings", json={"public_url": "https://scad.example.com"})
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect(route, headers={"origin": origin}),
        ):
            pass
        assert refused.value.code == 1008
        assert not pid_file.exists()
        # The refusal took no permit: the one there is still serves the UI.
        with client.websocket_connect(route, headers={"origin": "https://scad.example.com"}) as ok:
            assert "result" in _initialize(ok)


@pytest.mark.parametrize("which", [0, 1], ids=["model", "scratch"])
@pytest.mark.parametrize(
    "origin", ["https://scad.example.com:443", "http://localhost:5173", "https://scad.lan"]
)
def test_the_ui_origins_are_accepted(
    app: FastAPI, client: TestClient, model: str, which: int, origin: str
) -> None:
    state = getattr(app.state, STATE_ATTR)
    state.settings = state.settings.model_copy(update={"allowed_origins": "https://scad.lan"})
    client.put("/api/v1/settings", json={"public_url": "https://scad.example.com"})
    with client.websocket_connect(_lsp_routes(model)[which], headers={"origin": origin}) as ok:
        assert "result" in _initialize(ok)
